# Schema reference

cc-logger writes to 5 tables (`sessions`, `agent_invocations`, `tool_calls`, `artifacts`, `messages`). Core DDL is in [`migrations/001_initial_schema.py`](../migrations/001_initial_schema.py), token-usage columns in [`migrations/004_tokens.py`](../migrations/004_tokens.py), analytical views in [`migrations/002_views.py`](../migrations/002_views.py), and the `messages` (narration) table in [`migrations/003_messages.py`](../migrations/003_messages.py). Apply all of them at once with `cc-logger migrate --apply` (run order is defined by the list in the CLI, not the filename numbers — 004 runs before 002 because the views read its columns).

> **Where model and tokens come from.** Neither is reliably available from the hook stream — Claude Code's `SessionStart` hook frequently omits the model, and **no** hook event carries token totals (`SessionEnd` reports only a `reason`). Both are instead recovered from the transcript JSONL, where every assistant line records `message.model` and a `message.usage` block. cc-logger sums them in `transcripts.scan_transcript_stats` during the same transcript read it already does for narration. Existing rows can be repopulated with [`scripts/backfill-tokens-model.py`](../scripts/backfill-tokens-model.py).

## `sessions` — one row per Claude Code session

| column | type | notes |
|---|---|---|
| `session_id` | TEXT PK | Claude Code session UUID. UPSERT on resume. |
| `started_at` | TIMESTAMPTZ | When SessionStart fired. |
| `ended_at` | TIMESTAMPTZ | When SessionEnd fired (NULL if session is still open). |
| `cwd` | TEXT | Working directory at session start. |
| `model` | TEXT | The model that ran the session (most-frequent assistant model in the root transcript). Recovered from the transcript at `Stop`/`SessionEnd`, since the SessionStart hook often omits it. |
| `initial_prompt` | TEXT | First UserPromptSubmit content. |
| `end_reason` | TEXT | "exit", "logout", etc. |
| `input_tokens` / `output_tokens` / `cache_read_tokens` / `cache_creation_tokens` | BIGINT | Token usage summed across **all** the session's invocations (root + every sub-agent), recomputed from the transcript on each ingest. |
| `total_tokens` | BIGINT | Sum of the four columns above. (Originally meant to come from `SessionEnd`, but that hook carries no token data — it's now derived from the transcript.) |
| `self_rating` | SMALLINT (1-5) | Retrospective rating. Set with `cc-logger rate <session> <1-5>`. |
| `retro_note` | TEXT | Free-form retro note. Set with `cc-logger rate … --note "…"`. |

## `agent_invocations` — one row per agent (root + every sub-agent)

| column | type | notes |
|---|---|---|
| `invocation_id` | TEXT PK | For root: `root::<session_id>`. For sub-agents: Claude Code's `agent_id`. |
| `session_id` | TEXT FK | References `sessions(session_id)`. |
| `parent_invocation_id` | TEXT FK | NULL for root. References this same table for sub-agents. |
| `spawned_by_tool_call_id` | TEXT | The `Agent` tool_call that produced this sub-agent. |
| `candidate_parent_tool_call_ids` | JSONB | When linking is ambiguous (parallel fan-out with same agent_type), all candidate parent IDs. |
| `agent_id` | TEXT | Claude Code's `agent_id`. NULL for root. |
| `agent_type` | TEXT | "root" for the root, otherwise the sub-agent type (e.g., "general-purpose", "Explore"). |
| `model` | TEXT | The model that ran this invocation. For sub-agents, recovered from the sub-agent's transcript at `SubagentStop`; for the root, from the root transcript. |
| `prompt_received` | TEXT | The prompt this sub-agent was spawned with. |
| `last_message` | TEXT | Final message before SubagentStop. |
| `input_tokens` / `output_tokens` / `cache_read_tokens` / `cache_creation_tokens` / `total_tokens` | BIGINT | Per-invocation token usage summed from this invocation's own transcript. The session's totals are the sum of these across its invocations. |
| `started_at` | TIMESTAMPTZ | When SubagentStart fired (or session start for root). |
| `ended_at` | TIMESTAMPTZ | When SubagentStop fired. |
| `status` | TEXT | `pending` \| `completed` \| `orphaned`. |
| `orphaned_at` | TIMESTAMPTZ | Set during SessionEnd sweep if still pending. |

## `tool_calls` — one row per tool call in the capture allowlist

| column | type | notes |
|---|---|---|
| `tool_call_id` | TEXT PK | Claude Code's `tool_use_id`. |
| `session_id` | TEXT FK | References `sessions(session_id)`. |
| `invocation_id` | TEXT FK | The agent that made the call (root or sub-agent). |
| `tool_name` | TEXT | "Bash", "WebSearch", "Agent", "mcp__...", etc. |
| `subagent_type` | TEXT | Extracted from `tool_input.subagent_type` when `tool_name='Agent'`. Used for subagent linking. |
| `tool_input` | JSONB | The full input payload (after redaction). Oversized strings spill to `artifacts`. |
| `tool_response` | JSONB | The full response payload (after redaction). Same spillover rule. |
| `status` | TEXT | `pending` \| `success` \| `failure` \| `orphaned`. |
| `error` | TEXT | Populated on PostToolUseFailure. |
| `duration_ms` | INTEGER | Server-computed: PostToolUse timestamp minus PreToolUse timestamp. |
| `started_at` | TIMESTAMPTZ | When PreToolUse fired. |
| `received_at` | TIMESTAMPTZ | When the worker actually processed the event. Difference shows async queue lag. |

## `messages` — assistant text blocks (Claude's narration)

Populated by reading the Claude Code transcript JSONL at `Stop` / `SubagentStop` / `SessionEnd`. Only `text` blocks are captured; Claude's `thinking` blocks are encrypted in the transcript (signature only, no plaintext) and we can't extract them.

| column | type | notes |
|---|---|---|
| `message_id` | TEXT | Anthropic message UUID from the transcript. |
| `block_index` | INTEGER | Position of the text block within the message's `content` array. PK is composite (`message_id`, `block_index`). |
| `session_id` | TEXT FK | References `sessions(session_id)`. |
| `invocation_id` | TEXT FK | The agent that produced the message (root or sub-agent). |
| `role` | TEXT | `assistant` (we only capture assistant text). |
| `block_type` | TEXT | `text` (we only capture text blocks). |
| `text` | TEXT | The text Claude said, after redaction. |
| `position` | INTEGER | Line number in the source JSONL — gives a stable in-transcript ordering. |
| `created_at` | TIMESTAMPTZ | When the row was inserted. |

Indexed on `(session_id, position)` and `(invocation_id)`.

## `artifacts` — overflow for any field >50KB

| column | type | notes |
|---|---|---|
| `artifact_id` | TEXT PK | UUID. Referenced from `tool_input` / `tool_response` JSONB via `_truncated_artifact_id`. |
| `tool_call_id` | TEXT FK | The owning tool call. ON DELETE CASCADE. |
| `field_name` | TEXT | Dotted JSONPath of the spilled field, e.g. `"tool_response.stdout"` or `"items.[3]"`. |
| `full_content` | TEXT | The redacted-but-not-truncated content. |
| `size_bytes` | INTEGER | UTF-8 byte length. |

## Views (from `002_views.py`)

| view | what it shows |
|---|---|
| `vw_session_summary` | One row per session with tool/sub-agent counts, duration, failure count, prompt preview, model, token totals, and self-rating. |
| `vw_tool_usage_24h` | Tool mix over the last 24h with ok/fail/pending counts and latency percentiles. |
| `vw_subagent_tree` | Flattened recursive view of every agent invocation with parent linkage and depth. |
| `vw_repeat_fail_domains` | WebFetch hostnames ranked by failure count — a "URLs to avoid" list. |

## Indexes

Created by `001_initial_schema.py`:
- `sessions(started_at DESC)`
- `agent_invocations(session_id)`, `(parent_invocation_id)`, `(spawned_by_tool_call_id)`
- `tool_calls(session_id)`, `(invocation_id)`, `(tool_name)`, `(status) WHERE status='pending'` (partial)
- `artifacts(tool_call_id)`
