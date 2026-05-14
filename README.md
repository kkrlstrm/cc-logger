# cc-logger

Local observability for [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Captures every session, every sub-agent fan-out, every tool call to a Postgres database so you can review your AI-coding practice over time.

Built on top of [Claude Code's HTTP hooks](https://docs.claude.com/en/docs/claude-code/hooks) — no patches or modifications to Claude Code itself.

> **Status**: Work in progress. This README is the quickstart; deeper docs live in [`docs/`](docs/).

## What it captures

- Every Claude Code session (start, end, initial prompt, end reason)
- Every sub-agent invocation (root + children, with linkage to the spawning `Agent` tool call)
- Every tool call in the capture allowlist (Agent, Bash, Edit, Write, WebFetch, WebSearch, and `mcp__.*`)
- Tool input + tool response payloads as JSONB; anything >50KB spills to a separate `artifacts` table
- Optional regex redaction of common secret patterns before write (on by default)

## Quickstart (Docker Compose)

```bash
git clone https://github.com/kkrlstrm/cc-logger.git
cd cc-logger
cp .env.example .env                # defaults work for local Docker setup
docker compose up -d                # boots Postgres + cc-logger

# Migrate schema
docker compose exec cc-logger python migrations/001_initial_schema.py --apply

# Wire Claude Code hooks (merges into ~/.claude/settings.json with a backup)
python scripts/install-hooks.py

# Run a Claude Code session, then check what's been captured:
docker compose exec cc-logger python -m cc_logger.cli sessions
```

## Alternative: run natively (no Docker)

```bash
uv sync
# Put a Postgres connection string in .env (Neon, Supabase, RDS, local install — anything)
uv run python migrations/001_initial_schema.py --apply
./scripts/install.sh                # installs launchd (macOS) or systemd (Linux) auto-start
python scripts/install-hooks.py     # wires the Claude Code hooks
```

## Privacy

By default, common secret patterns (API keys, bearer tokens, passwords in connection strings) are redacted before any payload is written to Postgres. Disable with `REDACT_SECRETS=0` if you want raw capture. See [`docs/PRIVACY.md`](docs/PRIVACY.md).

## Tool capture allowlist

Captured: `Agent`, `Bash`, `Edit`, `Write`, `WebFetch`, `WebSearch`, `mcp__.*`
Skipped: `Read`, `Glob`, `Grep`, `TodoWrite` (to keep volume sane).

## CLI

```bash
cc-logger sessions [--limit N]              # list recent sessions
cc-logger inspect <session-id>              # render the session tree
cc-logger insights [--days N]               # cross-session analytics
```

## Schema

Four tables: `sessions`, `agent_invocations`, `tool_calls`, `artifacts`. Full reference in [`docs/SCHEMA.md`](docs/SCHEMA.md).

## License

MIT. See [LICENSE](LICENSE).
