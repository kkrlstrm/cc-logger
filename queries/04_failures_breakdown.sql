-- Recent failures broken down by tool and a hint at the cause.
-- "near_60s_timeout" suggests WebFetch hitting a stuck site.
SELECT tool_name,
       count(*) AS total_fails,
       count(*) FILTER (WHERE duration_ms BETWEEN 55000 AND 65000) AS near_60s_timeout,
       count(*) FILTER (WHERE duration_ms < 5000) AS quick_fails,
       round(avg(duration_ms)::numeric, 0) AS avg_ms
FROM tool_calls
WHERE status = 'failure'
  AND started_at > now() - interval '30 days'
GROUP BY 1
ORDER BY 2 DESC;
