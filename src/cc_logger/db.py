# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Postgres connection management.

A persistent autocommit connection used by the queue worker. If a write
fails with an operational error, we close and reconnect on the next call.

Works with any Postgres: local Docker Compose, Neon (use the DIRECT
endpoint, NOT the pooler endpoint), Supabase, RDS, etc.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from dotenv import load_dotenv

_ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(_ENV_PATH)

log = logging.getLogger("cc_logger.db")
_conn: psycopg.AsyncConnection | None = None
_conn_lock = asyncio.Lock()


def _dsn() -> str:
    # DATABASE_URL is the canonical env var. NEON_CC_LOGGER_URL is supported
    # as a legacy fallback for installs that predate the rename.
    dsn = os.getenv("DATABASE_URL") or os.getenv("NEON_CC_LOGGER_URL")
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL not set (.env missing or incomplete). See .env.example for setup."
        )
    return dsn


async def _connect() -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(_dsn(), autocommit=True)


async def _get_conn() -> psycopg.AsyncConnection:
    global _conn
    async with _conn_lock:
        if _conn is None or _conn.closed:
            _conn = await _connect()
        return _conn


async def _drop_conn() -> None:
    global _conn
    async with _conn_lock:
        if _conn is not None:
            try:
                await _conn.close()
            except Exception:
                pass
            _conn = None


async def get_pool():
    """Compatibility shim for app.lifespan — pre-warm the connection."""
    await _get_conn()


async def close_pool() -> None:
    await _drop_conn()


@asynccontextmanager
async def connection():
    """Yield the persistent connection. On OperationalError, drop and retry once."""
    try:
        conn = await _get_conn()
        yield conn
    except psycopg.OperationalError:
        log.warning("connection lost, will reconnect on next use")
        await _drop_conn()
        raise


async def smoke_test() -> dict:
    async with connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT current_database(), current_user, version()")
            row = await cur.fetchone()
            await cur.execute(
                """
                SELECT table_name FROM information_schema.tables
                WHERE table_schema='public' AND table_type='BASE TABLE'
                ORDER BY table_name
                """
            )
            tables = [r[0] for r in await cur.fetchall()]
    return {"database": row[0], "user": row[1], "version": row[2], "tables": tables}
