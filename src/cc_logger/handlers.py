# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""One async function per hook_event_name. Each takes a parsed event + a DB
connection and performs the appropriate INSERT/UPDATE.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Json

from . import models, transcripts
from .artifacts import truncate
from .filters import should_capture
from .linking import resolve_parent
from .redaction import redact_string

log = logging.getLogger("cc_logger.handlers")

# Cap on a recovered sub-agent result. Big enough for a schema-validated
# finding, small enough that one runaway agent cannot bloat the table.
_RESULT_MAX = 20_000


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _insert_artifacts(conn: AsyncConnection, tool_call_id: str, artifacts: list[dict]) -> None:
    if not artifacts:
        return
    async with conn.cursor() as cur:
        for a in artifacts:
            await cur.execute(
                """
                INSERT INTO artifacts (artifact_id, tool_call_id, field_name, full_content, size_bytes)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (artifact_id) DO NOTHING
                """,
                (a["artifact_id"], tool_call_id, a["field_name"], a["full_content"], a["size_bytes"]),
            )


async def handle_session_start(conn: AsyncConnection, ev: models.SessionStart) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO sessions (session_id, started_at, cwd, model)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (session_id) DO UPDATE
              SET cwd   = COALESCE(EXCLUDED.cwd, sessions.cwd),
                  model = COALESCE(EXCLUDED.model, sessions.model)
            """,
            (ev.session_id, _now(), ev.cwd, ev.model),
        )


async def handle_user_prompt_submit(conn: AsyncConnection, ev: models.UserPromptSubmit) -> None:
    """UPDATE initial_prompt if not set. INSERT root agent_invocations row if not present."""
    async with conn.cursor() as cur:
        # Ensure session row exists (in case SessionStart was missed)
        await cur.execute(
            """
            INSERT INTO sessions (session_id, started_at, cwd)
            VALUES (%s, %s, %s)
            ON CONFLICT (session_id) DO NOTHING
            """,
            (ev.session_id, _now(), ev.cwd),
        )
        await cur.execute(
            """
            UPDATE sessions
            SET initial_prompt = %s
            WHERE session_id = %s AND initial_prompt IS NULL
            """,
            (ev.prompt, ev.session_id),
        )
        # Create root invocation row once per session (idempotent)
        root_id = f"root::{ev.session_id}"
        await cur.execute(
            """
            INSERT INTO agent_invocations
                (invocation_id, session_id, parent_invocation_id, agent_type,
                 prompt_received, started_at, status)
            VALUES (%s, %s, NULL, 'root', %s, %s, 'pending')
            ON CONFLICT (invocation_id) DO NOTHING
            """,
            (root_id, ev.session_id, ev.prompt, _now()),
        )


async def _root_invocation_id(conn: AsyncConnection, session_id: str) -> str:
    """Return the root invocation_id, creating it if missing.

    PreToolUse from the root agent has no agent_id, so we synthesize.
    """
    root_id = f"root::{session_id}"
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO sessions (session_id, started_at)
            VALUES (%s, %s)
            ON CONFLICT (session_id) DO NOTHING
            """,
            (session_id, _now()),
        )
        await cur.execute(
            """
            INSERT INTO agent_invocations
                (invocation_id, session_id, agent_type, started_at, status)
            VALUES (%s, %s, 'root', %s, 'pending')
            ON CONFLICT (invocation_id) DO NOTHING
            """,
            (root_id, session_id, _now()),
        )
    return root_id


async def _invocation_for_agent_id(conn: AsyncConnection, session_id: str, agent_id: str | None) -> str:
    """Find or fall back to the invocation_id that owns a tool_call.

    For tool calls inside subagents, payload has agent_id. Match it to a row in
    agent_invocations. For root-level calls, agent_id is None and we use the
    synthetic root invocation.
    """
    if not agent_id:
        return await _root_invocation_id(conn, session_id)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT invocation_id FROM agent_invocations WHERE session_id=%s AND agent_id=%s LIMIT 1",
            (session_id, agent_id),
        )
        row = await cur.fetchone()
    if row:
        return row[0]
    # SubagentStart hasn't landed yet — fall back to root, will reconcile later.
    return await _root_invocation_id(conn, session_id)


async def handle_pre_tool_use(conn: AsyncConnection, ev: models.PreToolUse) -> None:
    if not should_capture(ev.tool_name):
        return
    truncated = truncate(ev.tool_input)
    invocation_id = await _invocation_for_agent_id(conn, ev.session_id, ev.agent_id)
    subagent_type = None
    if ev.tool_name == "Agent" and isinstance(ev.tool_input, dict):
        subagent_type = ev.tool_input.get("subagent_type") or "general-purpose"

    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO tool_calls
                (tool_call_id, session_id, invocation_id, tool_name, subagent_type,
                 tool_input, status, started_at, received_at)
            VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s)
            ON CONFLICT (tool_call_id) DO UPDATE
              SET tool_input = EXCLUDED.tool_input,
                  subagent_type = EXCLUDED.subagent_type
            """,
            (
                ev.tool_use_id,
                ev.session_id,
                invocation_id,
                ev.tool_name,
                subagent_type,
                Json(truncated.payload),
                _now(),
                _now(),
            ),
        )
    await _insert_artifacts(conn, ev.tool_use_id, truncated.artifacts)


async def handle_post_tool_use(conn: AsyncConnection, ev: models.PostToolUse) -> None:
    if not should_capture(ev.tool_name):
        return
    truncated = truncate(ev.tool_response)
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE tool_calls
            SET tool_response = %s,
                status = 'success',
                duration_ms = EXTRACT(EPOCH FROM (%s - started_at))::int * 1000
            WHERE tool_call_id = %s
            """,
            (Json(truncated.payload), _now(), ev.tool_use_id),
        )
    await _insert_artifacts(conn, ev.tool_use_id, truncated.artifacts)


async def handle_post_tool_use_failure(conn: AsyncConnection, ev: models.PostToolUseFailure) -> None:
    if not should_capture(ev.tool_name):
        return
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE tool_calls
            SET error = %s,
                status = 'failure',
                duration_ms = EXTRACT(EPOCH FROM (%s - started_at))::int * 1000
            WHERE tool_call_id = %s
            """,
            (ev.error, _now(), ev.tool_use_id),
        )


async def handle_subagent_start(conn: AsyncConnection, ev: models.SubagentStart) -> None:
    spawned_by, candidates, parent_inv = await resolve_parent(conn, ev.session_id, ev.agent_type)
    invocation_id = ev.agent_id
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO agent_invocations
                (invocation_id, session_id, parent_invocation_id, spawned_by_tool_call_id,
                 candidate_parent_tool_call_ids, agent_id, agent_type, prompt_received,
                 started_at, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')
            ON CONFLICT (invocation_id) DO UPDATE
              SET parent_invocation_id = COALESCE(EXCLUDED.parent_invocation_id, agent_invocations.parent_invocation_id),
                  spawned_by_tool_call_id = COALESCE(EXCLUDED.spawned_by_tool_call_id, agent_invocations.spawned_by_tool_call_id),
                  candidate_parent_tool_call_ids = COALESCE(EXCLUDED.candidate_parent_tool_call_ids, agent_invocations.candidate_parent_tool_call_ids),
                  agent_type = COALESCE(EXCLUDED.agent_type, agent_invocations.agent_type),
                  prompt_received = COALESCE(EXCLUDED.prompt_received, agent_invocations.prompt_received)
            """,
            (
                invocation_id,
                ev.session_id,
                parent_inv,
                spawned_by,
                Json(candidates) if candidates else None,
                ev.agent_id,
                ev.agent_type,
                ev.prompt,
                _now(),
            ),
        )


async def handle_subagent_stop(conn: AsyncConnection, ev: models.SubagentStop) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE agent_invocations
            SET last_message = %s,
                ended_at = %s,
                status = 'completed'
            WHERE invocation_id = %s
            """,
            (ev.last_message, _now(), ev.agent_id),
        )
    # Ingest the sub-agent's OWN transcript so its text blocks and token usage
    # land on its own row. `ev.transcript_path` is the session root, not this
    # agent's file -- passing it recorded the root's cumulative total as the
    # agent's usage (see transcripts.subagent_transcript). If the agent's file
    # isn't on disk yet, write nothing; backfill-tokens-model.py recovers it.
    own = transcripts.subagent_transcript(ev.session_id, ev.agent_id)
    if own is None:
        log.warning("no transcript on disk for sub-agent %s; skipping ingest",
                    ev.agent_id)
        return
    await _ingest_subagent(conn, ev.session_id, ev.agent_id, own, ev.last_message)


async def _ingest_subagent(conn: AsyncConnection, session_id: str, agent_id: str,
                           own, last_message: str | None) -> None:
    """Everything recovered from a sub-agent's own files. Shared by SubagentStop
    (the file is local) and TranscriptUpload (a remote machine sent it)."""
    # Prompt, description, workflow run and the exact parent Agent call: none of
    # them is in the hook payload; all are in the agent's own files.
    try:
        await transcripts.apply_agent_provenance(conn, agent_id, own)
    except Exception:
        log.exception("provenance update failed for sub-agent %s", agent_id)
    # The hook's last_message arrives empty, so recover what the agent actually
    # returned from its own transcript (StructuredOutput input, else final text).
    if last_message is None:
        result = transcripts.agent_result(own)
        if result is not None:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE agent_invocations SET last_message = %s "
                    "WHERE invocation_id = %s AND last_message IS NULL",
                    (redact_string(result)[:_RESULT_MAX], agent_id),
                )
    await transcripts.ingest(conn, session_id, str(own), agent_id)


async def handle_stop(conn: AsyncConnection, ev: models.Stop) -> None:
    """Root agent finished a turn. Ingest the transcript incrementally so
    Claude's narration / decisions land in `messages` near-realtime.
    """
    # Root invocation gets the message rows; root_id is synthesized from session_id.
    root_id = f"root::{ev.session_id}"
    await transcripts.ingest(conn, ev.session_id, ev.transcript_path, root_id)


async def handle_session_end(conn: AsyncConnection, ev: models.SessionEnd) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE sessions
            SET ended_at = %s,
                end_reason = %s,
                total_tokens = COALESCE(%s, total_tokens)
            WHERE session_id = %s
            """,
            (_now(), ev.reason, ev.total_tokens, ev.session_id),
        )
        # Sweep stale pendings for this session
        await cur.execute(
            """
            UPDATE tool_calls
            SET status = 'orphaned'
            WHERE session_id = %s AND status = 'pending'
            """,
            (ev.session_id,),
        )
        await cur.execute(
            """
            UPDATE agent_invocations
            SET status = 'orphaned', orphaned_at = %s
            WHERE session_id = %s AND status = 'pending'
            """,
            (_now(), ev.session_id),
        )
    # Final reconciliation pass on the transcript: catch anything Stop missed.
    root_id = f"root::{ev.session_id}"
    await transcripts.ingest(conn, ev.session_id, ev.transcript_path, root_id)


async def handle_peer_address(conn: AsyncConnection, ev: models.PeerAddress) -> None:
    """Record which inbox socket this session owns. A resume or fork starts a
    new process, so the same session can own several sockets over its life."""
    address = transcripts._peer_address(ev.messaging_socket)
    if not address:
        return
    pid = None
    stem = address.rsplit("/", 1)[-1].removesuffix(".sock")
    if stem.isdigit():
        pid = int(stem)
    async with conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO session_peer_addresses
                (session_id, address, pid, source, session_source, session_name, recorded_at)
            VALUES (%s, %s, %s, 'hook', %s, %s, %s)
            ON CONFLICT (session_id, address) DO UPDATE SET
                source = 'hook',
                session_source = COALESCE(EXCLUDED.session_source, session_peer_addresses.session_source),
                session_name = COALESCE(EXCLUDED.session_name, session_peer_addresses.session_name)
            """,
            (ev.session_id, address, pid, ev.source, ev.session_title, _now()),
        )


_UPLOAD_MAX_BYTES = 64 * 1024 * 1024


async def handle_transcript_upload(conn: AsyncConnection, ev: models.TranscriptUpload) -> None:
    """Write a remote machine's transcript files into this recorder's mirror of
    ~/.claude/projects, then run the ingest the local SubagentStop / Stop would.

    The upload and the HTTP hook events race, so the rows the ingest updates are
    created here if they do not exist yet; every write is idempotent.
    """
    written = []
    total = 0
    for rel, text in ev.files.items():
        target = transcripts.mirror_path(ev.project, rel, ev.session_id)
        if target is None:
            log.warning("refusing upload path %r for session %s", rel, ev.session_id)
            continue
        total += len(text)
        if total > _UPLOAD_MAX_BYTES:
            log.warning("upload for %s over %d bytes; rest dropped", ev.session_id, _UPLOAD_MAX_BYTES)
            break
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        written.append(target)
    if not written:
        return

    root_id = await _root_invocation_id(conn, ev.session_id)
    if ev.agent_id:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO agent_invocations (invocation_id, session_id, agent_id, started_at, status)
                VALUES (%s, %s, %s, %s, 'pending')
                ON CONFLICT (invocation_id) DO NOTHING
                """,
                (ev.agent_id, ev.session_id, ev.agent_id, _now()),
            )
        own = next((p for p in written if p.name == f"agent-{ev.agent_id}.jsonl"), None)
        if own is not None:
            await _ingest_subagent(conn, ev.session_id, ev.agent_id, own, None)
        return
    root = next((p for p in written if p.name == f"{ev.session_id}.jsonl"), None)
    if root is not None:
        await transcripts.ingest(conn, ev.session_id, str(root), root_id)


HANDLERS = {
    "SessionStart": handle_session_start,
    "UserPromptSubmit": handle_user_prompt_submit,
    "PreToolUse": handle_pre_tool_use,
    "PostToolUse": handle_post_tool_use,
    "PostToolUseFailure": handle_post_tool_use_failure,
    "SubagentStart": handle_subagent_start,
    "SubagentStop": handle_subagent_stop,
    "Stop": handle_stop,
    "SessionEnd": handle_session_end,
    "PeerAddress": handle_peer_address,
    "TranscriptUpload": handle_transcript_upload,
}


async def dispatch(conn: AsyncConnection, ev: models.HookEnvelope) -> None:
    handler = HANDLERS.get(ev.hook_event_name)
    if handler is None:
        log.debug("no handler for %s, skipping", ev.hook_event_name)
        return
    await handler(conn, ev)
