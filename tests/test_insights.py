# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regression coverage for the cross-session insights report."""
import os
import uuid

import pytest


@pytest.mark.asyncio
async def test_headline_totals_do_not_multiply_tools_by_invocations():
    """A session may have many invocations, but each tool call counts once."""
    dsn = os.getenv("DATABASE_URL") or os.getenv("NEON_CC_LOGGER_URL")
    if not dsn:
        pytest.skip("DATABASE_URL not set")

    import psycopg

    from cc_logger.insights import _HEADLINE_SQL, _query_all

    session_id = f"test-insights-{uuid.uuid4()}"
    root_id = f"root::{session_id}"
    child_ids = [f"child-{uuid.uuid4()}", f"child-{uuid.uuid4()}"]
    tool_ids = [f"tool-{uuid.uuid4()}", f"tool-{uuid.uuid4()}"]

    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        async with conn.cursor() as cur:
            before = (await _query_all(cur, _HEADLINE_SQL, ("1",)))[0]
            await cur.execute(
                "INSERT INTO sessions (session_id, started_at) VALUES (%s, now())",
                (session_id,),
            )
            await cur.execute(
                """INSERT INTO agent_invocations
                   (invocation_id, session_id, agent_type, started_at, status)
                   VALUES (%s, %s, 'root', now(), 'completed')""",
                (root_id, session_id),
            )
            for child_id in child_ids:
                await cur.execute(
                    """INSERT INTO agent_invocations
                       (invocation_id, session_id, parent_invocation_id, agent_type, started_at, status)
                       VALUES (%s, %s, %s, 'worker', now(), 'completed')""",
                    (child_id, session_id, root_id),
                )
            for tool_id, status, invocation_id in zip(
                tool_ids, ("success", "failure"), (root_id, child_ids[0]), strict=True
            ):
                await cur.execute(
                    """INSERT INTO tool_calls
                       (tool_call_id, session_id, invocation_id, tool_name, status, started_at)
                       VALUES (%s, %s, %s, 'Bash', %s, now())""",
                    (tool_id, session_id, invocation_id, status),
                )

            try:
                after = (await _query_all(cur, _HEADLINE_SQL, ("1",)))[0]
                assert after["sessions"] - before["sessions"] == 1
                assert after["tools"] - before["tools"] == 2
                assert after["fails"] - before["fails"] == 1
                assert after["subs"] - before["subs"] == 2
            finally:
                await cur.execute("DELETE FROM sessions WHERE session_id = %s", (session_id,))
