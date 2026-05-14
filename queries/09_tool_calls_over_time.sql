-- Tool call volume per day, broken out by tool category. Use this to spot
-- shifts in how you work (more research vs. more execution, etc.).
SELECT
  date_trunc('day', tc.started_at)::date AS day,
  count(*) FILTER (WHERE tc.tool_name = 'Bash') AS bash,
  count(*) FILTER (WHERE tc.tool_name IN ('WebSearch','WebFetch')) AS research,
  count(*) FILTER (WHERE tc.tool_name = 'Agent') AS agent_spawns,
  count(*) FILTER (WHERE tc.tool_name IN ('Edit','Write')) AS writes,
  count(*) FILTER (WHERE tc.tool_name LIKE 'mcp__%') AS mcp,
  count(*) AS total,
  count(distinct tc.session_id) AS sessions
FROM tool_calls tc
WHERE tc.started_at > now() - interval '60 days'
GROUP BY 1
ORDER BY 1;
