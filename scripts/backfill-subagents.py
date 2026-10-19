#!/usr/bin/env python3
# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backfill sub-agents and workflow runs from the transcripts still on disk.

Three passes, all idempotent:

  1. MISSING sub-agents -- a transcript under `<session>/subagents/**` whose
     agent_id has no agent_invocations row (the logger was down, or its
     database refused writes). Each gets
     its invocation row, tool calls, narration and tokens, marked
     source='backfill'. A session with no sessions row gets one, and its ROOT
     transcript is recovered the same way, so session totals are not just the
     sub-agents' share.
  2. PROVENANCE for every sub-agent on disk, logged or not: the prompt it was
     given, its description, its workflow run and its exact parent Agent call
     (transcripts.apply_agent_provenance -- the same code SubagentStop runs).
  3. WORKFLOWS: every `<session>/workflows/<runId>.json` into workflow_runs, and
     every Workflow launch call into tool_calls (the tool was never captured).

Claude Code deletes transcripts after `cleanupPeriodDays` (30 by default), so what
this cannot reach is gone for good.

Usage:
    uv run python scripts/backfill-subagents.py            # dry-run: counts only
    uv run python scripts/backfill-subagents.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
import psycopg
from psycopg.types.json import Json

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")
sys.path.insert(0, str(_ROOT / "src"))
from cc_logger import transcripts as T  # noqa: E402
from cc_logger.artifacts import truncate  # noqa: E402
from cc_logger.filters import should_capture  # noqa: E402
from cc_logger.redaction import redact_string  # noqa: E402

_RESULT_MAX = 20_000
PROJECTS = Path.home() / ".claude" / "projects"


def subagent_files(projects_dir: Path) -> list[tuple[str, str, Path]]:
    """(session_id, agent_id, path) for every sub-agent transcript on disk."""
    out = []
    for path in projects_dir.rglob("agent-*.jsonl"):
        if "subagents" not in path.parts:
            continue
        session_id = path.parts[path.parts.index("subagents") - 1]
        out.append((session_id, path.stem[len("agent-"):], path))
    return out


async def insert_tool_calls(cur, session_id: str, invocation_id: str, path: Path,
                            only: set[str] | None = None) -> int:
    rows, arts = [], []
    for tc in T.iter_tool_calls(path, only=only):
        if only is None and not should_capture(tc["tool_name"]):
            continue
        tin, tout = truncate(tc["tool_input"]), truncate(tc["tool_response"])
        started = tc["started_at"] or datetime.now(timezone.utc)
        rows.append((tc["tool_call_id"], session_id, invocation_id, tc["tool_name"],
                     tc["subagent_type"], Json(tin.payload), Json(tout.payload),
                     tc["status"], tc["error"], tc["duration_ms"], started, started))
        arts += [(a["artifact_id"], tc["tool_call_id"], a["field_name"], a["full_content"],
                  a["size_bytes"]) for a in tin.artifacts + tout.artifacts]
    if rows:
        await cur.executemany(
            """
            INSERT INTO tool_calls
                (tool_call_id, session_id, invocation_id, tool_name, subagent_type,
                 tool_input, tool_response, status, error, duration_ms, started_at,
                 received_at, source)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'backfill')
            ON CONFLICT (tool_call_id) DO NOTHING
            """, rows)
    if arts:
        await cur.executemany(
            """
            INSERT INTO artifacts (artifact_id, tool_call_id, field_name, full_content, size_bytes)
            SELECT %s,%s,%s,%s,%s
            WHERE EXISTS (SELECT 1 FROM tool_calls WHERE tool_call_id = %s)
            ON CONFLICT (artifact_id) DO NOTHING
            """, [a + (a[1],) for a in arts])
    return len(rows)


async def ensure_session(cur, session_id: str, root: Path | None, fallback: Path,
                         existing: set[str]) -> bool:
    """Create a sessions row + root invocation if missing. True if created."""
    if session_id in existing:
        return False
    span = T.transcript_span(root if root and root.exists() else fallback)
    started = span["first"] or datetime.fromtimestamp(fallback.stat().st_mtime, timezone.utc)
    await cur.execute(
        """
        INSERT INTO sessions (session_id, started_at, ended_at, cwd, initial_prompt, end_reason, source)
        VALUES (%s, %s, %s, %s, %s, 'backfill', 'backfill')
        ON CONFLICT (session_id) DO NOTHING
        """,
        (session_id, started, span["last"], span["cwd"],
         redact_string(span["prompt"]) if span["prompt"] else None))
    await cur.execute(
        """
        INSERT INTO agent_invocations
            (invocation_id, session_id, agent_type, prompt_received, started_at, ended_at,
             status, source)
        VALUES (%s, %s, 'root', %s, %s, %s, 'completed', 'backfill')
        ON CONFLICT (invocation_id) DO NOTHING
        """,
        (f"root::{session_id}", session_id,
         redact_string(span["prompt"]) if span["prompt"] else None, started, span["last"]))
    existing.add(session_id)
    return True


async def run(apply: bool) -> None:
    dsn = os.getenv("DATABASE_URL") or os.getenv("NEON_CC_LOGGER_URL")
    if not dsn:
        sys.exit("DATABASE_URL not set in environment (.env)")
    files = subagent_files(PROJECTS)
    run_files = [p for p in PROJECTS.glob("*/*/workflows/wf_*.json")]

    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        async with conn.cursor() as cur:
            # A backfilled row with no tool calls and no narration was cut off
            # mid-write by an earlier run; treat it as missing so a re-run finishes it.
            await cur.execute(
                """
                SELECT agent_id FROM agent_invocations ai
                WHERE agent_id IS NOT NULL
                  AND NOT (source = 'backfill'
                           AND NOT EXISTS (SELECT 1 FROM tool_calls t WHERE t.invocation_id = ai.invocation_id)
                           AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.invocation_id = ai.invocation_id))
                """)
            logged = {r[0] for r in await cur.fetchall()}
            await cur.execute("SELECT session_id FROM sessions")
            sessions = {r[0] for r in await cur.fetchall()}

        missing = [f for f in files if f[1] not in logged]
        by_kind = Counter("workflow" if T.workflow_run_id_for(p) else "agent" for _, _, p in missing)
        new_sessions = {s for s, _, _ in missing} - sessions
        print(f"sub-agent transcripts on disk: {len(files)}  logged: {len(files) - len(missing)}  "
              f"missing: {len(missing)} {dict(by_kind)}")
        print(f"sessions to create: {len(new_sessions)}  workflow run files: {len(run_files)}")
        if not apply:
            print("\nDRY RUN -- re-run with --apply to write.")
            return

        t0 = time.time()
        stats = Counter()
        async with conn.cursor() as cur:
            # 1a. sessions that exist only on disk: root transcript first, so the
            #     parent Agent calls exist before provenance links to them.
            for sid in sorted(new_sessions):
                any_agent = next(p for s, _, p in missing if s == sid)
                root = PROJECTS / any_agent.relative_to(PROJECTS).parts[0] / f"{sid}.jsonl"
                root = root if root.exists() else None
                await ensure_session(cur, sid, root, any_agent, sessions)
                stats["sessions_created"] += 1
                if root:
                    stats["root_tool_calls"] += await insert_tool_calls(cur, sid, f"root::{sid}", root)
                    await T.ingest(conn, sid, str(root), f"root::{sid}")

            # 1b. the missing sub-agents themselves.
            for i, (sid, aid, path) in enumerate(missing, 1):
                await ensure_session(cur, sid, None, path, sessions)
                span = T.transcript_span(path)
                meta = T.agent_meta(path)
                result = T.agent_result(path)
                started = span["first"] or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                await cur.execute(
                    """
                    INSERT INTO agent_invocations
                        (invocation_id, session_id, agent_id, agent_type, last_message,
                         started_at, ended_at, status, orphaned_at, source)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'backfill')
                    ON CONFLICT (invocation_id) DO NOTHING
                    """,
                    (aid, sid, aid, meta.get("agentType"),
                     redact_string(result)[:_RESULT_MAX] if result else None,
                     started, span["last"], "completed" if result else "orphaned",
                     None if result else span["last"]))
                stats["agent_tool_calls"] += await insert_tool_calls(cur, sid, aid, path)
                await T.ingest(conn, sid, str(path), aid)
                stats["agents_recovered"] += 1
                if i % 100 == 0:
                    print(f"  recovered {i}/{len(missing)} sub-agents ({time.time() - t0:.0f}s)")

            # 2. provenance for every sub-agent on disk.
            for i, (sid, aid, path) in enumerate(files, 1):
                await T.apply_agent_provenance(conn, aid, path)
                stats["provenance_applied"] += 1
                if i % 500 == 0:
                    print(f"  provenance {i}/{len(files)} ({time.time() - t0:.0f}s)")

            # 3. workflow runs + their launch calls.
            for sid in sorted({p.parts[-3] for p in run_files}):
                proj = next(p for p in run_files if p.parts[-3] == sid).parents[2]
                root = proj / f"{sid}.jsonl"
                if not root.exists():
                    stats["workflow_sessions_without_root"] += 1
                    continue
                if await ensure_session(cur, sid, root, root, sessions):
                    stats["sessions_created"] += 1
                # A logged session can still lack its root invocation row (seen in
                # practice), and the launch calls hang off it.
                await cur.execute(
                    """
                    INSERT INTO agent_invocations (invocation_id, session_id, agent_type, started_at,
                                                   status, source)
                    SELECT %s, %s, 'root', started_at, 'completed', 'backfill'
                    FROM sessions WHERE session_id = %s
                    ON CONFLICT (invocation_id) DO NOTHING
                    """, (f"root::{sid}", sid, sid))
                stats["workflow_runs"] += await T.ingest_workflow_runs(conn, sid, root)
                stats["workflow_launch_calls"] += await insert_tool_calls(
                    cur, sid, f"root::{sid}", root, only={"Workflow"})

        print(f"\ndone in {time.time() - t0:.0f}s: {dict(stats)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write to the DB (default: dry-run)")
    args = ap.parse_args()
    asyncio.run(run(args.apply))


if __name__ == "__main__":
    main()
