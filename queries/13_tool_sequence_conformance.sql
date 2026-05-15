-- Process conformance check: are runs of the same agent doing the same thing?
--
-- For each session in the time window, this query computes the root agent's
-- tool-call sequence as a string (e.g. "Bash > Bash > WebSearch > Agent > Edit")
-- and groups identical sequences together. You see:
--   - The top-N modal sequences (the "common paths")
--   - The count of unique snowflakes (each only occurring once)
--   - For each modal sequence, an example session_id you can inspect
--
-- Two runs producing the same OUTPUT via different SEQUENCES = process drift.
-- That's the signal this query surfaces. The film-room view shows you what one
-- run did; this shows whether the same agent is doing the same thing across runs.
--
-- Notes:
--   - Only the root agent's tool calls are flattened here. An Agent (sub-agent
--     spawn) shows up as a single "Agent" token in the sequence, regardless of
--     how many tools the sub-agent itself used. To compare sub-agent internals,
--     swap `parent_invocation_id IS NULL` for a specific `agent_type` filter
--     and join on `ai.agent_type` to group runs of the same sub-agent kind.
--   - Default scope is all sessions in the last 60 days. Edit the WHERE
--     clauses in `runs` to narrow by initial_prompt pattern, time window, or
--     specific session_ids when you want to compare a specific agent.
--   - On a fresh DB you'll see every session as a snowflake. Drift detection
--     gets meaningful only after you've run the same agent multiple times.
--
-- Usage:
--   psql $DATABASE_URL -f queries/13_tool_sequence_conformance.sql

WITH runs AS (
  SELECT
    s.session_id,
    s.started_at,
    LEFT(COALESCE(s.initial_prompt, ''), 60) AS prompt_preview,
    string_agg(tc.tool_name, ' > ' ORDER BY tc.started_at) AS sequence,
    count(*) AS seq_len
  FROM sessions s
  JOIN agent_invocations ai
    ON ai.session_id = s.session_id
   AND ai.parent_invocation_id IS NULL
  JOIN tool_calls tc
    ON tc.invocation_id = ai.invocation_id
  WHERE s.started_at > now() - interval '60 days'
    -- Narrow scope: pick ONE of these and uncomment to compare a specific agent.
    -- AND s.initial_prompt LIKE 'sync new email data%'
    -- AND s.session_id IN ('aaa...', 'bbb...')
  GROUP BY 1, 2, 3
),
grouped AS (
  SELECT
    sequence,
    count(*)                                 AS runs_count,
    avg(seq_len)::int                        AS avg_len,
    array_agg(session_id ORDER BY started_at) AS example_sessions,
    (array_agg(prompt_preview ORDER BY started_at))[1] AS sample_prompt
  FROM runs
  GROUP BY sequence
),
totals AS (
  SELECT
    count(*)                                 AS distinct_sequences,
    sum(runs_count)                          AS total_runs,
    count(*) FILTER (WHERE runs_count = 1)   AS snowflakes
  FROM grouped
)
SELECT
  rank() OVER (ORDER BY runs_count DESC)     AS rk,
  runs_count                                 AS runs,
  round(100.0 * runs_count / (SELECT total_runs FROM totals), 1) AS pct,
  avg_len,
  CASE WHEN runs_count = 1 THEN 'snowflake' ELSE 'modal' END AS kind,
  sample_prompt,
  example_sessions[1]                        AS example_session,
  LEFT(sequence, 180) ||
    CASE WHEN length(sequence) > 180 THEN '...' ELSE '' END AS sequence_preview
FROM grouped
ORDER BY runs_count DESC, sequence
LIMIT 20;

-- Summary line (run after the main query):
--   WITH ... [same CTEs as above] ...
--   SELECT distinct_sequences, total_runs, snowflakes,
--          round(100.0 * snowflakes / total_runs, 1) AS snowflake_pct
--   FROM totals;
