#!/usr/bin/env python3
# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration 004: Token usage columns.

Claude Code's hook events do NOT carry token totals (SessionEnd only reports a
`reason`), so the original `sessions.total_tokens` path was a dead end and stayed
NULL. Tokens are instead recovered from the transcript, where every assistant
message records a `usage` block. This migration adds the breakdown columns those
sums land in — on both `sessions` (root + all sub-agents) and `agent_invocations`
(per-invocation).

Additive and idempotent: ADD COLUMN IF NOT EXISTS only. `sessions.total_tokens`
already exists from migration 001 and is reused.

Usage:
    python3 migrations/004_tokens.py            # dry-run, shows DDL
    python3 migrations/004_tokens.py --apply    # execute against DATABASE_URL
    python3 migrations/004_tokens.py --verify   # report token columns present
"""
import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
import psycopg

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# token semantics: each value is summed across assistant messages in the
# transcript. cache_read is re-counted per turn (that's how it bills).
# total_tokens = input + output + cache_read + cache_creation.
DDL_STATEMENTS = [
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS input_tokens BIGINT",
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS output_tokens BIGINT",
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS cache_read_tokens BIGINT",
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS cache_creation_tokens BIGINT",
    # sessions.total_tokens already exists (migration 001).
    "ALTER TABLE agent_invocations ADD COLUMN IF NOT EXISTS input_tokens BIGINT",
    "ALTER TABLE agent_invocations ADD COLUMN IF NOT EXISTS output_tokens BIGINT",
    "ALTER TABLE agent_invocations ADD COLUMN IF NOT EXISTS cache_read_tokens BIGINT",
    "ALTER TABLE agent_invocations ADD COLUMN IF NOT EXISTS cache_creation_tokens BIGINT",
    "ALTER TABLE agent_invocations ADD COLUMN IF NOT EXISTS total_tokens BIGINT",
]

TOKEN_COLUMNS = {
    "sessions": {"input_tokens", "output_tokens", "cache_read_tokens",
                 "cache_creation_tokens", "total_tokens"},
    "agent_invocations": {"input_tokens", "output_tokens", "cache_read_tokens",
                          "cache_creation_tokens", "total_tokens"},
}


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
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        ok = True
        for table, expected in TOKEN_COLUMNS.items():
            cur.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema='public' AND table_name=%s
                """,
                (table,),
            )
            actual = {r[0] for r in cur.fetchall()}
            missing = expected - actual
            print(f"{table}: {sorted(expected & actual)}")
            if missing:
                ok = False
                print(f"  MISSING: {sorted(missing)}")
    if not ok:
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
