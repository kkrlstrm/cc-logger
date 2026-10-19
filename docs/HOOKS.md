# Claude Code hooks reference

cc-logger registers HTTP hooks for 8 Claude Code event types in `~/.claude/settings.json`. This doc explains what each event is and what cc-logger extracts from it.

Official Claude Code hooks documentation: https://code.claude.com/docs/en/hooks.md

## Events captured

| Event | When it fires | What we extract |
|---|---|---|
| `SessionStart` | Claude Code session starts (fresh launch, resume, or `/clear`). | Inserts a `sessions` row with `session_id`, `cwd`, `model`. |
| `UserPromptSubmit` | User submits a prompt. | First prompt populates `sessions.initial_prompt`. Always ensures the root `agent_invocations` row exists. |
| `PreToolUse` | Before a tool runs. **Filtered** — see allowlist below. | Inserts a `tool_calls` row with `status='pending'` and the input payload. If `tool_name='Agent'`, also captures `subagent_type` for later linking. |
| `PostToolUse` | After a tool succeeds. **Filtered**. | Updates the matching `tool_calls` row with the response, `status='success'`, and duration. Spills payloads >50KB to `artifacts`. |
| `PostToolUseFailure` | When a tool fails. **Filtered**. | Updates the matching `tool_calls` row with `error`, `status='failure'`, duration. |
| `SubagentStart` | Sub-agent spawned. | Inserts an `agent_invocations` row. Resolves the parent `Agent` tool_call by `subagent_type` match. |
| `SubagentStop` | Sub-agent finishes. | Updates the matching `agent_invocations` row with `last_message`, `ended_at`, `status='completed'`. **Also reads the sub-agent's transcript file and ingests `text` blocks into the `messages` table.** |
| `Stop` | Root agent finishes a turn (one response to one user message). | Reads the root transcript file at `transcript_path`, extracts assistant `text` blocks, INSERTs them into `messages` (idempotent on `message_id` + `block_index`). This is where Claude's mid-process narration lands. |
| `SessionEnd` | Session ends (exit, logout, kill, etc.). | Updates `sessions.ended_at` + `end_reason`. Sweeps any still-pending `tool_calls` and `agent_invocations` for this session to `orphaned`. Also does a final transcript ingestion pass to catch anything `Stop` missed. |

## Tool capture allowlist

`PreToolUse` / `PostToolUse` / `PostToolUseFailure` are **filtered by `matcher`** in the settings.json block. Only these tool names are captured:

- `Agent` (sub-agent spawning)
- `Bash`
- `Edit`
- `Write`
- `WebFetch`
- `WebSearch`
- `Read` — **attribution**: `tool_input.file_path` is the context artifact loaded
- `Skill` — **attribution**: which skill a run actually invoked
- `SendMessage`, `ListAgents` — cross-session messaging (sending half; see "Cross-session messages" below)
- `Workflow` — fan-out launches; the response's `runId` joins to `workflow_runs` (see "Sub-agents and workflows" below)
- `mcp__.*` (any MCP server tool, regex)

**Intentionally skipped**: `Glob`, `Grep`, `TodoWrite`, `NotebookEdit`. High-volume, and `Glob`/`Grep` name a *pattern* rather than a specific artifact, so they can't be attributed to a piece of context.

`Read` and `Skill` were skipped originally for volume, then added back: they are the only record of **which context was actually loaded** into a run. Without them you can see what an agent did but never which instruction, memory, or skill it did it from — so "which of these skills is ever used?" and "does loading a given doc change how a run goes?" have no data behind them. See [`scripts/knowledge-effect.py`](../../scripts/knowledge-effect.py) in the parent repo for the report built on this.

If you want to change what's captured, edit the `matcher` lines in `~/.claude/settings.json` (under each tool event) AND the `CAPTURE_TOOLS` set in [`src/cc_logger/filters.py`](../src/cc_logger/filters.py).

## Sub-agent linking

Claude Code does **not** provide a `parent_session_id` or `parent_tool_call_id` field on `SubagentStart`. Linkage is implicit: parent and child share `session_id`, child gets its own `agent_id`.

cc-logger resolves the link by matching on `subagent_type`:

1. On `PreToolUse` for `tool_name='Agent'`, we record the `tool_input.subagent_type` in the `tool_calls.subagent_type` column.
2. On `SubagentStart`, we query for pending `Agent` `tool_calls` in the same session with matching `subagent_type`.
3. **One match** → `spawned_by_tool_call_id` and `parent_invocation_id` are populated cleanly.
4. **Multiple matches** (parallel fan-out of identical sub-agents) → we store all candidate IDs in `candidate_parent_tool_call_ids` JSONB and leave the direct fields NULL. You can resolve offline by ordering `SubagentStop` and `PostToolUse` timestamps.
5. **Zero matches** → both fields stay NULL (logged as a warning).

In practice, Claude Code emits hook events sequentially even when sub-agents execute in parallel, so the multi-candidate case is rare.

## Transcript-based message capture

Hooks don't include Claude's narration text in their payloads — they only fire at action boundaries. To capture the *decisions* Claude is making mid-process (e.g., "I'll start by exploring the project structure..."), cc-logger reads the Claude Code JSONL transcript file at `transcript_path` (a field present in every hook event).

- On `Stop` / `SubagentStop`: incremental ingest, near-realtime
- On `SessionEnd`: final reconciliation pass

Only `text` blocks are extracted. Claude's `thinking` (extended thinking) blocks are encrypted in the transcript by Anthropic — only a `signature` is present, no plaintext reasoning. This is an API-level choice and not something cc-logger can work around.

Insertion is idempotent (`ON CONFLICT (message_id, block_index) DO NOTHING`), so repeated reads of the same transcript are safe.

## Hook payload notes

Every hook event includes a common envelope:
- `hook_event_name`, `session_id`, `transcript_path`, `cwd`, `permission_mode`, `effort` (may be a dict like `{"level": "xhigh"}`), `agent_id`, `agent_type`

Models in [`src/cc_logger/models.py`](../src/cc_logger/models.py) use `extra="allow"` so new Claude Code fields don't break the worker — they're just stored in the JSONB columns.

## Disabling capture

To pause without uninstalling: stop the launchd/systemd service. Claude Code's hook timeout is 5 seconds; with cc-logger down it logs the failure and continues.

To uninstall fully: `python scripts/install-hooks.py --uninstall` removes the hook entries from settings.json, and `./scripts/uninstall.sh` removes the daemon unit file.

## Cross-session messages

Claude Code sessions on one machine can message each other with `SendMessage`
([docs](https://code.claude.com/docs/en/cross-session-messaging)). cc-logger records three
things, in three different ways, because no single hook sees all of it:

| What | How it is captured | Where it lands |
|---|---|---|
| The send | `SendMessage` / `ListAgents` in the tool matchers | `tool_calls` |
| The send + the receipt, joined | Parsed from each transcript on `Stop` / `SessionEnd` (`transcripts.iter_peer_events`). No hook fires when a message arrives. | `peer_messages` (one row per `msg_id`; the id is the same in the sender's tool result and the receiver's `origin`), `peer_notices` |
| Which session owns which inbox socket | A SessionStart **command** hook, `scripts/peer-address-hook.py`. The socket is only exported as `CLAUDE_CODE_MESSAGING_SOCKET`; an HTTP hook on SessionStart did not fire in a headless `claude -p` test (2026-10-13). | `session_peer_addresses` |

Read it through `vw_peer_messages`. A receipt names its sender only by socket
(`uds:/tmp/cc-socks/<pid>.sock`); when the sender's transcript was not ingested, the view
resolves the sender from `session_peer_addresses` by time (`sender_inferred = true`), because
a socket path is reused once its pid is.

Two record shapes hold a received message: a meta `user` record with top-level `origin`
(it arrived while the session was idle and started a turn) and an `attachment` of type
`queued_command` with `attachment.origin` (it arrived mid-turn). Sub-agent hand-backs and a
background sub-agent messaging its parent also carry `origin.kind == "peer"`; they have
`senderTaskId` and are excluded.

Backfill everything on disk: `uv run python scripts/backfill-peer-messages.py --apply`.

## Sub-agents and workflows

Three things the hook payloads do not carry, filled from files Claude Code writes beside
each transcript:

| Gap | Source | Column / table |
|---|---|---|
| The task a sub-agent was given (`SubagentStart` has no prompt) | first user message of `subagents/**/agent-<id>.jsonl` | `agent_invocations.prompt_received` |
| Exact parent of a parallel fan-out (the `subagent_type` match is ambiguous) | `agent-<id>.meta.json` → `toolUseId` | `spawned_by_tool_call_id`, `parent_invocation_id` |
| Workflow runs (the `Workflow` tool returns at launch) | `<session>/workflows/<runId>.json` | `workflow_runs`; agents link via `agent_invocations.workflow_run_id` |

Filled on `SubagentStop` (provenance) and `Stop` / `SessionEnd` (workflow runs). Read
`vw_workflow_runs` for a run beside what its agents actually recorded. Rows recovered from
disk after the fact carry `source = 'backfill'`:
`uv run python scripts/backfill-subagents.py --apply`.
