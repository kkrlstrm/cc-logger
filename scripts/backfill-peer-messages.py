#!/usr/bin/env python3
# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backfill cross-session messages from every transcript on disk.

Live capture runs on Stop / SessionEnd (transcripts.ingest), so only sessions
that end a turn after migration 005 are covered automatically. This walks every
project folder under ~/.claude/projects -- root transcripts and sub-agent
transcripts (a sub-agent's send goes out under its parent session) -- and runs
the same ingest_peer_events, so the result is identical to live capture.

Dry-run parses and reports what it would write; --apply writes. Idempotent.

Usage:
    uv run python scripts/backfill-peer-messages.py            # dry-run
    uv run python scripts/backfill-peer-messages.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv
import psycopg

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")
sys.path.insert(0, str(_ROOT / "src"))
from cc_logger.transcripts import ingest_peer_events, iter_peer_events  # noqa: E402


def transcripts(projects_dir: Path):
    """Yield (session_id, path) for every root and sub-agent transcript."""
    for path in projects_dir.rglob("*.jsonl"):
        if "subagents" in path.parts:
            session_id = path.parts[path.parts.index("subagents") - 1]
        else:
            session_id = path.stem
        yield session_id, path


def has_peer_traffic(path: Path) -> bool:
    try:
        data = path.read_bytes()
    except OSError:
        return False
    return b'"peer"' in data or b"Cross-session" in data or b"msg_id" in data


async def run(projects_dir: Path, apply: bool) -> None:
    candidates = [(s, p) for s, p in transcripts(projects_dir) if has_peer_traffic(p)]
    per_kind: Counter = Counter()
    per_project: Counter = Counter()
    msg_ids: set[str] = set()
    for session_id, path in candidates:
        for ev in iter_peer_events(path):
            per_kind[ev["event"]] += 1
            per_project[path.relative_to(projects_dir).parts[0]] += 1
            if ev["event"] in ("sent", "received"):
                msg_ids.add(ev["msg_id"])
    print(f"scanned {len(candidates)} transcripts with candidate peer traffic")
    print(f"events: {dict(per_kind)}  distinct messages: {len(msg_ids)}")
    for proj, n in per_project.most_common():
        print(f"  {n:4d}  {proj}")
    if not apply:
        print("\nDRY RUN -- re-run with --apply to write.")
        return

    dsn = os.getenv("DATABASE_URL") or os.getenv("NEON_CC_LOGGER_URL")
    if not dsn:
        sys.exit("DATABASE_URL not set in environment (.env)")
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        for session_id, path in candidates:
            await ingest_peer_events(conn, session_id, path)
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*), count(*) FILTER (WHERE sender_session_id IS NOT NULL "
                "AND receiver_session_id IS NOT NULL) FROM peer_messages")
            total, matched = await cur.fetchone()
            await cur.execute("SELECT count(*) FROM peer_notices")
            (notices,) = await cur.fetchone()
            await cur.execute("SELECT count(*) FROM session_peer_addresses")
            (addresses,) = await cur.fetchone()
    print(f"\nwritten -- peer_messages: {total} ({matched} matched both ends), "
          f"peer_notices: {notices}, session_peer_addresses: {addresses}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write to the DB (default: dry-run)")
    ap.add_argument("--projects-dir", type=Path, default=Path.home() / ".claude" / "projects")
    args = ap.parse_args()
    asyncio.run(run(args.projects_dir, args.apply))


if __name__ == "__main__":
    main()
