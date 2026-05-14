-- Recent sessions with counts and duration.
-- Usage: psql $DATABASE_URL -f queries/01_session_summary.sql
SELECT session_id, started_at, duration_s, tool_count, subagent_count,
       fail_count, end_reason, prompt_preview
FROM vw_session_summary
ORDER BY started_at DESC
LIMIT 20;
