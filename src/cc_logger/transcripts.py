# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read Claude Code transcript JSONL files and extract assistant text blocks.

Claude Code writes a JSONL file per session at the `transcript_path` given
in every hook payload. Each line is one message: `user`, `assistant`,
`tool_result`, or various Claude-Code-internal types. Assistant messages
contain a `content` list of blocks: `text`, `thinking`, `tool_use`, etc.

We extract:
    - `text` blocks: Claude's narration / decisions. Plaintext.
    - cross-session messages and notices (`iter_peer_events`) into
      peer_messages / peer_notices. No hook fires when a message arrives,
      so the transcript is the only record of the receiving side.
    - sub-agent provenance (prompt, description, exact parent, workflow run)
      from the agent's own transcript + meta.json, and workflow run records
      from `<session>/workflows/<runId>.json`.

We do NOT extract:
    - `thinking` blocks: encrypted in the transcript (only `signature` is
      present, no plaintext). Anthropic doesn't expose raw reasoning to
      client apps.
    - `tool_use` blocks: already captured via PreToolUse hooks. (`iter_tool_calls`
      rebuilds them only for scripts/backfill-subagents.py, for spans the hooks
      never saw.)
    - User / tool_result messages: not assistant decisions.

Insert behavior is idempotent (PK = message_id + block_index, ON CONFLICT
DO NOTHING). Calling this multiple times for the same transcript is safe.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from psycopg import AsyncConnection
from psycopg.types.json import Json

from .redaction import redact, redact_string

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


# CC_LOGGER_PROJECTS_DIR points a REMOTE recorder at the mirror it writes uploaded
# transcripts into (see mirror_path); on the machine that ran the session it is unset.
_DEFAULT_PROJECTS_DIR = Path(os.environ.get("CC_LOGGER_PROJECTS_DIR")
                             or Path.home() / ".claude" / "projects")

_SAFE_NAME = re.compile(r"^[A-Za-z0-9-][A-Za-z0-9._-]{0,200}$")   # project dirs start with "-"
_SAFE_REL = re.compile(
    r"^(?P<sid>[A-Za-z0-9-]{8,64})(?:\.jsonl"
    r"|/subagents/(?:workflows/wf_[A-Za-z0-9-]{1,64}/)?agent-[A-Za-z0-9]{1,64}\.(?:jsonl|meta\.json)"
    r"|/workflows/wf_[A-Za-z0-9-]{1,64}\.json)$"
)


def mirror_path(project: str, relpath: str, session_id: str,
                projects_dir: Path | None = None) -> Path | None:
    """Where an uploaded transcript file goes on a remote recorder, or None.

    Only the four shapes Claude Code itself writes are accepted (root transcript,
    sub-agent transcript + meta, workflow run record), only for the uploading
    session, and the result must stay inside the projects dir: a request names a
    path, so a lax check here is a file write anywhere the recorder can reach.
    """
    m = _SAFE_REL.match(relpath)
    if not _SAFE_NAME.match(project or "") or project in (".", "..") or not m \
            or m.group("sid") != session_id:
        return None
    base = (projects_dir or _DEFAULT_PROJECTS_DIR).resolve()
    target = (base / project / relpath).resolve()
    return target if target.is_relative_to(base) else None
_TOKEN_KEYS = ("input_tokens", "output_tokens",
               "cache_read_tokens", "cache_creation_tokens", "total_tokens")


def session_root_transcript(
    session_id: str, projects_dir: Path | None = None
) -> Path | None:
    """Locate the ROOT transcript `<proj>/<session_id>.jsonl` for a session."""
    base = projects_dir or _DEFAULT_PROJECTS_DIR
    hits = list(base.glob(f"*/{session_id}.jsonl"))
    return hits[0] if hits else None


def session_subagent_transcripts(
    session_id: str, projects_dir: Path | None = None
) -> list[tuple[str, Path]]:
    """Return (agent_id, path) for every sub-agent / workflow transcript of a
    session.

    Claude Code writes each sub-agent's conversation to its OWN file under
    `<proj>/<session_id>/subagents/agent-<agent_id>.jsonl`; Workflow-tool agents
    are nested one level deeper at `subagents/workflows/wf_*/agent-<agent_id>.jsonl`.
    The root `<session_id>.jsonl` contains none of them (no sidechain lines), so
    a scan that only reads the root file misses ALL sub-agent tokens — which is
    why session token totals undercounted ~3x. `agent_id` is parsed from the
    filename and matches `agent_invocations.agent_id`.
    """
    base = projects_dir or _DEFAULT_PROJECTS_DIR
    out: list[tuple[str, Path]] = []
    for subagents_dir in base.glob(f"*/{session_id}/subagents"):
        for path in subagents_dir.rglob("agent-*.jsonl"):
            out.append((path.stem[len("agent-"):], path))
    return out


def subagent_transcript(
    session_id: str, agent_id: str, projects_dir: Path | None = None
) -> Path | None:
    """Locate ONE sub-agent's own transcript: `.../subagents/**/agent-<id>.jsonl`.

    The SubagentStop hook payload's `transcript_path` is the SESSION's root
    transcript, not the agent's own file. Scanning that and writing the result
    to the agent's invocation row records the root's cumulative total at the
    moment the agent stopped -- so every sub-agent row is grossly inflated,
    siblings that stop together get byte-identical numbers, and summing the
    rows multiplies a running total by the fan-out width. This resolver is how
    a caller gets the agent's real file instead.
    """
    base = projects_dir or _DEFAULT_PROJECTS_DIR
    for subagents_dir in base.glob(f"*/{session_id}/subagents"):
        for path in subagents_dir.rglob(f"agent-{agent_id}.jsonl"):
            return path
    return None


def agent_result(transcript_path: Path) -> str | None:
    """The value a sub-agent actually RETURNED, read from its own transcript.

    `SubagentStop.last_message` arrives empty from the hook stream, so 100% of
    invocation rows recorded no result. The transcript has it, in one of two
    shapes depending on how the agent was asked to answer:

      - schema-validated agents (every Workflow fan-out) end with a
        `StructuredOutput` tool_use whose INPUT is the finding. Their final
        text block is empty, so reading text alone returns nothing for exactly
        the agents whose results matter most.
      - free-text agents end with a normal assistant text block.

    Prefer StructuredOutput, fall back to the last non-empty text block.
    `StructuredOutput` is also absent from the PreToolUse matcher, so it never
    reaches `tool_calls` either -- the transcript is the only record.
    """
    structured: str | None = None
    last_text: str | None = None
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
                for b in (m.get("message") or {}).get("content") or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use" and b.get("name") == "StructuredOutput":
                        structured = json.dumps(b.get("input"), ensure_ascii=False)
                    elif b.get("type") == "text" and (b.get("text") or "").strip():
                        last_text = b["text"]
    except OSError:
        return None
    return structured or last_text


_PROMPT_MAX = 20_000


def agent_meta(transcript_path: Path) -> dict:
    """The `agent-<id>.meta.json` Claude Code writes beside a sub-agent transcript.

    For a sub-agent spawned by the Agent tool it names `agentType`, `description`
    and `toolUseId` -- the parent's Agent tool_use id, an EXACT parent link. The
    hook side can only match on subagent_type, which leaves parallel fan-outs of
    one type ambiguous. Workflow agents carry
    `agentType` and `model` but no toolUseId; their parent is the run.
    """
    meta = transcript_path.with_name(transcript_path.stem + ".meta.json")
    try:
        data = json.loads(meta.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def workflow_run_id_for(transcript_path: Path) -> str | None:
    """`.../subagents/workflows/<run_id>/agent-<id>.jsonl` -> run_id, else None."""
    parent = transcript_path.parent.name
    return parent if parent.startswith("wf_") and transcript_path.parent.parent.name == "workflows" else None


def _user_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p) or None
    return None


def agent_prompt(transcript_path: Path) -> str | None:
    """The task a sub-agent was given: the first user message of its OWN transcript.

    The SubagentStart payload carries no prompt, so prompt_received was NULL on
    every sub-agent row. A workflow agent's prompt keeps its harness preamble
    ("[Workflow harness — computed task] ..."): that is what the agent received.
    """
    try:
        with open(transcript_path, errors="replace") as f:
            for line in f:
                try:
                    m = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if m.get("type") != "user" or m.get("isMeta"):
                    continue
                text = _user_text((m.get("message") or {}).get("content"))
                if text:
                    return text[:_PROMPT_MAX]
    except OSError:
        return None
    return None


def transcript_span(transcript_path: Path) -> dict:
    """First/last timestamp, cwd and first real user prompt of a transcript."""
    out: dict[str, Any] = {"first": None, "last": None, "cwd": None, "prompt": None}
    try:
        with open(transcript_path, errors="replace") as f:
            for line in f:
                try:
                    m = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _parse_ts(m.get("timestamp"))
                if ts:
                    out["first"] = out["first"] or ts
                    out["last"] = ts
                out["cwd"] = out["cwd"] or m.get("cwd")
                if out["prompt"] is None and m.get("type") == "user" and not m.get("isMeta"):
                    text = _user_text((m.get("message") or {}).get("content"))
                    if text and not text.startswith("<"):
                        out["prompt"] = text[:_PROMPT_MAX]
    except OSError:
        pass
    return out


def iter_tool_calls(transcript_path: Path, only: set[str] | None = None) -> Iterator[dict]:
    """Rebuild tool_calls rows from a transcript, for spans the hooks never saw.

    `tool_call_id` is the tool_use id, the same id the PreToolUse hook records,
    so a backfilled row and a live row for one call collide instead of
    duplicating. A call with no result in the file is yielded as 'orphaned'.
    """
    pending: dict[str, dict] = {}
    with open(transcript_path, errors="replace") as f:
        for line in f:
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            content = (m.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            ts = _parse_ts(m.get("timestamp"))
            for b in content:
                if not isinstance(b, dict):
                    continue
                if m.get("type") == "assistant" and b.get("type") == "tool_use":
                    name = b.get("name")
                    if only is not None and name not in only:
                        continue
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    pending[b.get("id")] = {
                        "tool_call_id": b.get("id"),
                        "tool_name": name,
                        "tool_input": inp,
                        "subagent_type": (inp.get("subagent_type") or "general-purpose")
                                         if name == "Agent" else None,
                        "started_at": ts,
                        "tool_response": None,
                        "status": "orphaned",
                        "error": None,
                        "duration_ms": None,
                    }
                elif m.get("type") == "user" and b.get("type") == "tool_result" \
                        and b.get("tool_use_id") in pending:
                    row = pending.pop(b["tool_use_id"])
                    result = m.get("toolUseResult")
                    if b.get("is_error"):
                        row["status"] = "failure"
                        row["error"] = _user_text(b.get("content")) or str(b.get("content"))[:2000]
                    else:
                        row["status"] = "success"
                        row["tool_response"] = result if result is not None else b.get("content")
                    if ts and row["started_at"]:
                        row["duration_ms"] = int((ts - row["started_at"]).total_seconds() * 1000)
                    yield row
    yield from pending.values()


def workflow_run_files(session_id: str, transcript_path: Path) -> list[Path]:
    """`<proj>/<session_id>/workflows/wf_*.json` -- one complete record per run."""
    return sorted((transcript_path.parent / session_id / "workflows").glob("wf_*.json"))


def _ms_ts(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


async def ingest_workflow_runs(
    conn: AsyncConnection, session_id: str, transcript_path: Path
) -> int:
    """Upsert this session's workflow run records. A run file is rewritten while
    the run progresses and once when it completes; it is re-read only when its
    mtime moved, so a Stop in a session with large finished runs costs a stat."""
    files = workflow_run_files(session_id, transcript_path)
    if not files:
        return 0
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT run_id, file_mtime FROM workflow_runs WHERE session_id = %s", (session_id,))
        seen = {r[0]: r[1] for r in await cur.fetchall()}
        written = 0
        for path in files:
            mtime = path.stat().st_mtime
            if seen.get(path.stem) == mtime:
                continue
            try:
                d = json.loads(path.read_text())
            except (OSError, ValueError):
                log.warning("unreadable workflow run file %s", path)
                continue
            args = d.get("args")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {"_raw": args}
            r = redact
            await cur.execute(
                """
                INSERT INTO workflow_runs
                    (run_id, session_id, task_id, workflow_name, summary, status, default_model,
                     started_at, recorded_at, duration_ms, agent_count, total_tokens,
                     total_tool_calls, script, script_path, args, result, phases, logs,
                     progress, file_mtime)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (run_id) DO UPDATE SET
                    status = EXCLUDED.status, recorded_at = EXCLUDED.recorded_at,
                    duration_ms = EXCLUDED.duration_ms, agent_count = EXCLUDED.agent_count,
                    total_tokens = EXCLUDED.total_tokens,
                    total_tool_calls = EXCLUDED.total_tool_calls, result = EXCLUDED.result,
                    phases = EXCLUDED.phases, logs = EXCLUDED.logs,
                    progress = EXCLUDED.progress, file_mtime = EXCLUDED.file_mtime,
                    ingested_at = now()
                """,
                (path.stem, session_id, d.get("taskId"), d.get("workflowName"),
                 d.get("summary"), d.get("status"), d.get("defaultModel"),
                 _ms_ts(d.get("startTime")), _parse_ts(d.get("timestamp")),
                 d.get("durationMs"), d.get("agentCount"), d.get("totalTokens"),
                 d.get("totalToolCalls"), r(d.get("script")), d.get("scriptPath"),
                 Json(r(args)), Json(r(d.get("result"))), Json(r(d.get("phases"))),
                 Json(r(d.get("logs"))), Json(r(d.get("workflowProgress"))), mtime),
            )
            written += 1
    return written


async def apply_agent_provenance(
    conn: AsyncConnection, agent_id: str, transcript_path: Path
) -> None:
    """Fill what the SubagentStart hook cannot know: the prompt, the description,
    the workflow run, and -- from meta.json -- the exact parent Agent call."""
    meta = agent_meta(transcript_path)
    prompt = agent_prompt(transcript_path)
    async with conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE agent_invocations SET
                prompt_received = COALESCE(prompt_received, %s),
                description = COALESCE(description, %s),
                workflow_run_id = COALESCE(workflow_run_id, %s),
                workflow_phase = COALESCE(workflow_phase, %s),
                agent_type = COALESCE(agent_type, %s),
                spawned_by_tool_call_id = COALESCE(%s, spawned_by_tool_call_id),
                parent_invocation_id = COALESCE(
                    (SELECT tc.invocation_id FROM tool_calls tc WHERE tc.tool_call_id = %s),
                    parent_invocation_id)
            WHERE invocation_id = %s
            """,
            (redact_string(prompt) if prompt else None, meta.get("description"),
             workflow_run_id_for(transcript_path), meta.get("workflowPhase"), meta.get("agentType"),
             meta.get("toolUseId"), meta.get("toolUseId"), agent_id),
        )


_IDLE_TAG = "[Cross-session idle notice]"
_DELIVERY_TAG = "[Cross-session delivery notice]"
_NOTICE_ABOUT = re.compile(r'^\[Cross-session [a-z]+ notice\]\s*"([^"]+)"')


def _cross_session_origin(origin: Any) -> dict | None:
    """Return `origin` if it records a message from ANOTHER Claude Code session.

    Three things share `origin.kind == "peer"`, and only one is cross-session:
      - a sub-agent's final report handed back to its parent (`handback`),
      - a background sub-agent messaging its own parent (`senderTaskId`),
      - a message from another session (`from` is that session's address).
    Counting the first two as cross-session overstates the traffic many times over.
    """
    if not isinstance(origin, dict) or origin.get("kind") != "peer":
        return None
    if origin.get("handback") or origin.get("senderTaskId"):
        return None
    return origin


def iter_peer_events(transcript_path: Path) -> Iterator[dict]:
    """Yield the cross-session messaging events recorded in one transcript.

    No hook fires when a message ARRIVES, so the transcript is the only record
    of the receiving side. Event shapes (key `event`):

      sent      -- a SendMessage whose result carries a `msg_id`. Only
                   cross-session sends get one; a send to a sub-agent or
                   teammate does not, so this also filters those out.
      received  -- a message from another session. Stored either as a meta
                   `user` record (arrived while idle and started a turn,
                   delivery="new_turn") or as an `attachment` of type
                   queued_command (arrived mid-turn, delivery="mid_turn").
                   Both carry `origin.msg_id`, which equals the sender's.
      notice    -- an idle or delivery notice addressed to this session. These
                   have no msg_id; the same text appears as a queue-operation
                   and again as the meta user record, so callers dedupe on text.
    """
    pending: dict[str, tuple[dict, str | None]] = {}
    with open(transcript_path, errors="replace") as f:
        for line in f:
            if ("peer" not in line and "SendMessage" not in line
                    and "Cross-session" not in line and "msg_id" not in line):
                continue
            try:
                m = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = m.get("type")
            ts = m.get("timestamp")

            origin, delivery = _cross_session_origin(m.get("origin")), "new_turn"
            if origin is None and kind == "attachment" and isinstance(m.get("attachment"), dict):
                origin, delivery = _cross_session_origin(m["attachment"].get("origin")), "mid_turn"
            if origin is not None:
                body = origin.get("body") or ""
                msg_id = origin.get("msg_id")
                if not msg_id:
                    seed = f"{origin.get('from')}|{ts}|{body}"
                    msg_id = "nomsgid:" + hashlib.sha1(seed.encode()).hexdigest()[:24]
                yield {
                    "event": "received",
                    "msg_id": msg_id,
                    "received_at": _parse_ts(ts),
                    "delivery": delivery,
                    "sender_address": origin.get("from"),
                    "sender_name": origin.get("name"),
                    "sender_mode": origin.get("fromMode"),
                    "hop_chain": origin.get("hopChain"),
                    "body": body,
                }
                continue

            text = None
            if kind == "queue-operation" and m.get("operation") == "enqueue":
                text = m.get("content")
            elif kind == "user" and m.get("isMeta"):
                text = (m.get("message") or {}).get("content")
            if isinstance(text, str) and text.startswith((_IDLE_TAG, _DELIVERY_TAG)):
                about = _NOTICE_ABOUT.match(text)
                yield {
                    "event": "notice",
                    "kind": "idle" if text.startswith(_IDLE_TAG) else "delivery",
                    "about_name": about.group(1) if about else None,
                    "text": text,
                    "created_at": _parse_ts(ts),
                }
                continue

            content = (m.get("message") or {}).get("content")
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                if kind == "assistant" and b.get("type") == "tool_use" and b.get("name") == "SendMessage":
                    pending[b.get("id")] = (b.get("input") or {}, ts)
                elif kind == "user" and b.get("type") == "tool_result" and b.get("tool_use_id") in pending:
                    inp, sent_ts = pending.pop(b["tool_use_id"])
                    result = m.get("toolUseResult")
                    msg_id = result.get("msg_id") if isinstance(result, dict) else None
                    if not msg_id:
                        continue
                    yield {
                        "event": "sent",
                        "msg_id": msg_id,
                        "sent_at": _parse_ts(sent_ts),
                        "addressed_to": inp.get("to"),
                        "summary": inp.get("summary"),
                        # `message` in the transcript; the hook's normalized copy
                        # also carries it as `content`. Empty for a pure
                        # notify_when_idle subscription.
                        "body": inp.get("message") or inp.get("content") or None,
                        "notify_when_idle": bool(inp.get("notify_when_idle")),
                        "send_tool_use_id": b["tool_use_id"],
                        "send_result": result.get("message"),
                    }


def _peer_address(raw: str | None) -> str | None:
    """Normalize a socket path to the `uds:` form that `origin.from` uses."""
    if not raw:
        return None
    return raw if raw.startswith("uds:") else f"uds:{raw}"


async def ingest_peer_events(
    conn: AsyncConnection, session_id: str, transcript_path: Path
) -> dict[str, int]:
    """Upsert this transcript's cross-session events into peer_messages /
    peer_notices. Idempotent. Each message row is filled from both ends: the
    sender's transcript supplies sender_session_id + what it addressed, the
    receiver's supplies receiver_session_id + delivery. When both ends are on
    disk, the row also teaches session_peer_addresses which session owned the
    sender's socket at that time (source='msg_id-join').
    """
    counts = {"sent": 0, "received": 0, "notice": 0}
    redact_enabled = os.getenv("REDACT_SECRETS", "1") not in ("0", "false", "no")

    def clean(s: str | None) -> str | None:
        # '' -> NULL, so an empty side never wins the COALESCE over the other's text
        if not s:
            return None
        return redact_string(s) if redact_enabled else s

    async with conn.cursor() as cur:
        for ev in iter_peer_events(transcript_path):
            if ev["event"] == "sent":
                await cur.execute(
                    """
                    INSERT INTO peer_messages
                        (msg_id, sender_session_id, sent_at, addressed_to, summary, body,
                         notify_when_idle, send_tool_use_id, send_result)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (msg_id) DO UPDATE SET
                        sender_session_id = COALESCE(peer_messages.sender_session_id, EXCLUDED.sender_session_id),
                        sent_at = COALESCE(peer_messages.sent_at, EXCLUDED.sent_at),
                        addressed_to = COALESCE(peer_messages.addressed_to, EXCLUDED.addressed_to),
                        summary = COALESCE(peer_messages.summary, EXCLUDED.summary),
                        body = COALESCE(peer_messages.body, EXCLUDED.body),
                        notify_when_idle = EXCLUDED.notify_when_idle,
                        send_tool_use_id = COALESCE(peer_messages.send_tool_use_id, EXCLUDED.send_tool_use_id),
                        send_result = COALESCE(peer_messages.send_result, EXCLUDED.send_result),
                        updated_at = now()
                    """,
                    (ev["msg_id"], session_id, ev["sent_at"], ev["addressed_to"],
                     clean(ev["summary"]), clean(ev["body"]), ev["notify_when_idle"],
                     ev["send_tool_use_id"], ev["send_result"]),
                )
            elif ev["event"] == "received":
                await cur.execute(
                    """
                    INSERT INTO peer_messages
                        (msg_id, receiver_session_id, received_at, delivery, sender_address,
                         sender_name, sender_mode, hop_chain, body)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (msg_id) DO UPDATE SET
                        receiver_session_id = COALESCE(peer_messages.receiver_session_id, EXCLUDED.receiver_session_id),
                        received_at = COALESCE(peer_messages.received_at, EXCLUDED.received_at),
                        delivery = COALESCE(peer_messages.delivery, EXCLUDED.delivery),
                        sender_address = COALESCE(peer_messages.sender_address, EXCLUDED.sender_address),
                        sender_name = COALESCE(peer_messages.sender_name, EXCLUDED.sender_name),
                        sender_mode = COALESCE(peer_messages.sender_mode, EXCLUDED.sender_mode),
                        hop_chain = COALESCE(peer_messages.hop_chain, EXCLUDED.hop_chain),
                        body = COALESCE(peer_messages.body, EXCLUDED.body),
                        updated_at = now()
                    """,
                    (ev["msg_id"], session_id, ev["received_at"], ev["delivery"],
                     ev["sender_address"], ev["sender_name"], ev["sender_mode"],
                     Json(ev["hop_chain"]) if ev["hop_chain"] else None, clean(ev["body"])),
                )
            else:
                notice_id = hashlib.sha1(
                    f"{session_id}|{ev['kind']}|{ev['text']}".encode()).hexdigest()
                await cur.execute(
                    """
                    INSERT INTO peer_notices (notice_id, session_id, kind, about_name, text, created_at)
                    VALUES (%s, %s, %s, %s, %s, COALESCE(%s, now()))
                    ON CONFLICT (notice_id) DO NOTHING
                    """,
                    (notice_id, session_id, ev["kind"], ev["about_name"],
                     clean(ev["text"]), ev["created_at"]),
                )
            counts[ev["event"]] += 1

            if ev["event"] in ("sent", "received"):
                # Both ends known -> the sender owned that socket at send time.
                # A send addressed by socket also names the receiver's socket.
                await cur.execute(
                    """
                    INSERT INTO session_peer_addresses
                        (session_id, address, pid, source, session_name, recorded_at)
                    SELECT sender_session_id, sender_address,
                           NULLIF(substring(sender_address from '([0-9]+)\\.sock$'), '')::int,
                           'msg_id-join', sender_name, COALESCE(sent_at, received_at)
                    FROM peer_messages
                    WHERE msg_id = %s AND sender_session_id IS NOT NULL
                      AND sender_address LIKE 'uds:%%'
                    UNION ALL
                    SELECT receiver_session_id, addressed_to,
                           NULLIF(substring(addressed_to from '([0-9]+)\\.sock$'), '')::int,
                           'msg_id-join', NULL, COALESCE(received_at, sent_at)
                    FROM peer_messages
                    WHERE msg_id = %s AND receiver_session_id IS NOT NULL
                      AND addressed_to LIKE 'uds:%%'
                    ON CONFLICT (session_id, address) DO UPDATE SET
                        session_name = COALESCE(session_peer_addresses.session_name, EXCLUDED.session_name)
                    """,
                    (ev["msg_id"], ev["msg_id"]),
                )
    return counts


def scan_session_totals(
    session_id: str, projects_dir: Path | None = None
) -> dict:
    """Authoritative per-session token usage: the root transcript PLUS every
    sub-agent and workflow transcript on disk.

    Returns a dict with:
        root        — scan_transcript_stats of the root transcript (or None)
        per_agent   — {agent_id: stats} for each sub-agent transcript
        totals      — summed token columns across root + all sub-agents
        subagent_files — count of sub-agent transcripts scanned

    Unlike summing `agent_invocations` rows, this captures sub-agent and
    workflow transcripts that never produced an invocation row (e.g. Workflow
    agents that don't fire SubagentStop).
    """
    totals = {k: 0 for k in _TOKEN_KEYS}

    def _add(stats: dict | None) -> None:
        if stats:
            for k in _TOKEN_KEYS:
                totals[k] += stats.get(k) or 0

    root_path = session_root_transcript(session_id, projects_dir)
    root_stats = scan_transcript_stats(root_path) if root_path else None
    _add(root_stats)

    per_agent: dict[str, dict] = {}
    for agent_id, path in session_subagent_transcripts(session_id, projects_dir):
        stats = scan_transcript_stats(path)
        if stats is None:
            continue
        if agent_id in per_agent:
            # Rare: same agent_id split across files — sum them.
            for k in _TOKEN_KEYS:
                per_agent[agent_id][k] = (per_agent[agent_id].get(k) or 0) + (stats.get(k) or 0)
        else:
            per_agent[agent_id] = stats
        _add(stats)

    return {
        "root": root_stats,
        "per_agent": per_agent,
        "totals": totals,
        "subagent_files": len(per_agent),
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

    # A sub-agent invocation may only ever be attributed its OWN transcript.
    # Callers used to pass the hook's `transcript_path`, which is the session
    # root -- producing per-agent token rows that were really the root's
    # running total, and root text blocks tagged with a sub-agent's id. Refuse
    # rather than write the wrong number: a blank row is recoverable by the
    # backfill, a plausible wrong one is not.
    root_id = f"root::{session_id}"
    if invocation_id is not None and invocation_id != root_id:
        if p.name == f"{session_id}.jsonl":
            log.warning(
                "refusing to attribute session root transcript to sub-agent %s "
                "(pass its own subagents/**/agent-<id>.jsonl)", invocation_id,
            )
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

        # Cross-session messages. Best-effort: a failure here (e.g. migration
        # 005 not applied yet) must not cost the session its narration/tokens.
        try:
            await ingest_peer_events(conn, session_id, p)
        except Exception:
            log.exception("peer-message ingest failed for %s", p.name)

        # Workflow run records live beside the ROOT transcript only.
        if invocation_id is None or invocation_id == f"root::{session_id}":
            try:
                await ingest_workflow_runs(conn, session_id, p)
            except Exception:
                log.exception("workflow-run ingest failed for %s", p.name)

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
