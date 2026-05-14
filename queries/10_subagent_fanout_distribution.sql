-- How often do you spawn sub-agents, and how wide do you fan out?
-- Most sessions are zero (tactical execution). The interesting tail is
-- 4+ sub-agents in a single session = deep research mode.
WITH per_session AS (
  SELECT s.session_id,
         (SELECT count(*) FROM agent_invocations ai
          WHERE ai.session_id = s.session_id AND ai.parent_invocation_id IS NOT NULL) AS sub_count
  FROM sessions s
  WHERE s.started_at > now() - interval '60 days'
)
SELECT sub_count,
       count(*) AS n_sessions,
       round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS pct_of_sessions
FROM per_session
GROUP BY 1
ORDER BY 1;
