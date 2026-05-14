-- Tool calls that started but never received a PostToolUse hook.
-- Most often: long-running Bash commands that timed out or sessions that
-- crashed before completing. Useful for spotting reliability issues.
SELECT session_id, tool_name,
       to_char(started_at, 'YYYY-MM-DD HH24:MI:SS') AS started,
       tool_input
FROM tool_calls
WHERE status IN ('pending', 'orphaned')
  AND started_at > now() - interval '30 days'
ORDER BY started_at DESC;
