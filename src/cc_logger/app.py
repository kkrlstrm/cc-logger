"""FastAPI app.

POST /hook -- validate the payload, enqueue, return immediately so Claude
              Code's hook turn isn't blocked on DB I/O.
GET  /healthz -- pool + DB liveness.

A background asyncio.Task drains the queue and writes each event in its own
transaction. If the queue grows, that's visible in the /healthz response.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import db, handlers, models

log = logging.getLogger("cc_logger.app")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "info").upper())


class State:
    queue: asyncio.Queue[dict[str, Any]]
    worker_task: asyncio.Task | None = None
    processed: int = 0
    failed: int = 0


async def _worker(state: State) -> None:
    import psycopg
    while True:
        raw = await state.queue.get()
        evname = raw.get("hook_event_name")
        try:
            ev = models.parse_event(raw)
        except Exception:
            log.exception("parse failed: %s", evname)
            state.failed += 1
            state.queue.task_done()
            continue

        for attempt in range(2):
            try:
                async with db.connection() as conn:
                    # 60s: most handlers finish in <100ms, but Stop/SubagentStop/SessionEnd
                    # also ingest the transcript file which can have hundreds of text blocks.
                    await asyncio.wait_for(handlers.dispatch(conn, ev), timeout=60.0)
                state.processed += 1
                break
            except psycopg.OperationalError:
                if attempt == 0:
                    log.warning("op error on %s — retrying with fresh connection", evname)
                    continue
                log.exception("op error on %s — giving up", evname)
                state.failed += 1
            except Exception:
                log.exception("worker failed processing event: %s", evname)
                state.failed += 1
                break
        state.queue.task_done()


@asynccontextmanager
async def lifespan(app: FastAPI):
    state = State()
    state.queue = asyncio.Queue(maxsize=10_000)
    state.worker_task = asyncio.create_task(_worker(state))
    app.state.cc = state
    await db.get_pool()
    try:
        yield
    finally:
        if state.worker_task:
            state.worker_task.cancel()
            try:
                await state.worker_task
            except asyncio.CancelledError:
                pass
        await db.close_pool()


app = FastAPI(title="cc-logger", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    state: State = app.state.cc
    db_ok = True
    db_error: str | None = None
    try:
        async with db.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
                await cur.fetchone()
    except Exception as e:
        db_ok = False
        db_error = type(e).__name__

    payload = {
        "ok": db_ok,
        "db": "up" if db_ok else "down",
        "queue_depth": state.queue.qsize(),
        "queue_capacity": state.queue.maxsize,
        "processed": state.processed,
        "failed": state.failed,
    }
    if db_error:
        payload["db_error"] = db_error
    status = 200 if db_ok else 503
    return JSONResponse(status_code=status, content=payload)


@app.post("/hook")
async def hook(req: Request):
    raw = await req.json()
    state: State = app.state.cc
    try:
        state.queue.put_nowait(raw)
    except asyncio.QueueFull:
        log.warning("queue full, dropping event: %s", raw.get("hook_event_name"))
        return JSONResponse(status_code=503, content={"continue": True, "queue_full": True})
    return {"continue": True}
