# cc-logger

**Agent QA for Claude Code.** Replay sessions like game film, trace every sub-agent and tool call, and compare repeated runs to catch workflow drift. Captures every prompt, sub-agent, tool call, and Claude's narration between them into Postgres so you can see how the work actually happened, not just what came out.

Two agent runs can produce identical-looking outputs while one took the happy path and the other recovered from three failed WebFetches, fell back to a different source, and got lucky. The outputs look the same. The processes are not. This is the tool that catches that.

```bash
cc-logger sessions                                              # what ran recently
cc-logger inspect <session-id>                                  # one session, full tree
cc-logger insights --days 7                                     # cross-session patterns
psql $DATABASE_URL -f queries/13_tool_sequence_conformance.sql  # drift across runs
```

**Who this is for:**

- **Solo Claude Code users** who want to review their sessions like game film.
- **Teams and agencies** running the same agent repeatedly across clients or projects, who need to know whether each run is following the canonical path or drifting.
- **Operators of production agent systems** who want tool-level traces and the ability to compare behavior across runs.

Built on [Claude Code's HTTP hooks](https://docs.claude.com/en/docs/claude-code/hooks) — no patches or modifications to Claude Code itself.

## What you get

Run a Claude Code session, then `cc-logger inspect <session-id>` shows you the full tree of what happened:

```
SESSION 94b8ee2b-b51f-4125-a116-82adaf4066af
  started 2026-05-13 11:55  ended 2026-05-13 20:08  (8h 13m)
  end_reason: exit
  prompt: 'Look into our top accounts and brainstorm a summer outreach strategy.
           Map district fiscal sustainability against state academic calendars
           and propose which personas are reachable in each week of June-August.'

  [root  completed]
    · 'I'll start by pulling the campaign performance and matching it against
       district fiscal data, then fan out to verify against state sources.'
    Bash       'glab api "groups/12345/projects?search=accounts"'              3.0s  ok
    Bash       'psql ... -c "SELECT campaign, replies FROM eb_campaigns ..."'  2.0s  ok
    Bash       'ls /Users/me/Downloads/k12-district-fiscal-sustainability'      0ms  ok
    · 'Got the baseline. Now I'll spawn three sub-agents in parallel: one for
       NY state data, one for Ohio, one for the cross-state academic calendar.'
    Agent      'Map district fiscal sustainability data'                       38s   ok
      [general-purpose ab72bd109071caf  completed]
        · 'Searching state Comptroller / Auditor databases for fiscal-stress designations.'
        WebSearch  'K-12 district fiscal stress New York Comptroller 2024 2025'  5.9s  ok
        WebFetch   'https://www.osc.ny.gov/state-agencies/audits/fiscal-stress'  8.2s  ok
        WebSearch  'Ohio Auditor school district fiscal distress 2024'          6.1s  ok
        WebFetch   'https://ohioauditor.gov/auditsearch/Reports/2024'           64s   FAIL
        · 'Ohio Auditor blocked the fetch. Falling back to Comptroller summary.'
        ... 47 more tool calls
        → 'Found 31 districts in NY designated fiscal stress, 12 in OH...'
    Agent      'Cross-reference academic calendars by state'                  5m 55s ok
      [general-purpose ab9dcc9453675d9  completed]
        ... 64 tool calls
    ... 7 more sub-agents

  10 invocations (9 sub-agents), 556 tool calls (24 failed, 0 pending), 142 text blocks
```

`cc-logger insights` adds the cross-session view — power-law distribution of where your time goes, top failure domains, sub-agent fan-out patterns, hourly activity.

## Comparing runs: the conformance loop

The film-room mode is retrospective — watch one session, learn from it, move on. The other mode is **conformance testing**: when you run the same agent repeatedly, are the runs actually doing the same thing?

Two runs of the same agent can produce identical-looking output where one took the happy path and the other recovered from three WebFetch failures, fell back to a different source, and got lucky. The outputs look the same. The processes are not. Conformance testing catches that.

Three queries make up the loop:

```bash
# 1. Find drift across many runs of similar work
psql $DATABASE_URL -f queries/13_tool_sequence_conformance.sql

# 2. Diff two specific runs side by side
psql $DATABASE_URL \
  -v sid1="'<session-a>'" -v sid2="'<session-b>'" \
  -f queries/14_compare_two_runs.sql

# 3. Find the branching point + Claude's narration there
psql $DATABASE_URL \
  -v sid1="'<session-a>'" -v sid2="'<session-b>'" \
  -f queries/15_branching_points.sql
```

Real example from two runs of the same brief-generating agent (35 min / 19 tools vs. 6 min / 13 tools):

```
=== Branch summary ===
pos | run_a_tool | run_a_hint                    | run_b_tool | run_b_hint
  5 | Bash       | ls .../runs/latest-brief.md   | Write      | .../runs/latest-brief.md

=== Narration around the branch (±60s) ===
RUN-A | 07:15 | "Collection complete. Now I'll read the context and synthesize..."
RUN-A | 07:16 | "Now I have full context. Let me synthesize and send."
RUN-A | 07:17 | "Brief is 959 words; hard cap is 800. Trimming."
RUN-B | 07:52 | "919 words — need to trim under 800. Tightening."
```

Run A did an extra precautionary `ls` before writing, then spent two minutes on context-reading narration before the first word-count check. Run B was more direct. Same agent, same data, same output — different process. Four rows of diagnostic.

This is the layer that turns a logger into agent QA. If you run the same agent across many clients, projects, or days, query 13 tells you which runs are doing it the canonical way and which are snowflakes. Queries 14 + 15 tell you what changed and why.

## Quickstart (Docker Compose)

```bash
git clone https://github.com/kkrlstrm/cc-logger.git
cd cc-logger
cp .env.example .env                # defaults work for local Docker setup
docker compose up -d                # boots Postgres + cc-logger

# Migrate schema — tables + analytics views + narration table (all of it)
docker compose exec cc-logger python -m cc_logger.cli migrate --apply

# Wire Claude Code hooks (merges into ~/.claude/settings.json with a backup)
python scripts/install-hooks.py

# Run a Claude Code session, then check what's been captured:
docker compose exec cc-logger python -m cc_logger.cli sessions
```

## Alternative: run natively (no Docker)

```bash
uv sync
# Put a Postgres connection string in .env (Neon, Supabase, RDS, local install — anything)
uv run cc-logger migrate --apply     # applies all migrations: tables, views, narration table
./scripts/install.sh                # installs launchd (macOS) or systemd (Linux) auto-start
python scripts/install-hooks.py     # wires the Claude Code hooks
```

## What it captures

- Every Claude Code session (start, end, initial prompt, end reason)
- Every sub-agent invocation (root + children, with linkage to the spawning `Agent` tool call)
- Every tool call in the capture allowlist (Agent, Bash, Edit, Write, WebFetch, WebSearch, and `mcp__.*`)
- Tool input + tool response payloads as JSONB; anything >50KB spills to a separate `artifacts` table
- **Claude's text narration between tool calls** — read from the Claude Code transcript file at every `Stop` / `SubagentStop`, stored in the `messages` table. (Extended `thinking` blocks are encrypted by Anthropic — only `text` blocks are capturable.)
- Optional regex redaction of common secret patterns before write (on by default)

## Privacy

By default, common secret patterns (API keys, bearer tokens, passwords in connection strings) are redacted before any payload is written to Postgres. Known gaps are tracked as `xfail` tests in [`tests/test_redaction_known_gaps.py`](tests/test_redaction_known_gaps.py). Disable with `REDACT_SECRETS=0` if you want raw capture. Full story in [`docs/PRIVACY.md`](docs/PRIVACY.md).

## Tool capture allowlist

Captured: `Agent`, `Bash`, `Edit`, `Write`, `WebFetch`, `WebSearch`, `mcp__.*`
Skipped: `Read`, `Glob`, `Grep`, `TodoWrite` (to keep volume sane).

## CLI

```bash
cc-logger sessions [--limit N]              # list recent sessions
cc-logger inspect <session-id>              # render the session tree
cc-logger insights [--days N]               # cross-session analytics
```

## Canned queries

Fifteen ready-to-run SQL files in [`queries/`](queries/).

**The conformance loop** (the differentiated value — see "Comparing runs" above):
- `13_tool_sequence_conformance.sql` — groups sessions by their root-agent tool sequence to surface drift across repeat runs of the same agent. Modal paths vs. snowflakes.
- `14_compare_two_runs.sql` — side-by-side diff of two specific sessions, with a `match` column flagging where they did the same thing vs. where they diverged.
- `15_branching_points.sql` — for two sessions, finds the first position where their tool sequences differ, and pulls Claude's narration (±60s) around that branch.

**Single-session and aggregate analytics:**
- `01_session_summary.sql` — recent sessions with counts and duration
- `02_tool_usage.sql` — tool mix and reliability (last 24h)
- `03_subagent_tree.sql` — sub-agent tree for a session
- `04_failures_breakdown.sql` — what's timing out vs. failing fast
- `05_hourly_activity.sql` — when you actually work
- `06_repeat_fail_domains.sql` — WebFetch hosts to avoid
- `07_slowest_subagents.sql` — outlier deep dives
- `08_orphaned_calls.sql` — Bash calls that never reported back
- `09_tool_calls_over_time.sql` — tool volume by day
- `10_subagent_fanout_distribution.sql` — how often you fan out, and how wide
- `11_longest_sessions_by_prompt.sql` — which prompts produced the longest sessions
- `12_error_rates_by_tool.sql` — fail % per tool name

## Schema

Five tables: `sessions`, `agent_invocations`, `tool_calls`, `artifacts`, `messages`. Full reference in [`docs/SCHEMA.md`](docs/SCHEMA.md).

## License

MIT. See [LICENSE](LICENSE).
