-- Fail % per tool name. Flags chronically unreliable tools / MCP servers.
-- Anything over 5% is worth a look; over 20% is a problem.
SELECT
  tool_name,
  count(*) FILTER (WHERE status = 'success')  AS ok,
  count(*) FILTER (WHERE status = 'failure')  AS fail,
  count(*) FILTER (WHERE status = 'pending')  AS pending,
  count(*) FILTER (WHERE status = 'orphaned') AS orphaned,
  count(*) AS total,
  round(100.0 * count(*) FILTER (WHERE status = 'failure') / NULLIF(count(*), 0), 1) AS fail_pct,
  round(avg(duration_ms) FILTER (WHERE status = 'failure'), 0) AS avg_fail_ms
FROM tool_calls
WHERE started_at > now() - interval '60 days'
GROUP BY 1
HAVING count(*) >= 5
ORDER BY fail_pct DESC NULLS LAST, total DESC;
