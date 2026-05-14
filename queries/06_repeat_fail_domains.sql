-- WebFetch hosts that keep failing. Strong candidates for an avoid-list
-- or fallback-to-WebSearch strategy.
SELECT host, fails, avg_ms
FROM vw_repeat_fail_domains
LIMIT 15;
