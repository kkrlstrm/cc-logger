# Privacy and redaction

cc-logger captures **every tool call** Claude Code makes (within the allowlist). That includes the full `tool_input` and `tool_response`. Bash commands, WebFetch URLs, file paths — all of it lands in your Postgres database.

By default, **secrets are redacted before write**. Disable with `REDACT_SECRETS=0` if you want raw capture (e.g., on a strictly private DB you control).

## What gets captured

For an allowlist tool call, cc-logger writes the full payload to `tool_calls.tool_input` and `tool_calls.tool_response` (both JSONB). Examples:

- `Bash` PreToolUse → `tool_input` contains `{"command": "<the full shell command>", "description": "..."}`
- `Bash` PostToolUse → `tool_response` contains `{"stdout": "...", "stderr": "...", "exit_code": N}`
- `WebFetch` PreToolUse → `tool_input` contains `{"url": "...", "prompt": "..."}`
- `WebSearch` PreToolUse → `tool_input` contains `{"query": "..."}`
- `Edit` / `Write` PreToolUse → `tool_input` contains the full file path and the old/new strings

**This means a Bash command like `curl -H "Authorization: Bearer sk-..."` would put your token into the database.** Without redaction, every time you copy-paste a token into a Claude Code session, it lands in `tool_calls`.

## What redaction does

Default-on. Driven by [`src/cc_logger/redaction.py`](../src/cc_logger/redaction.py). The regex set:

| pattern | example | replacement |
|---|---|---|
| Anthropic / OpenAI API keys | `sk-ant-...`, `sk-proj-...` | `[REDACTED:anthropic-or-openai-key]` |
| GitHub tokens | `ghp_...`, `ghs_...`, `gho_...` | `[REDACTED:github-token]` |
| GitLab tokens | `glpat-...` | `[REDACTED:gitlab-token]` |
| Neon passwords | `npg_...` | `[REDACTED:neon-password]` |
| Slack tokens | `xoxb-...`, `xoxp-...` | `[REDACTED:slack-token]` |
| AWS access keys | `AKIA...` | `[REDACTED:aws-access-key]` |
| Bearer headers | `Bearer abc123...` | `[REDACTED:bearer-header]` |
| Postgres connection passwords | `postgresql://u:secret@host/db` | `postgresql://u:[REDACTED:postgres-password]@host/db` |
| URL query params (password, api_key, token, secret) | `?api_key=foo` | `?api_key=[REDACTED:url-password]` |

Redaction runs **before** truncation, so `artifacts` (the spillover table for payloads >50KB) is also redacted.

## What redaction does NOT catch

- Custom token formats specific to one service (we only ship regex for the common patterns above)
- Tokens embedded in payloads that don't match our regex (e.g., a 16-character password in a Bash command isn't distinguishable from regular text)
- Secrets in `initial_prompt` — that field is **not redacted** in v0.1. If you paste secrets into a user prompt, they land in `sessions.initial_prompt` unmodified. Future work.
- Secrets in `agent_invocations.prompt_received` (the message passed to a sub-agent) — same caveat. Not redacted in v0.1.

**Treat your `cclogger` database as sensitive even with redaction enabled.** It's safer than raw capture, but not bulletproof.

## How to audit your own DB

Quick check — does your DB contain any pattern that looks like a secret?

```sql
-- Common API key shapes
SELECT count(*) FROM tool_calls
WHERE tool_input::text ~ '(sk-|ghp_|glpat-|npg_|AKIA|xox[bp]-)';

-- Bearer tokens
SELECT count(*) FROM tool_calls
WHERE tool_input::text ~ 'Bearer [A-Za-z0-9._-]{20,}';

-- Postgres connection passwords
SELECT count(*) FROM tool_calls
WHERE tool_input::text ~ 'postgres(ql)?://[^:]+:[^@]+@';
```

With redaction on, those queries should return zero (or only false positives like the literal string `[REDACTED:...]`). Repeat against `tool_response` and `artifacts.full_content`.

## How to disable redaction

```
# In your .env
REDACT_SECRETS=0
```

Then restart the service (`launchctl unload/load` or `systemctl --user restart cclogger.service` or `docker compose restart cc-logger`). Going forward, payloads are stored raw. Existing rows are unchanged.

## How to purge

Drop the captured data while keeping the schema:

```sql
TRUNCATE artifacts, tool_calls, agent_invocations, sessions CASCADE;
```

Drop everything (database and all):
```bash
docker compose down -v   # also wipes the postgres-data volume
```
