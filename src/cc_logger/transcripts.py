# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read Claude Code transcript JSONL files and extract assistant text blocks.

Claude Code writes a JSONL file per session at the `transcript_path` given
in every hook payload. Each line is one message: `user`, `assistant`,
`tool_result`, or various Claude-Code-internal types. Assistant messages
contain a `content` list of blocks: `text`, `thinking`, `tool_use`, etc.

We extract:
    - `text` blocks: Claude's narration / decisions. Plaintext.

We do NOT extract:
    - `thinking` blocks: encrypted in the transcript (only `signature` is
      present, no plaintext). Anthropic doesn't expose raw reasoning to
      client apps.
    - `tool_use` blocks: already captured via PreToolUse hooks.
    - User / tool_result messages: not assistant decisions.

Insert behavior is idempotent (PK = message_id + block_index, ON CONFLICT
DO NOTHING). Calling this multiple times for the same transcript is safe.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Iterator

from psycopg import AsyncConnection

from .redaction import redact_string

log = logging.getLogger("cc_logger.transcripts")


def _parse_ts(value: str | None) -> datetime | None:
    """Parse Claude Code's ISO timestamp (e.g., '2026-05-13T09:51:02.278Z')."""
    if not value:
        return None
    try:
        # Python's fromisoformat handles 'Z' in 3.11+
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def scan_transcript_stats(transcript_path: Path) -> dict | None:
    """Single pass over a transcript JSONL returning the model that ran it and
    its summed token usage.

    Neither is available from the hook stream: Claude Code's SessionStart hook
    frequently omits the model, and NO hook event carries token totals
    (SessionEnd reports only a `reason`). But every assistant line records both
    `message.model` and a `message.usage` block, so the transcript is the
    reliable source for each.

    Returns a dict with keys: model (modal assistant model, or None),
    input_tokens / output_tokens / cache_read_tokens / cache_creation_tokens
    (summed across assistant messages), total_tokens (sum of those four), and
    assistant_messages (count with usage). Returns None if the file is
    unreadable or has no assistant messages.

    Token note: each assistant message's `usage` is for its own API call;
    summing across messages yields what was actually billed for this
    transcript. cache-read is re-counted per turn, which is how it bills.
    """
    counts: dict[str, int] = {}
    agg = {"input_tokens": 0, "output_tokens": 0,
           "cache_read_tokens": 0, "cache_creation_tokens": 0}
    seen = 0
    try:
        with open(transcript_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    m = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if m.get("type") != "assistant":
                    continue
                msg = m.get("message") or {}
                model = msg.get("model")
                if isinstance(model, str) and model and model != "<synthetic>":
                    counts[model] = counts.get(model, 0) + 1
                u = msg.get("usage") or {}
                if u:
                    seen += 1
                    agg["input_tokens"] += u.get("input_tokens") or 0
                    agg["output_tokens"] += u.get("output_tokens") or 0
                    agg["cache_read_tokens"] += u.get("cache_read_input_tokens") or 0
                    agg["cache_creation_tokens"] += u.get("cache_creation_input_tokens") or 0
    except OSError:
        return None
    if not counts and seen == 0:
        return None
    return {
        "model": max(counts, key=counts.get) if counts else None,
        "total_tokens": sum(agg.values()),
        "assistant_messages": seen,
        **agg,
    }


def _iter_text_blocks(transcript_path: Path) -> Iterator[dict]:
    """Yield {message_id, block_index, text, position, created_at} for every
    text block in every assistant message in the JSONL.

    `created_at` is the transcript's recorded timestamp for that message line —
    NOT when we ingested it. This lets inspect.py interleave text blocks with
    tool calls on the real timeline.
    """
    position = 0
    with open(transcript_path) as f:
        for line in f:
            position += 1
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            if m.get("type") != "assistant":
                continue
            msg = m.get("message") or {}
            content = msg.get("content") or []
            if not isinstance(content, list):
                continue
            message_id = msg.get("id") or m.get("uuid") or m.get("messageId")
            if not message_id:
                continue
            ts = _parse_ts(m.get("timestamp"))
            for idx, block in enumerate(content):
                if not isinstance(block, dict):
                    continue
                if block.get("type") != "text":
                    continue
                text = block.get("text") or ""
                if not text.strip():
                    continue
                yield {
                    "message_id": str(message_id),
                    "block_index": idx,
                    "text": text,
                    "position": position,
                    "created_at": ts,
                }


async def ingest(
    conn: AsyncConnection,
    session_id: str,
    transcript_path: str | None,
    invocation_id: str | None = None,
) -> int:
    """Read the transcript at `transcript_path` and INSERT any text blocks
    not already present. Returns the number of rows newly inserted.

    Best-effort: if the file is missing or unreadable, logs a warning and
    returns 0. The capture pipeline shouldn't crash because of a missing
    transcript.
    """
    if not transcript_path:
        return 0
    p = Path(transcript_path)
    if not p.exists():
        log.warning("transcript file not found: %s", p)
        return 0

    inserted = 0
    redact_enabled = os.getenv("REDACT_SECRETS", "1") not in ("0", "false", "no")

    async with conn.cursor() as cur:
        for block in _iter_text_blocks(p):
            text = block["text"]
            if redact_enabled:
                text = redact_string(text)
            # COALESCE on created_at: if transcript had no timestamp, fall
            # back to DB default (now()).
            await cur.execute(
                """
                INSERT INTO messages
                    (message_id, block_index, session_id, invocation_id,
                     role, block_type, text, position, created_at)
                VALUES (%s, %s, %s, %s, 'assistant', 'text', %s, %s, COALESCE(%s, now()))
                ON CONFLICT (message_id, block_index) DO NOTHING
                """,
                (
                    block["message_id"],
                    block["block_index"],
                    session_id,
                    invocation_id,
                    text,
                    block["position"],
                    block["created_at"],
                ),
            )
            if cur.rowcount:
                inserted += 1

        # Backfill the model + token usage that actually ran, from the
        # transcript. Neither is reliably available from the hook stream, but
        # both are recorded on every assistant line. Only fills/corrects.
        stats = scan_transcript_stats(p)
        if stats:
            root_id = f"root::{session_id}"
            is_root = invocation_id is None or invocation_id == root_id
            # Per-invocation: write model + tokens to this invocation's own row.
            # The root invocation is the real row 'root::<session_id>'; a
            # sub-agent's is keyed by its agent_id. (Earlier this branch wrote
            # the ROOT transcript's model to agent_invocations and never to
            # sessions, because every caller passes a non-None invocation_id —
            # which is why sessions.model stayed NULL.)
            if invocation_id is not None:
                await cur.execute(
                    """
                    UPDATE agent_invocations SET
                        model = COALESCE(%s, model),
                        input_tokens = %s, output_tokens = %s,
                        cache_read_tokens = %s, cache_creation_tokens = %s,
                        total_tokens = %s
                    WHERE invocation_id = %s
                    """,
                    (stats["model"], stats["input_tokens"], stats["output_tokens"],
                     stats["cache_read_tokens"], stats["cache_creation_tokens"],
                     stats["total_tokens"], invocation_id),
                )
            # Session-level model comes from the ROOT transcript only.
            if is_root and stats["model"]:
                await cur.execute(
                    "UPDATE sessions SET model = %s "
                    "WHERE session_id = %s AND model IS DISTINCT FROM %s",
                    (stats["model"], session_id, stats["model"]),
                )
            # Session token totals = sum across all the session's invocations
            # (root + every sub-agent). Recomputed on each ingest so it stays
            # correct as sub-agent transcripts land at SubagentStop.
            await cur.execute(
                """
                UPDATE sessions s SET
                    input_tokens = t.in_tok, output_tokens = t.out_tok,
                    cache_read_tokens = t.cr_tok, cache_creation_tokens = t.cc_tok,
                    total_tokens = t.tot_tok
                FROM (
                    SELECT COALESCE(sum(input_tokens), 0) AS in_tok,
                           COALESCE(sum(output_tokens), 0) AS out_tok,
                           COALESCE(sum(cache_read_tokens), 0) AS cr_tok,
                           COALESCE(sum(cache_creation_tokens), 0) AS cc_tok,
                           COALESCE(sum(total_tokens), 0) AS tot_tok
                    FROM agent_invocations WHERE session_id = %s
                ) t
                WHERE s.session_id = %s
                """,
                (session_id, session_id),
            )

    if inserted:
        log.info("ingested %d new text blocks from %s", inserted, p.name)
    return inserted
