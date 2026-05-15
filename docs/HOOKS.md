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
- `mcp__.*` (any MCP server tool, regex)

**Intentionally skipped**: `Read`, `Glob`, `Grep`, `TodoWrite`, `NotebookEdit`. These are very high-volume and rarely interesting for prompt-practice review.

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
