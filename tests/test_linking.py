# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for linking.resolve_parent.

These use psycopg + a real Postgres if DATABASE_URL is set; otherwise they're
skipped. Pure-unit testing of resolve_parent without a DB would require
mocking psycopg's async cursor, which is heavy. Skipping is fine — the CI
job sets up Postgres via the docker workflow.
"""
import os
import uuid
import pytest


@pytest.fixture
def has_db():
    return bool(os.getenv("DATABASE_URL") or os.getenv("NEON_CC_LOGGER_URL"))


@pytest.mark.asyncio
async def test_resolve_parent_zero_matches(has_db):
    if not has_db:
        pytest.skip("DATABASE_URL not set")
    import psycopg
    from cc_logger.linking import resolve_parent

    dsn = os.getenv("DATABASE_URL") or os.getenv("NEON_CC_LOGGER_URL")
    sid = f"test-link-zero-{uuid.uuid4()}"
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        # No Agent tool_calls for this session at all
        spawned_by, candidates, parent_inv = await resolve_parent(conn, sid, "general-purpose")
        assert spawned_by is None
        assert candidates == []
        assert parent_inv is None


@pytest.mark.asyncio
async def test_resolve_parent_one_match(has_db):
    if not has_db:
        pytest.skip("DATABASE_URL not set")
    import psycopg
    from cc_logger.linking import resolve_parent

    dsn = os.getenv("DATABASE_URL") or os.getenv("NEON_CC_LOGGER_URL")
    sid = f"test-link-one-{uuid.uuid4()}"
    tool_id = f"tool-{uuid.uuid4()}"
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO sessions (session_id, started_at) VALUES (%s, now())",
                (sid,),
            )
            inv_id = f"root::{sid}"
            await cur.execute(
                """INSERT INTO agent_invocations
                   (invocation_id, session_id, agent_type, started_at, status)
                   VALUES (%s, %s, 'root', now(), 'pending')""",
                (inv_id, sid),
            )
            await cur.execute(
                """INSERT INTO tool_calls
                   (tool_call_id, session_id, invocation_id, tool_name, subagent_type,
                    status, started_at)
                   VALUES (%s, %s, %s, 'Agent', 'Explore', 'pending', now())""",
                (tool_id, sid, inv_id),
            )
        try:
            spawned_by, candidates, parent_inv = await resolve_parent(conn, sid, "Explore")
            assert spawned_by == tool_id
            assert candidates == []
            assert parent_inv == inv_id
        finally:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM sessions WHERE session_id=%s", (sid,))


@pytest.mark.asyncio
async def test_resolve_parent_multiple_candidates(has_db):
    if not has_db:
        pytest.skip("DATABASE_URL not set")
    import psycopg
    from cc_logger.linking import resolve_parent

    dsn = os.getenv("DATABASE_URL") or os.getenv("NEON_CC_LOGGER_URL")
    sid = f"test-link-many-{uuid.uuid4()}"
    ids = [f"tool-{i}-{uuid.uuid4()}" for i in range(3)]
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO sessions (session_id, started_at) VALUES (%s, now())",
                (sid,),
            )
            inv_id = f"root::{sid}"
            await cur.execute(
                """INSERT INTO agent_invocations
                   (invocation_id, session_id, agent_type, started_at, status)
                   VALUES (%s, %s, 'root', now(), 'pending')""",
                (inv_id, sid),
            )
            for tid in ids:
                await cur.execute(
                    """INSERT INTO tool_calls
                       (tool_call_id, session_id, invocation_id, tool_name, subagent_type,
                        status, started_at)
                       VALUES (%s, %s, %s, 'Agent', 'Explore', 'pending', now())""",
                    (tid, sid, inv_id),
                )
        try:
            spawned_by, candidates, parent_inv = await resolve_parent(conn, sid, "Explore")
            assert spawned_by is None
            assert set(candidates) == set(ids)
            assert parent_inv is None
        finally:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM sessions WHERE session_id=%s", (sid,))
