-- When during the day do you use Claude Code most?
SELECT EXTRACT(hour FROM started_at)::int AS hour,
       count(*) AS tool_calls,
       count(distinct session_id) AS sessions
FROM tool_calls
WHERE started_at > now() - interval '30 days'
GROUP BY 1
ORDER BY 1;
