-- Identify the first point where two runs of the same agent diverge, and
-- pull Claude's narration from both runs in a 60-second window around that
-- branching tool call. This is the diagnostic layer: query 14 shows you the
-- structural diff, this one tells you WHY the runs took different paths.
--
-- Output sections:
--   1) Branch summary: the position where sequences first differ, and what
--      each run did at that step.
--   2) Narration around the branch: assistant text blocks from both runs
--      whose timestamp falls within ±60s of the branch tool call.
--
-- Usage:
--   psql $DATABASE_URL \
--     -v sid1="'aaaaaaaa-...'" \
--     -v sid2="'bbbbbbbb-...'" \
--     -f queries/15_branching_points.sql

\echo
\echo '=== Branch summary ==='
WITH seq AS (
  SELECT s.session_id,
         row_number() OVER (PARTITION BY s.session_id ORDER BY tc.started_at) AS pos,
         tc.tool_name,
         tc.started_at,
         LEFT(COALESCE(
                tc.tool_input->>'command',
                tc.tool_input->>'description',
                tc.tool_input->>'query',
                tc.tool_input->>'url',
                tc.tool_input->>'file_path',
                tc.tool_input->>'subagent_type'), 80) AS hint
  FROM sessions s
  JOIN agent_invocations ai
    ON ai.session_id = s.session_id
   AND ai.parent_invocation_id IS NULL
  JOIN tool_calls tc
    ON tc.invocation_id = ai.invocation_id
  WHERE s.session_id IN (:sid1, :sid2)
),
paired AS (
  SELECT COALESCE(a.pos, b.pos) AS pos,
         a.tool_name AS tool_a, a.started_at AS ts_a, a.hint AS hint_a,
         b.tool_name AS tool_b, b.started_at AS ts_b, b.hint AS hint_b
  FROM (SELECT * FROM seq WHERE session_id = :sid1) a
  FULL OUTER JOIN (SELECT * FROM seq WHERE session_id = :sid2) b USING (pos)
),
branch AS (
  SELECT pos AS branch_pos, ts_a, ts_b, tool_a, hint_a, tool_b, hint_b
  FROM paired
  WHERE tool_a IS DISTINCT FROM tool_b
  ORDER BY pos
  LIMIT 1
)
SELECT
  branch_pos AS pos,
  tool_a     AS run_a_tool,
  hint_a     AS run_a_hint,
  tool_b     AS run_b_tool,
  hint_b     AS run_b_hint
FROM branch;

\echo
\echo '=== Narration around the branch (±60s) ==='
WITH seq AS (
  SELECT s.session_id,
         row_number() OVER (PARTITION BY s.session_id ORDER BY tc.started_at) AS pos,
         tc.tool_name,
         tc.started_at
  FROM sessions s
  JOIN agent_invocations ai
    ON ai.session_id = s.session_id
   AND ai.parent_invocation_id IS NULL
  JOIN tool_calls tc
    ON tc.invocation_id = ai.invocation_id
  WHERE s.session_id IN (:sid1, :sid2)
),
paired AS (
  SELECT COALESCE(a.pos, b.pos) AS pos,
         a.tool_name AS tool_a, a.started_at AS ts_a,
         b.tool_name AS tool_b, b.started_at AS ts_b
  FROM (SELECT * FROM seq WHERE session_id = :sid1) a
  FULL OUTER JOIN (SELECT * FROM seq WHERE session_id = :sid2) b USING (pos)
),
branch AS (
  SELECT ts_a, ts_b
  FROM paired
  WHERE tool_a IS DISTINCT FROM tool_b
  ORDER BY pos
  LIMIT 1
)
SELECT
  CASE WHEN m.session_id = :sid1 THEN 'RUN-A' ELSE 'RUN-B' END AS run,
  to_char(m.created_at, 'HH24:MI:SS')                          AS ts,
  LEFT(m.text, 220)                                            AS narration
FROM messages m, branch
WHERE m.session_id IN (:sid1, :sid2)
  AND (
    (m.session_id = :sid1 AND m.created_at BETWEEN branch.ts_a - interval '60 seconds'
                                                AND branch.ts_a + interval '60 seconds')
   OR
    (m.session_id = :sid2 AND m.created_at BETWEEN branch.ts_b - interval '60 seconds'
                                                AND branch.ts_b + interval '60 seconds')
  )
ORDER BY m.created_at;
