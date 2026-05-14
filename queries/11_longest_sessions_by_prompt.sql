-- Which prompts produced the longest sessions? Sort by tool_count or
-- duration_s to see what kinds of asks drive the most work. Useful for
-- spotting prompts that should be broken down or that lit up unexpectedly.
SELECT
  session_id,
  duration_s,
  tool_count,
  subagent_count,
  fail_count,
  prompt_preview
FROM vw_session_summary
WHERE started_at > now() - interval '60 days'
  AND tool_count > 0
ORDER BY tool_count DESC NULLS LAST
LIMIT 25;
