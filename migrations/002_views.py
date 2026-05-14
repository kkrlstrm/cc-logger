#!/usr/bin/env python3
"""Migration 002: Analytical views.

Creates Postgres views that make common questions one-line SELECTs instead
of joins/CTEs. Idempotent: CREATE OR REPLACE everywhere.

Usage:
    python3 migrations/002_views.py            # dry-run
    python3 migrations/002_views.py --apply
"""
import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
import psycopg

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


VIEWS = [
    # Per-session summary with counts and duration
    """
    CREATE OR REPLACE VIEW vw_session_summary AS
    SELECT
      s.session_id,
      s.started_at,
      s.ended_at,
      EXTRACT(EPOCH FROM (s.ended_at - s.started_at))::int AS duration_s,
      s.end_reason,
      s.cwd,
      s.model,
      LEFT(COALESCE(s.initial_prompt,''), 200) AS prompt_preview,
      s.self_rating,
      (SELECT count(*) FROM tool_calls tc WHERE tc.session_id = s.session_id) AS tool_count,
      (SELECT count(*) FROM tool_calls tc WHERE tc.session_id = s.session_id AND tc.status='failure') AS fail_count,
      (SELECT count(*) FROM agent_invocations ai WHERE ai.session_id = s.session_id) AS invocation_count,
      (SELECT count(*) FROM agent_invocations ai WHERE ai.session_id = s.session_id AND ai.parent_invocation_id IS NOT NULL) AS subagent_count
    FROM sessions s
    """,
    # Tool usage rolled up across the last 24h
    """
    CREATE OR REPLACE VIEW vw_tool_usage_24h AS
    SELECT
      tc.tool_name,
      count(*) FILTER (WHERE tc.status='success') AS ok,
      count(*) FILTER (WHERE tc.status='failure') AS fail,
      count(*) FILTER (WHERE tc.status='pending') AS pending,
      round(avg(tc.duration_ms)::numeric, 0) AS avg_ms,
      round((percentile_cont(0.9) WITHIN GROUP (ORDER BY tc.duration_ms))::numeric, 0) AS p90_ms,
      count(*) AS total
    FROM tool_calls tc
    WHERE tc.started_at > now() - interval '24 hours'
    GROUP BY tc.tool_name
    """,
    # Subagent tree flattened (recursive CTE materialized as a view)
    """
    CREATE OR REPLACE VIEW vw_subagent_tree AS
    WITH RECURSIVE tree AS (
      SELECT
        invocation_id, session_id, parent_invocation_id, agent_type,
        started_at, ended_at, status, 0 AS depth,
        invocation_id::text AS path
      FROM agent_invocations
      WHERE parent_invocation_id IS NULL
      UNION ALL
      SELECT
        ai.invocation_id, ai.session_id, ai.parent_invocation_id, ai.agent_type,
        ai.started_at, ai.ended_at, ai.status, t.depth + 1,
        t.path || ' > ' || ai.invocation_id::text
      FROM agent_invocations ai
      JOIN tree t ON ai.parent_invocation_id = t.invocation_id
    )
    SELECT * FROM tree
    """,
    # WebFetch hosts ranked by failure count
    """
    CREATE OR REPLACE VIEW vw_repeat_fail_domains AS
    SELECT
      split_part(replace(replace(tc.tool_input->>'url', 'https://', ''), 'http://', ''), '/', 1) AS host,
      count(*) AS fails,
      round(avg(tc.duration_ms)::numeric, 0) AS avg_ms
    FROM tool_calls tc
    WHERE tc.tool_name = 'WebFetch' AND tc.status = 'failure'
    GROUP BY 1
    ORDER BY 2 DESC
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
    args = ap.parse_args()
    dsn = get_dsn()

    if not args.apply:
        print("DRY RUN — would execute the following statements:\n")
        for stmt in VIEWS:
            print(stmt.strip())
            print("---")
        print(f"\n{len(VIEWS)} view DDL statements. Re-run with --apply to execute.")
        return

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        for stmt in VIEWS:
            cur.execute(stmt)
    print(f"Applied {len(VIEWS)} view DDL statements.")


if __name__ == "__main__":
    main()
