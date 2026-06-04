#!/usr/bin/env python3
# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration 003: Assistant message capture.

Adds a `messages` table that stores Claude's text/narration blocks
extracted from the Claude Code transcript JSONL files. This is the
"what Claude was thinking out loud" layer — the decisions and framing
between tool calls.

Note: Claude's extended `thinking` blocks are encrypted in the transcript
(only a `signature` is present, no plaintext). We capture only `text`
blocks. See docs/PRIVACY.md.

Usage:
    python3 migrations/003_messages.py            # dry-run
    python3 migrations/003_messages.py --apply
"""
import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
import psycopg

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS messages (
        message_id      TEXT NOT NULL,
        block_index     INTEGER NOT NULL,
        session_id      TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
        invocation_id   TEXT REFERENCES agent_invocations(invocation_id) ON DELETE SET NULL,
        role            TEXT NOT NULL DEFAULT 'assistant',
        block_type      TEXT NOT NULL DEFAULT 'text',
        text            TEXT NOT NULL,
        position        INTEGER NOT NULL,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (message_id, block_index)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_messages_session_position ON messages (session_id, position)",
    "CREATE INDEX IF NOT EXISTS idx_messages_invocation ON messages (invocation_id)",
    """
    CREATE OR REPLACE VIEW vw_session_messages AS
    SELECT m.session_id, m.invocation_id, m.position, m.text, m.created_at
    FROM messages m
    ORDER BY m.session_id, m.position
    """,
]


def get_dsn() -> str:
    dsn = os.getenv("DATABASE_URL") or os.getenv("NEON_CC_LOGGER_URL")
    if not dsn:
        sys.exit("DATABASE_URL not set in environment (.env)")
    return dsn


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--verify", action="store_true",
                    help="report the table this migration manages (no changes)")
    args = ap.parse_args()
    dsn = get_dsn()

    if not args.apply:
        print("DRY RUN — would execute the following statements:\n")
        for stmt in DDL_STATEMENTS:
            print(stmt.strip())
            print("---")
        print(f"\n{len(DDL_STATEMENTS)} DDL statements. Re-run with --apply.")
        return

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        for stmt in DDL_STATEMENTS:
            cur.execute(stmt)
    print(f"Applied {len(DDL_STATEMENTS)} DDL statements.")


if __name__ == "__main__":
    main()
