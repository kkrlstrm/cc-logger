#!/usr/bin/env python3
# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration 001: Initial schema for Claude Code hook event capture.

Creates 4 tables:
    sessions               One row per Claude Code session.
    agent_invocations      One row per agent (root + every sub-agent).
    tool_calls             One row per tool call (filtered allowlist).
    artifacts              Spillover for any tool_input/tool_response field >50KB.

Usage:
    python3 migrations/001_initial_schema.py            # dry-run, shows DDL
    python3 migrations/001_initial_schema.py --apply    # execute against DATABASE_URL
    python3 migrations/001_initial_schema.py --verify   # report tables/columns/indexes after apply
"""
import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
import psycopg

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)

DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS sessions (
        session_id      TEXT PRIMARY KEY,
        started_at      TIMESTAMPTZ NOT NULL,
        ended_at        TIMESTAMPTZ,
        cwd             TEXT,
        model           TEXT,
        initial_prompt  TEXT,
        end_reason      TEXT,
        total_tokens    BIGINT,
        self_rating     SMALLINT CHECK (self_rating BETWEEN 1 AND 5),
        retro_note      TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_invocations (
        invocation_id                   TEXT PRIMARY KEY,
        session_id                      TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
        parent_invocation_id            TEXT REFERENCES agent_invocations(invocation_id) ON DELETE SET NULL,
        spawned_by_tool_call_id         TEXT,
        candidate_parent_tool_call_ids  JSONB,
        agent_id                        TEXT,
        agent_type                      TEXT,
        model                           TEXT,
        prompt_received                 TEXT,
        last_message                    TEXT,
        started_at                      TIMESTAMPTZ NOT NULL,
        ended_at                        TIMESTAMPTZ,
        status                          TEXT NOT NULL DEFAULT 'pending'
                                        CHECK (status IN ('pending', 'completed', 'orphaned')),
        orphaned_at                     TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tool_calls (
        tool_call_id    TEXT PRIMARY KEY,
        session_id      TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
        invocation_id   TEXT REFERENCES agent_invocations(invocation_id) ON DELETE SET NULL,
        tool_name       TEXT NOT NULL,
        subagent_type   TEXT,
        tool_input      JSONB,
        tool_response   JSONB,
        status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'success', 'failure', 'orphaned')),
        error           TEXT,
        duration_ms     INTEGER,
        started_at      TIMESTAMPTZ NOT NULL,
        received_at     TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS artifacts (
        artifact_id     TEXT PRIMARY KEY,
        tool_call_id    TEXT NOT NULL REFERENCES tool_calls(tool_call_id) ON DELETE CASCADE,
        field_name      TEXT NOT NULL,
        full_content    TEXT NOT NULL,
        size_bytes      INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sessions_started_at ON sessions (started_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_agent_invocations_session_id ON agent_invocations (session_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_invocations_parent ON agent_invocations (parent_invocation_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_invocations_spawned_by ON agent_invocations (spawned_by_tool_call_id)",
    "CREATE INDEX IF NOT EXISTS idx_tool_calls_session_id ON tool_calls (session_id)",
    "CREATE INDEX IF NOT EXISTS idx_tool_calls_invocation_id ON tool_calls (invocation_id)",
    "CREATE INDEX IF NOT EXISTS idx_tool_calls_tool_name ON tool_calls (tool_name)",
    "CREATE INDEX IF NOT EXISTS idx_tool_calls_pending ON tool_calls (status) WHERE status = 'pending'",
    "CREATE INDEX IF NOT EXISTS idx_artifacts_tool_call_id ON artifacts (tool_call_id)",
]


def get_dsn() -> str:
    dsn = os.getenv("DATABASE_URL") or os.getenv("NEON_CC_LOGGER_URL")
    if not dsn:
        sys.exit("DATABASE_URL not set in environment (.env)")
    return dsn


def run_apply(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        for stmt in DDL_STATEMENTS:
            cur.execute(stmt)
    print(f"Applied {len(DDL_STATEMENTS)} DDL statements.")


def run_verify(dsn: str) -> None:
    expected_tables = {"sessions", "agent_invocations", "tool_calls", "artifacts"}
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
            ORDER BY table_name
            """
        )
        actual = {r[0] for r in cur.fetchall()}
        missing = expected_tables - actual
        extra = actual - expected_tables
        print(f"Tables present: {sorted(actual)}")
        if missing:
            print(f"  MISSING: {sorted(missing)}")
        if extra:
            print(f"  EXTRA:   {sorted(extra)}")

        cur.execute(
            """
            SELECT tablename, indexname FROM pg_indexes
            WHERE schemaname = 'public'
            ORDER BY tablename, indexname
            """
        )
        for table, idx in cur.fetchall():
            print(f"  {table}: {idx}")
    if missing:
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true", help="execute DDL against the live DB")
    group.add_argument("--verify", action="store_true", help="report on current schema state")
    args = parser.parse_args()

    dsn = get_dsn()

    if args.apply:
        run_apply(dsn)
    elif args.verify:
        run_verify(dsn)
    else:
        print("DRY RUN — would execute the following statements against DATABASE_URL:\n")
        for stmt in DDL_STATEMENTS:
            print(stmt.strip())
            print("---")
        print(f"\nTotal: {len(DDL_STATEMENTS)} statements. Re-run with --apply to execute.")


if __name__ == "__main__":
    main()
