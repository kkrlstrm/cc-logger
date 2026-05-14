"""Subagent → parent Agent tool_call resolution.

Claude Code doesn't tell us which Agent tool_call spawned a given SubagentStart.
Both share session_id. We match on agent_type. If exactly one pending Agent
tool_call in this session has subagent_type == agent_type, bind directly.
If multiple, store all candidates and resolve offline.
"""
from __future__ import annotations

from psycopg import AsyncConnection


async def resolve_parent(
    conn: AsyncConnection,
    session_id: str,
    agent_type: str,
) -> tuple[str | None, list[str], str | None]:
    """Look up pending Agent tool_calls matching this subagent_type.

    Returns (spawned_by_tool_call_id, candidate_tool_call_ids, parent_invocation_id).
    If exactly one match, spawned_by and parent_invocation_id are populated and
    candidates is empty. If multiple, spawned_by/parent are None and candidates
    has all ids. If zero, all three are None.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT tool_call_id, invocation_id
            FROM tool_calls
            WHERE session_id = %s
              AND tool_name = 'Agent'
              AND subagent_type = %s
              AND status = 'pending'
            ORDER BY started_at ASC
            """,
            (session_id, agent_type),
        )
        rows = await cur.fetchall()

    if not rows:
        return None, [], None
    if len(rows) == 1:
        tool_call_id, invocation_id = rows[0]
        return tool_call_id, [], invocation_id
    return None, [r[0] for r in rows], None
