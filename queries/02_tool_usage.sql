-- Tool mix and reliability over the last 24h. Lower fail% is better;
-- WebFetch hovering at 60s avg is a sign of timeout problems.
SELECT tool_name, ok, fail, pending, avg_ms, p90_ms,
       CASE WHEN (ok + fail) > 0
            THEN round(100.0 * fail / (ok + fail), 1)
            ELSE 0 END AS fail_pct
FROM vw_tool_usage_24h
ORDER BY total DESC;
