#!/usr/bin/env python3
# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backfill session model + token usage for already-captured sessions.

Why this exists: neither the model nor token totals come from the hook stream
reliably (SessionStart often omits the model; no hook event carries tokens), so
older rows have sessions.model NULL and sessions.total_tokens NULL. Both are
recorded on every assistant line of the Claude Code transcript, which is still
on disk for most sessions. This script reads each session's root transcript
(`~/.claude/projects/<proj>/<session_id>.jsonl`) AND every sub-agent / workflow
transcript under `<proj>/<session_id>/subagents/`, recomputes model + token sums
via the shared scanner, and writes them to the session, its root invocation row,
and each matching sub-agent invocation row.

Sub-agents ARE recoverable after the fact: Claude Code writes each one to its own
`subagents/agent-<agent_id>.jsonl` (Workflow agents nested under
`subagents/workflows/wf_*/`), keyed by an agent_id that matches
`agent_invocations.agent_id`. The session total is computed authoritatively from
the FULL file set (root + all sub-agent files), so it also captures sub-agent /
workflow transcripts that never produced an invocation row. Reading only the root
file undercounted session totals ~3x — that is the bug this closes.

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
from cc_logger.transcripts import scan_session_totals  # noqa: E402


def get_dsn() -> str:
    dsn = os.getenv("DATABASE_URL") or os.getenv("NEON_CC_LOGGER_URL")
    if not dsn:
        sys.exit("DATABASE_URL not set in environment (.env)")
    return dsn


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
        sub_rows_set = sub_files_total = 0
        grew_tokens = 0  # sum of (new_total - old_total) across sessions
        for session_id, cur_model, cur_tokens in sessions:
            scan = scan_session_totals(session_id)
            root = scan["root"]
            if root is None:
                missing += 1
                continue
            scanned += 1
            if root["assistant_messages"] == 0:
                no_usage += 1
            root_id = f"root::{session_id}"
            totals = scan["totals"]          # root + ALL sub-agent / workflow files
            new_total = totals["total_tokens"]
            sub_files_total += scan["subagent_files"]

            if not args.apply:
                tag = []
                if root["model"] and root["model"] != cur_model:
                    tag.append(f"model:{root['model'].replace('claude-','')}")
                if new_total:
                    delta = new_total - (cur_tokens or 0)
                    tag.append(f"tok:{new_total:,}")
                    if delta:
                        tag.append(f"(+{delta:,} from {scan['subagent_files']} sub-agent files)")
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
                (root["model"], root["input_tokens"], root["output_tokens"],
                 root["cache_read_tokens"], root["cache_creation_tokens"],
                 root["total_tokens"], root_id),
            )
            # Each matching sub-agent invocation row gets its own model + tokens
            # (for the per-agent breakdown). Files with no row are still counted
            # in the session total below — they just have nowhere to attribute.
            for agent_id, st in scan["per_agent"].items():
                cur.execute(
                    """
                    UPDATE agent_invocations SET
                        model = COALESCE(%s, model),
                        input_tokens = %s, output_tokens = %s,
                        cache_read_tokens = %s, cache_creation_tokens = %s,
                        total_tokens = %s
                    WHERE invocation_id = %s
                    """,
                    (st["model"], st["input_tokens"], st["output_tokens"],
                     st["cache_read_tokens"], st["cache_creation_tokens"],
                     st["total_tokens"], agent_id),
                )
                if cur.rowcount:
                    sub_rows_set += 1
            # Session model (from the root transcript).
            if root["model"]:
                cur.execute(
                    "UPDATE sessions SET model = %s WHERE session_id = %s AND model IS DISTINCT FROM %s",
                    (root["model"], session_id, root["model"]),
                )
                if root["model"] != cur_model:
                    model_set += 1
            # Session token totals come from the AUTHORITATIVE full-file scan
            # (root + every sub-agent / workflow transcript), not a sum over
            # invocation rows — so unmatched workflow agents are still counted.
            cur.execute(
                """
                UPDATE sessions SET
                    input_tokens = %s, output_tokens = %s,
                    cache_read_tokens = %s, cache_creation_tokens = %s,
                    total_tokens = %s
                WHERE session_id = %s
                """,
                (totals["input_tokens"], totals["output_tokens"],
                 totals["cache_read_tokens"], totals["cache_creation_tokens"],
                 new_total, session_id),
            )
            if new_total:
                tokens_set += 1
                grew_tokens += new_total - (cur_tokens or 0)

    verb = "Would scan" if not args.apply else "Scanned"
    print(f"\n{verb} {scanned} sessions "
          f"({missing} had no transcript on disk, {no_usage} had no usage data); "
          f"read {sub_files_total} sub-agent / workflow transcripts.")
    if args.apply:
        print(f"Set/updated model on {model_set} sessions, tokens on {tokens_set} sessions, "
              f"{sub_rows_set} sub-agent invocation rows.")
        print(f"Session token totals grew by {grew_tokens:,} tokens "
              f"vs the old root-only figures.")
    else:
        print("Dry run — re-run with --apply to write.")


if __name__ == "__main__":
    main()
