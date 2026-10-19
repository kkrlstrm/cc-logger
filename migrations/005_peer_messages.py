#!/usr/bin/env python3
# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration 005: Cross-session messages.

Claude Code sessions on one machine can message each other (SendMessage /
ListAgents, see https://code.claude.com/docs/en/cross-session-messaging). The
SENDING half is a tool call, captured in `tool_calls`. The RECEIVING half fires
no hook at all; it exists only in the receiver's transcript, so it is recovered
by transcripts.ingest_peer_events on every Stop / SessionEnd.

Tables:
  peer_messages          one row per message, keyed by Claude Code's msg_id,
                         which is identical in the sender's tool result and the
                         receiver's `origin`. Each end fills its own columns, so
                         a row with both session ids is a matched pair.
  peer_notices           idle / delivery notices a session received (no msg_id).
  session_peer_addresses which session owned which inbox socket, and when.
                         Socket paths are `/tmp/cc-socks/<pid>.sock`, so a path
                         is reused once a pid is; resolve by time, never by
                         path alone. Filled by the SessionStart command hook
                         (source='hook') and by matched pairs (source='msg_id-join').

No foreign keys to `sessions`: the backfill covers transcripts whose sessions
were never logged, so their sessions rows do not exist.

Usage:
    python3 migrations/005_peer_messages.py            # dry-run
    python3 migrations/005_peer_messages.py --apply
    python3 migrations/005_peer_messages.py --verify
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
    CREATE TABLE IF NOT EXISTS peer_messages (
        msg_id              TEXT PRIMARY KEY,
        sender_session_id   TEXT,
        receiver_session_id TEXT,
        sent_at             TIMESTAMPTZ,
        received_at         TIMESTAMPTZ,
        addressed_to        TEXT,
        sender_address      TEXT,
        sender_name         TEXT,
        sender_mode         TEXT,
        delivery            TEXT,
        summary             TEXT,
        body                TEXT,
        notify_when_idle    BOOLEAN NOT NULL DEFAULT false,
        hop_chain           JSONB,
        send_tool_use_id    TEXT,
        send_result         TEXT,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_peer_messages_sender ON peer_messages (sender_session_id)",
    "CREATE INDEX IF NOT EXISTS idx_peer_messages_receiver ON peer_messages (receiver_session_id)",
    "CREATE INDEX IF NOT EXISTS idx_peer_messages_time ON peer_messages ((COALESCE(sent_at, received_at)))",
    """
    CREATE TABLE IF NOT EXISTS peer_notices (
        notice_id   TEXT PRIMARY KEY,
        session_id  TEXT NOT NULL,
        kind        TEXT NOT NULL,
        about_name  TEXT,
        text        TEXT NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_peer_notices_session ON peer_notices (session_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS session_peer_addresses (
        session_id     TEXT NOT NULL,
        address        TEXT NOT NULL,
        pid            INTEGER,
        source         TEXT NOT NULL,
        session_source TEXT,
        session_name   TEXT,
        recorded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (session_id, address)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_session_peer_addresses_addr ON session_peer_addresses (address, recorded_at)",
    # Sender resolution: the msg_id join when both transcripts were ingested,
    # else the session that most recently claimed the sender's socket before
    # the message was received.
    """
    CREATE OR REPLACE VIEW vw_peer_messages AS
    SELECT
        pm.msg_id,
        COALESCE(pm.sent_at, pm.received_at)                 AS at,
        COALESCE(pm.sender_session_id, addr.session_id)      AS sender_session_id,
        (pm.sender_session_id IS NULL AND addr.session_id IS NOT NULL) AS sender_inferred,
        pm.sender_name,
        pm.receiver_session_id,
        pm.addressed_to,
        pm.delivery,
        pm.notify_when_idle,
        pm.sender_session_id IS NOT NULL AND pm.receiver_session_id IS NOT NULL AS matched,
        s_from.cwd                                           AS sender_cwd,
        s_to.cwd                                             AS receiver_cwd,
        pm.summary,
        pm.body,
        length(pm.body)                                      AS body_chars
    FROM peer_messages pm
    LEFT JOIN LATERAL (
        SELECT a.session_id FROM session_peer_addresses a
        WHERE a.address = pm.sender_address
          AND a.recorded_at <= COALESCE(pm.received_at, pm.sent_at)
        ORDER BY a.recorded_at DESC LIMIT 1
    ) addr ON pm.sender_session_id IS NULL
    LEFT JOIN sessions s_from ON s_from.session_id = COALESCE(pm.sender_session_id, addr.session_id)
    LEFT JOIN sessions s_to   ON s_to.session_id = pm.receiver_session_id
    """,
]

TABLES = ("peer_messages", "peer_notices", "session_peer_addresses")


def get_dsn() -> str:
    dsn = os.getenv("DATABASE_URL") or os.getenv("NEON_CC_LOGGER_URL")
    if not dsn:
        sys.exit("DATABASE_URL not set in environment (.env)")
    return dsn


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--verify", action="store_true",
                    help="report the tables this migration manages (no changes)")
    args = ap.parse_args()
    dsn = get_dsn()

    if args.verify:
        with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
            for t in TABLES:
                cur.execute("SELECT to_regclass(%s)", (t,))
                present = cur.fetchone()[0] is not None
                count = None
                if present:
                    cur.execute(f"SELECT count(*) FROM {t}")
                    count = cur.fetchone()[0]
                print(f"{t}: {'present' if present else 'MISSING'}"
                      + (f" ({count} rows)" if present else ""))
        return

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
