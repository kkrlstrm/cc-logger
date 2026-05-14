-- All sub-agents in a session, with parent linkage and durations.
-- Usage: psql $DATABASE_URL -v sid="'<session-id>'" -f queries/03_subagent_tree.sql
SELECT depth, agent_type, status,
       to_char(started_at, 'HH24:MI:SS') AS started,
       EXTRACT(EPOCH FROM (ended_at - started_at))::int AS dur_s,
       invocation_id, parent_invocation_id
FROM vw_subagent_tree
WHERE session_id = :sid
ORDER BY started_at;
