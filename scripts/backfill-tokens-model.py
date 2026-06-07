#!/usr/bin/env python3
# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backfill session model + token usage for already-captured sessions.

Why this exists: neither the model nor token totals come from the hook stream
reliably (SessionStart often omits the model; no hook event carries tokens), so
older rows have sessions.model NULL and sessions.total_tokens NULL. Both are
recorded on every assistant line of the Claude Code transcript, which is still
on disk for most sessions. This script reads each session's root transcript
(`~/.claude/projects/<proj>/<session_id>.jsonl`), recomputes model + token sums
via the same scanner the live pipeline now uses, and writes them to the
session and its root invocation row.

Scope / limitation: only the ROOT transcript is recoverable after the fact —
sub-agent transcripts live in separate files whose paths were never stored, so
historical sub-agent tokens cannot be backfilled. They populate going forward
via the fixed live ingest (SubagentStop). For sessions with heavy fan-out, the
backfilled total therefore reflects the root agent only; this is logged.

Usage:
    uv run python scripts/backfill-tokens-model.py            # dry-run (report only)
    uv run python scripts/backfill-tokens-model.py --apply    # write to the DB
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
import psycopg

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / ".env")
sys.path.insert(0, str(_ROOT / "src"))
from cc_logger.transcripts import scan_transcript_stats  # noqa: E402

PROJECTS_DIR = Path.home() / ".claude" / "projects"


def get_dsn() -> str:
    dsn = os.getenv("DATABASE_URL") or os.getenv("NEON_CC_LOGGER_URL")
    if not dsn:
        sys.exit("DATABASE_URL not set in environment (.env)")
    return dsn


def find_transcript(session_id: str) -> Path | None:
    """Locate <session_id>.jsonl under any project dir (root session transcript)."""
    hits = list(PROJECTS_DIR.glob(f"*/{session_id}.jsonl"))
    return hits[0] if hits else None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write to the DB (default: dry-run)")
    ap.add_argument("--limit", type=int, default=0, help="cap sessions processed (0 = all)")
    args = ap.parse_args()

    dsn = get_dsn()
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT session_id, model, total_tokens FROM sessions ORDER BY started_at DESC")
        sessions = cur.fetchall()
        if args.limit:
            sessions = sessions[: args.limit]

        scanned = model_set = tokens_set = missing = no_usage = 0
        for session_id, cur_model, cur_tokens in sessions:
            tp = find_transcript(session_id)
            if tp is None:
                missing += 1
                continue
            stats = scan_transcript_stats(tp)
            if stats is None:
                missing += 1
                continue
            scanned += 1
            if stats["assistant_messages"] == 0:
                no_usage += 1
            root_id = f"root::{session_id}"

            if not args.apply:
                tag = []
                if stats["model"] and stats["model"] != cur_model:
                    tag.append(f"model:{stats['model'].replace('claude-','')}")
                if stats["total_tokens"]:
                    tag.append(f"tok:{stats['total_tokens']:,}")
                print(f"  {session_id[:18]}  {' '.join(tag) or '(no change)'}")
                continue

            # Root invocation row gets model + tokens.
            cur.execute(
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
                 stats["total_tokens"], root_id),
            )
            # Session model (from the root transcript).
            if stats["model"]:
                cur.execute(
                    "UPDATE sessions SET model = %s WHERE session_id = %s AND model IS DISTINCT FROM %s",
                    (stats["model"], session_id, stats["model"]),
                )
                if stats["model"] != cur_model:
                    model_set += 1
            # Session token totals = sum across its invocations (root + any
            # sub-agents already captured live).
            cur.execute(
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
            if stats["total_tokens"]:
                tokens_set += 1

    verb = "Would scan" if not args.apply else "Scanned"
    print(f"\n{verb} {scanned} transcripts "
          f"({missing} sessions had no transcript on disk, {no_usage} had no usage data).")
    if args.apply:
        print(f"Set/updated model on {model_set} sessions, tokens on {tokens_set} sessions.")
    else:
        print("Dry run — re-run with --apply to write.")


if __name__ == "__main__":
    main()
