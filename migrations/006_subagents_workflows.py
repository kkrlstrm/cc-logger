#!/usr/bin/env python3
# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration 006: Sub-agent provenance + Workflow runs.

Three gaps:

  - `agent_invocations.prompt_received` was NULL on 100% of sub-agent rows: the
    SubagentStart payload carries no prompt. The sub-agent's own transcript opens
    with it, and Claude Code writes `agent-<id>.meta.json` beside it naming the
    agent type, description and the parent's Agent `toolUseId` -- an exact parent
    link where the hook-side subagent_type match leaves parallel fan-outs ambiguous.
  - Workflow runs were not recorded at all. The Workflow tool returns at launch
    ("async_launched", a runId); the run itself -- script, args, result, phases,
    agent count, tokens -- lands in `<session>/workflows/<runId>.json`.
  - Sub-agents whose hook events were never recorded (the logger was down, or
    its database refused writes) exist only on disk. scripts/backfill-subagents.py recovers them; `source` marks those rows.

Additive and idempotent.

Usage:
    python3 migrations/006_subagents_workflows.py            # dry-run
    python3 migrations/006_subagents_workflows.py --apply
    python3 migrations/006_subagents_workflows.py --verify
"""
import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
import psycopg

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


DDL_STATEMENTS = [
    # NULL source = written by the live hooks; 'backfill' = recovered from disk later.
    "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS source TEXT",
    "ALTER TABLE agent_invocations ADD COLUMN IF NOT EXISTS source TEXT",
    "ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS source TEXT",
    "ALTER TABLE agent_invocations ADD COLUMN IF NOT EXISTS description TEXT",
    "ALTER TABLE agent_invocations ADD COLUMN IF NOT EXISTS workflow_run_id TEXT",
    # meta.json `workflowPhase` (e.g. "Verify"); only newer workflow agents carry it.
    "ALTER TABLE agent_invocations ADD COLUMN IF NOT EXISTS workflow_phase TEXT",
    "CREATE INDEX IF NOT EXISTS idx_agent_invocations_workflow_run ON agent_invocations (workflow_run_id)",
    """
    CREATE TABLE IF NOT EXISTS workflow_runs (
        run_id            TEXT PRIMARY KEY,
        session_id        TEXT NOT NULL,
        task_id           TEXT,
        workflow_name     TEXT,
        summary           TEXT,
        status            TEXT,
        default_model     TEXT,
        started_at        TIMESTAMPTZ,
        recorded_at       TIMESTAMPTZ,
        duration_ms       BIGINT,
        agent_count       INTEGER,
        total_tokens      BIGINT,
        total_tool_calls  INTEGER,
        script            TEXT,
        script_path       TEXT,
        args              JSONB,
        result            JSONB,
        phases            JSONB,
        logs              JSONB,
        progress          JSONB,
        file_mtime        DOUBLE PRECISION,
        ingested_at       TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_workflow_runs_session ON workflow_runs (session_id)",
    "CREATE INDEX IF NOT EXISTS idx_workflow_runs_name ON workflow_runs (workflow_name, started_at)",
    # A run with its launching tool call and what its agents actually recorded.
    """
    CREATE OR REPLACE VIEW vw_workflow_runs AS
    SELECT
        w.run_id, w.session_id, w.workflow_name, w.status, w.started_at,
        w.duration_ms, w.agent_count AS agents_declared,
        a.agents_logged, a.agents_with_result, a.tokens_logged,
        w.total_tokens AS tokens_declared, w.total_tool_calls,
        t.tool_call_id AS launch_tool_call_id,
        w.args
    FROM workflow_runs w
    LEFT JOIN LATERAL (
        SELECT count(*) AS agents_logged,
               count(last_message) AS agents_with_result,
               sum(total_tokens) AS tokens_logged
        FROM agent_invocations ai WHERE ai.workflow_run_id = w.run_id
    ) a ON true
    LEFT JOIN LATERAL (
        SELECT tc.tool_call_id FROM tool_calls tc
        WHERE tc.session_id = w.session_id AND tc.tool_name = 'Workflow'
          AND tc.tool_response->>'runId' = w.run_id
        LIMIT 1
    ) t ON true
    """,
]

COLUMNS = {
    "sessions": {"source"},
    "agent_invocations": {"source", "description", "workflow_run_id", "workflow_phase"},
    "tool_calls": {"source"},
}


def get_dsn() -> str:
    dsn = os.getenv("DATABASE_URL") or os.getenv("NEON_CC_LOGGER_URL")
    if not dsn:
        sys.exit("DATABASE_URL not set in environment (.env)")
    return dsn


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--verify", action="store_true",
                    help="report the columns + table this migration manages (no changes)")
    args = ap.parse_args()
    dsn = get_dsn()

    if args.verify:
        with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
            for table, cols in COLUMNS.items():
                cur.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                    (table,))
                have = {r[0] for r in cur.fetchall()}
                missing = cols - have
                print(f"{table}: {'ok' if not missing else 'MISSING ' + ', '.join(sorted(missing))}")
            cur.execute("SELECT to_regclass('workflow_runs')")
            if cur.fetchone()[0] is None:
                print("workflow_runs: MISSING")
            else:
                cur.execute("SELECT count(*) FROM workflow_runs")
                print(f"workflow_runs: present ({cur.fetchone()[0]} rows)")
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
