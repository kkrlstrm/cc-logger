-- Side-by-side comparison of two runs of the same (or similar) agent.
-- Shows each tool call in both sessions at the same position, plus a `match`
-- column flagging whether the runs did the same thing at that step.
--
-- Pair with queries/13_tool_sequence_conformance.sql (which surfaces drift
-- across many runs) and queries/15_branching_points.sql (which zeroes in on
-- the first divergence and pulls Claude's narration around it).
--
-- Usage:
--   psql $DATABASE_URL \
--     -v sid1="'aaaaaaaa-...'" \
--     -v sid2="'bbbbbbbb-...'" \
--     -f queries/14_compare_two_runs.sql
--
-- Notes:
--   - Only root-agent tool calls are compared. Sub-agent fan-outs collapse to
--     a single `Agent` token; to diff sub-agent internals, swap the
--     parent_invocation_id IS NULL clause for a specific agent_id pair.
--   - "match" is true when both runs had the same tool_name at the same
--     positional index, regardless of input arguments.

WITH seq AS (
  SELECT s.session_id,
         row_number() OVER (PARTITION BY s.session_id ORDER BY tc.started_at) AS pos,
         tc.tool_name,
         tc.status,
         tc.duration_ms,
         tc.started_at,
         LEFT(COALESCE(
                tc.tool_input->>'command',
                tc.tool_input->>'description',
                tc.tool_input->>'query',
                tc.tool_input->>'url',
                tc.tool_input->>'file_path',
                tc.tool_input->>'subagent_type'), 60) AS hint
  FROM sessions s
  JOIN agent_invocations ai
    ON ai.session_id = s.session_id
   AND ai.parent_invocation_id IS NULL
  JOIN tool_calls tc
    ON tc.invocation_id = ai.invocation_id
  WHERE s.session_id IN (:sid1, :sid2)
),
a AS (SELECT * FROM seq WHERE session_id = :sid1),
b AS (SELECT * FROM seq WHERE session_id = :sid2)
SELECT
  COALESCE(a.pos, b.pos)                                        AS pos,
  CASE
    WHEN a.tool_name IS NOT DISTINCT FROM b.tool_name THEN '='
    WHEN a.tool_name IS NULL                          THEN '> '
    WHEN b.tool_name IS NULL                          THEN '<'
    ELSE                                                   '!'
  END                                                           AS match,
  a.tool_name || COALESCE(' ' || NULLIF(a.status, 'success'), '') AS run_a_tool,
  a.hint                                                        AS run_a_hint,
  b.tool_name || COALESCE(' ' || NULLIF(b.status, 'success'), '') AS run_b_tool,
  b.hint                                                        AS run_b_hint
FROM a
FULL OUTER JOIN b USING (pos)
ORDER BY pos;

-- match column legend:
--   =   both runs ran the same tool_name at this position
--   !   tool_name differs between runs (the branching candidate)
--   <   run A had no more tools at this position (run B is longer)
--   >   run B had no more tools at this position (run A is longer)
