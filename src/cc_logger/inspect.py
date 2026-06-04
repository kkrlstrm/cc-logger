# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render a single session as a terminal tree.

Layout:
    SESSION <id>
      started <ts>, ended <ts>, end_reason=<...>
      prompt: <first 200 chars of initial_prompt>
      [root]
        Bash      "ls -la"                 12ms   ok
        Agent     general-purpose          5.3s   ok
          [general-purpose <agent_id>]
            WebSearch "..."               4.1s   ok
            WebFetch  "https://..."       8.2s   FAIL
            ...
        Edit      "/tmp/foo.txt"           0ms   ok
"""
from __future__ import annotations

import asyncio
from typing import Any

from . import db


_STATUS_GLYPH = {
    "success": "ok",
    "failure": "FAIL",
    "pending": "pend",
    "orphaned": "orph",
    "completed": "ok",
}


def _fmt_duration(ms: int | None) -> str:
    if ms is None:
        return "    -"
    if ms < 1000:
        return f"{ms:>4}ms"
    return f"{ms/1000:>5.1f}s"


def _fmt_tool_summary(row: dict) -> str:
    name = row["tool_name"]
    inp = row.get("tool_input") or {}
    # Pull a useful 1-line description from common shapes
    desc = ""
    if isinstance(inp, dict):
        if name == "Bash":
            desc = (inp.get("command") or "")[:60]
        elif name in ("WebFetch", "WebSearch"):
            desc = (inp.get("url") or inp.get("query") or "")[:60]
        elif name in ("Edit", "Write"):
            desc = (inp.get("file_path") or "")[:60]
        elif name == "Agent":
            desc = (inp.get("description") or inp.get("subagent_type") or "")[:60]
    return f"{name:10s} {desc!r:<62s} {_fmt_duration(row.get('duration_ms')):>7s}  {_STATUS_GLYPH.get(row.get('status') or '', '?')}"


async def render(session_id: str, file=None) -> None:
    import sys
    out = file or sys.stdout

    async with db.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT session_id, started_at, ended_at, end_reason,
                       cwd, model, initial_prompt
                FROM sessions WHERE session_id = %s
                """,
                (session_id,),
            )
            session = await cur.fetchone()
            if not session:
                print(f"No session with id {session_id!r}", file=out)
                return
            cols = [c.name for c in cur.description]
            session = dict(zip(cols, session))

            await cur.execute(
                """
                SELECT invocation_id, parent_invocation_id, agent_id, agent_type,
                       started_at, ended_at, status,
                       LEFT(COALESCE(last_message, ''), 120) AS last_message
                FROM agent_invocations WHERE session_id = %s
                ORDER BY started_at
                """,
                (session_id,),
            )
            inv_cols = [c.name for c in cur.description]
            invocations = [dict(zip(inv_cols, r)) for r in await cur.fetchall()]

            await cur.execute(
                """
                SELECT tool_call_id, invocation_id, tool_name, status,
                       duration_ms, started_at, tool_input
                FROM tool_calls WHERE session_id = %s
                ORDER BY started_at
                """,
                (session_id,),
            )
            tc_cols = [c.name for c in cur.description]
            tool_calls = [dict(zip(tc_cols, r)) for r in await cur.fetchall()]

            # Pull assistant text blocks (may be empty if Stop hook hasn't run yet)
            try:
                await cur.execute(
                    """
                    SELECT invocation_id, text, position, created_at
                    FROM messages WHERE session_id = %s
                    ORDER BY position
                    """,
                    (session_id,),
                )
                msg_cols = [c.name for c in cur.description]
                messages = [dict(zip(msg_cols, r)) for r in await cur.fetchall()]
            except Exception:
                # `messages` table may not exist on older installs
                messages = []

    # Header
    print("", file=out)
    print(f"SESSION {session['session_id']}", file=out)
    duration = ""
    if session["started_at"] and session["ended_at"]:
        d = (session["ended_at"] - session["started_at"]).total_seconds()
        duration = f"  ({d:.0f}s)"
    print(f"  started {session['started_at']}  ended {session['ended_at'] or 'open'}{duration}", file=out)
    if session.get("end_reason"):
        print(f"  end_reason: {session['end_reason']}", file=out)
    if session.get("cwd"):
        print(f"  cwd: {session['cwd']}", file=out)
    if session.get("model"):
        print(f"  model: {session['model']}", file=out)
    prompt = (session.get("initial_prompt") or "")[:240]
    if prompt:
        print(f"  prompt: {prompt!r}", file=out)
    print("", file=out)

    # Tree
    inv_by_id = {i["invocation_id"]: i for i in invocations}
    children: dict[str | None, list[dict]] = {}
    for i in invocations:
        children.setdefault(i["parent_invocation_id"], []).append(i)

    # Interleave tool_calls and messages per invocation by timestamp.
    # tool_calls have started_at; messages have created_at + position (line
    # number in transcript). We use created_at as the sort key for messages.
    timeline_by_inv: dict[str | None, list[tuple]] = {}
    for tc in tool_calls:
        timeline_by_inv.setdefault(tc["invocation_id"], []).append(
            (tc["started_at"], "tool", tc)
        )
    for m in messages:
        timeline_by_inv.setdefault(m["invocation_id"], []).append(
            (m["created_at"], "msg", m)
        )
    for inv_id in timeline_by_inv:
        timeline_by_inv[inv_id].sort(key=lambda x: x[0])

    def _fmt_msg(msg: dict, indent: str) -> str:
        text = msg["text"].strip()
        # Wrap long lines onto continuation lines for readability
        first = text[:120].replace("\n", " ")
        if len(text) > 120:
            first += "..."
        return f'{indent}  · {first!r}'

    def render_inv(inv: dict, depth: int) -> None:
        indent = "  " * depth
        head = f"{indent}[{inv['agent_type']}"
        if inv.get("agent_id"):
            head += f" {inv['agent_id'][:18]}"
        head += f"  {_STATUS_GLYPH.get(inv['status'], '?')}]"
        print(head, file=out)
        for _ts, kind, item in timeline_by_inv.get(inv["invocation_id"], []):
            if kind == "tool":
                print(f"{indent}  {_fmt_tool_summary(item)}", file=out)
            else:
                print(_fmt_msg(item, indent), file=out)
        for child in children.get(inv["invocation_id"], []):
            render_inv(child, depth + 1)
        if inv.get("last_message"):
            print(f"{indent}  → {inv['last_message']!r}", file=out)

    for root in children.get(None, []):
        render_inv(root, 1)

    # Footer summary
    n_tools = len(tool_calls)
    n_fail = sum(1 for t in tool_calls if t["status"] == "failure")
    n_pend = sum(1 for t in tool_calls if t["status"] == "pending")
    n_subs = sum(1 for i in invocations if i["parent_invocation_id"])
    n_msgs = len(messages)
    print("", file=out)
    print(f"  {len(invocations)} invocations ({n_subs} sub-agents), {n_tools} tool calls "
          f"({n_fail} failed, {n_pend} pending), {n_msgs} text blocks", file=out)


def run(session_id: str) -> None:
    asyncio.run(render(session_id))
