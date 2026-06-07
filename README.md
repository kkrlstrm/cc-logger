# cc-logger

**Agent QA infrastructure for Claude Code workflows.** Replay, inspect, and compare agent runs so repeated workflows don't silently drift. cc-logger captures every prompt, sub-agent, tool call, and Claude's narration in between into Postgres — so you can see how the work actually happened, not just what came out, and turn your own usage into data you can optimize against.

Two runs of the same workflow can produce identical-looking output while one took the happy path and the other recovered from three failed WebFetches, fell back to a different source, and got lucky. The outputs match. The processes don't. cc-logger is the layer that catches that.

```bash
cc-logger sessions                                              # what ran recently
cc-logger inspect <session-id>                                  # one session, full tree
cc-logger insights --days 7                                     # cross-session patterns
psql $DATABASE_URL -f queries/13_tool_sequence_conformance.sql  # drift across runs
```

**What it's good for:**

- **See what you actually do, not what you think you do.** Your repo shows what's *possible*; your logs show your real workload — which tools, how often, where the time and the failures go. That's the surface you optimize against.
- **Catch workflow drift.** Run the same agent across many clients or days and know which runs took the canonical path and which are snowflakes — before the snowflake turns out to be the one that mattered.
- **Turn sub-agent runs into specs.** Every sub-agent fan-out is recorded (most setups drop these entirely). The worker prompt you keep re-typing is, almost verbatim, the definition for a reusable sub-agent.
- **Compare models on real work.** Every run is tagged with the model that ran it, so you can slice one model version against another on the same kind of task instead of trusting a single impressive session.
- **Decide what to make deterministic.** The same API/DB/parsing glue, re-improvised every session, shows up as a pattern — the signal that it should be a script, not an LLM call.

**Who this is for:**

- **Solo Claude Code users** reviewing their own sessions like game film.
- **Teams and agencies** running the same agent across clients or projects, who need to know whether each run is following the canonical path or drifting.
- **Operators of production agent systems** who want tool-level traces and the ability to compare behavior across runs.

Built on [Claude Code's HTTP hooks](https://docs.claude.com/en/docs/claude-code/hooks) — no patches or modifications to Claude Code itself.

## What you get

Run a Claude Code session, then `cc-logger inspect <session-id>` shows you the full tree of what happened:

```
SESSION 94b8ee2b-b51f-4125-a116-82adaf4066af
  started 2026-05-13 11:55  ended 2026-05-13 20:08  (8h 13m)
  model: claude-opus-4-8
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

The film-room mode is retrospective — watch one session, learn from it, move on. The other mode is **conformance testing**: when you run the same agent repeatedly, are the runs actually doing the same thing? Same input, same output, different process is exactly the failure that ships unnoticed — three queries make the process visible:

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
- **The model and token usage that actually ran** — per session and per invocation, summed from the transcript (`message.model` + `message.usage`). The hook stream can't supply these: SessionStart often omits the model and no hook event carries token totals, so cc-logger reads them from the transcript it's already parsing for narration.
- Optional regex redaction of common secret patterns before write (on by default)

Token usage and model are recovered from the transcript, so they work even though the hooks don't report them. To repopulate sessions captured before this existed, run `python scripts/backfill-tokens-model.py --apply`.

## Privacy

By default, common secret patterns (API keys, bearer tokens, passwords in connection strings) are redacted before any payload is written to Postgres. Known gaps are tracked as `xfail` tests in [`tests/test_redaction_known_gaps.py`](tests/test_redaction_known_gaps.py). Disable with `REDACT_SECRETS=0` if you want raw capture. Full story in [`docs/PRIVACY.md`](docs/PRIVACY.md).

## Tool capture allowlist

Captured: `Agent`, `Bash`, `Edit`, `Write`, `WebFetch`, `WebSearch`, `mcp__.*`
Skipped: `Read`, `Glob`, `Grep`, `TodoWrite` (to keep volume sane).

## CLI

```bash
cc-logger sessions [--limit N]              # list recent sessions (model, tokens, rating)
cc-logger inspect <session-id>              # render the session tree
cc-logger insights [--days N]               # cross-session analytics (incl. tokens by model)
cc-logger rate <session-id> <1-5> [--note "…"]   # attach a retrospective rating + note
```

`rate` accepts a unique session-id prefix. The rating and note land in
`sessions.self_rating` / `sessions.retro_note` and show up in `sessions`,
`insights`, and `vw_session_summary` — closing the film-room loop so a run you
learned something from is queryable later.

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

[GNU AGPL-3.0-or-later](LICENSE). cc-logger is free software: use, modify, and
self-host it freely. The copyleft terms mean that if you modify cc-logger and
make it available to others — including over a network (e.g. as a hosted
service) — you must release your modified source under the same license.

Copyright (C) 2026 Kai Karlstrom. For commercial licensing outside the AGPL
terms, open an issue to discuss.
