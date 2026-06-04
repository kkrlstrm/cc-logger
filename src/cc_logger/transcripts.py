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


def _scan_model(transcript_path: Path) -> str | None:
    """Return the model that actually ran this transcript — the most frequent
    assistant `message.model` across the file.

    Claude Code's SessionStart hook frequently omits the model, leaving
    `sessions.model` NULL, which breaks model-vs-model comparison. The transcript
    records the real model on every assistant line, so it's the reliable source.
    """
    counts: dict[str, int] = {}
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
                model = (m.get("message") or {}).get("model")
                if isinstance(model, str) and model and model != "<synthetic>":
                    counts[model] = counts.get(model, 0) + 1
    except OSError:
        return None
    return max(counts, key=counts.get) if counts else None


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

        # Backfill the model that actually ran, from the transcript. For the
        # root transcript this sets sessions.model; for a sub-agent transcript
        # it sets that invocation's model. Only fills/corrects, never clobbers
        # with NULL.
        model = _scan_model(p)
        if model:
            if invocation_id is None:
                await cur.execute(
                    "UPDATE sessions SET model = %s "
                    "WHERE session_id = %s AND model IS DISTINCT FROM %s",
                    (model, session_id, model),
                )
            else:
                await cur.execute(
                    "UPDATE agent_invocations SET model = %s "
                    "WHERE invocation_id = %s AND model IS DISTINCT FROM %s",
                    (model, invocation_id, model),
                )

    if inserted:
        log.info("ingested %d new text blocks from %s", inserted, p.name)
    return inserted
