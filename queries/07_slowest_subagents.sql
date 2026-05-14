-- Slowest sub-agents across the last 30 days. Useful for spotting the
-- "this Explore took 9 minutes" outliers.
SELECT
  ai.session_id,
  ai.agent_type,
  to_char(ai.started_at, 'YYYY-MM-DD HH24:MI') AS started,
  EXTRACT(EPOCH FROM (ai.ended_at - ai.started_at))::int AS dur_s,
  (SELECT count(*) FROM tool_calls tc WHERE tc.invocation_id = ai.invocation_id) AS tools_used,
  (SELECT count(*) FROM tool_calls tc WHERE tc.invocation_id = ai.invocation_id AND tc.status='failure') AS tool_fails
FROM agent_invocations ai
WHERE ai.parent_invocation_id IS NOT NULL
  AND ai.ended_at IS NOT NULL
  AND ai.started_at > now() - interval '30 days'
ORDER BY dur_s DESC
LIMIT 15;
