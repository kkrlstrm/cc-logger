-- Which context does your agent actually load — and which never gets loaded at all?
--
-- Every setup accumulates context: skill/command definitions, instruction files
-- (CLAUDE.md, AGENTS.md), memory or notes directories, docs an agent is "supposed" to
-- consult. Almost nobody knows which of it is ever read. It costs tokens to exist and
-- it dilutes the parts that matter, so the unread half is a real, invisible tax.
--
-- This query answers it from `Read` and `Skill` tool calls (captured since cc-logger
-- added them to the allowlist — see docs/HOOKS.md). If your DB predates that change
-- these results will be empty; that absence means "not recorded", not "not loaded".
--
--   psql "$DATABASE_URL" -f queries/17_context_usage.sql
--
-- Reading it:
--   sessions       distinct runs that loaded this artifact — the usage signal
--   reads          total loads (reads >> sessions means it's re-read within a run,
--                  which usually means it's too long or in the wrong place)
--   last_used      recency; a once-important doc that stopped being read is a signal
--   kind           bucketed by path so skills/instructions/memory are comparable
--
-- The actionable half is what is NOT here. To find that, list your context files on
-- disk and diff them against this result — anything absent was never loaded in the
-- window. Two caveats before you delete anything:
--   1. A file consumed by a *script* that globs a directory never appears as a Read.
--   2. An artifact created inside the window hasn't had a chance to be used.
--
-- Adjust the interval and the `kind` CASE to match your own layout.

WITH reads AS (
  SELECT
    tc.session_id,
    tc.tool_input ->> 'file_path' AS path,
    tc.started_at
  FROM tool_calls tc
  WHERE tc.tool_name = 'Read'
    AND tc.started_at > now() - interval '90 days'
    AND tc.tool_input ? 'file_path'
),
skills AS (
  -- A Skill invocation is a context load too: it pulls that skill's whole definition
  -- into the window. Counted alongside reads so both are visible in one place.
  SELECT
    tc.session_id,
    'skill:' || coalesce(tc.tool_input ->> 'skill', tc.tool_input ->> 'name') AS path,
    tc.started_at
  FROM tool_calls tc
  WHERE tc.tool_name = 'Skill'
    AND tc.started_at > now() - interval '90 days'
    AND coalesce(tc.tool_input ->> 'skill', tc.tool_input ->> 'name') IS NOT NULL
),
loads AS (
  SELECT * FROM reads
  UNION ALL
  SELECT * FROM skills
)
SELECT
  CASE
    WHEN path LIKE 'skill:%'                          THEN 'skill (invoked)'
    WHEN path LIKE '%/SKILL.md'                       THEN 'skill (definition)'
    WHEN path LIKE '%/.claude/commands/%'             THEN 'command'
    WHEN path LIKE '%/.claude/agents/%'               THEN 'subagent'
    WHEN path ~ '(CLAUDE|AGENTS|README|REVIEW)\.md$'  THEN 'instructions'
    WHEN path LIKE '%/memory/%'                       THEN 'memory'
    WHEN path LIKE '%/docs/%'                         THEN 'docs'
    ELSE 'other'
  END                                    AS kind,
  path,
  count(DISTINCT session_id)             AS sessions,
  count(*)                               AS reads,
  round(count(*)::numeric
        / nullif(count(DISTINCT session_id), 0), 1) AS reads_per_session,
  max(started_at)::date                  AS last_used
FROM loads
WHERE path IS NOT NULL
GROUP BY 1, 2
ORDER BY sessions DESC, reads DESC
LIMIT 60;
