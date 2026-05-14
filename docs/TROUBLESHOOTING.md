# Troubleshooting

## "Port 8787 is already in use"

Something else (or an older cc-logger) is bound to 8787. Find it:

```bash
lsof -i :8787    # macOS / Linux
```

Either stop that process or change `HOOK_PORT` in `.env` (and re-run `python scripts/install-hooks.py --port <new>` to update the settings.json URL).

## `/healthz` returns 503 with `"db": "down"`

The service is up but can't talk to Postgres. Common causes:

- **Docker Postgres not running**: `docker compose ps` should show `postgres` healthy. If not: `docker compose up -d postgres`.
- **`DATABASE_URL` is wrong**: confirm the URL works with `psql "$DATABASE_URL" -c 'select 1'`.
- **Neon pooler endpoint**: Neon's `-pooler` host is incompatible with long-lived prepared statements. Use the direct (non-pooler) endpoint instead. Connection string should NOT contain `-pooler` in the hostname.

## Events post but nothing lands in DB

Check `processed` and `failed` counters on `/healthz`:

```bash
curl -s http://127.0.0.1:8787/healthz | jq
```

If `processed=0` and `failed=0`, hook events aren't arriving — check `~/.claude/settings.json` has the hooks block. Re-run `python scripts/install-hooks.py`.

If `failed > 0`, check the err log:
- macOS: `tail -100 ~/Library/Logs/cc-logger.err.log`
- Linux: `tail -100 ~/.local/state/cc-logger/cc-logger.err.log`
- Docker: `docker compose logs cc-logger`

Typical failures: pydantic validation errors (Claude Code shipped a new payload shape) or psycopg connection drops. The worker auto-retries OperationalError once.

## launchd service won't load (macOS)

```bash
launchctl list | grep cclogger
```

If the line shows a non-zero exit code, check the err log. Common causes:
- `uv` is not at the path the plist expected. Re-run `./scripts/install-launchd.sh` to detect the right path.
- The `.venv` is missing. Run `uv sync` in the repo directory.

## systemd service won't start (Linux)

```bash
systemctl --user status cclogger.service
journalctl --user -u cclogger.service -e
```

If the service exits immediately, it's almost always a missing `uv` on PATH or a bad `DATABASE_URL`. Edit `~/.config/systemd/user/cclogger.service` directly if you need to adjust paths, then `systemctl --user daemon-reload && systemctl --user restart cclogger.service`.

For the service to survive across logout, enable linger:

```bash
sudo loginctl enable-linger $USER
```

## Settings.json merge conflict

`scripts/install-hooks.py` makes a `.bak.<timestamp>` backup before every write. If something went wrong, restore from the backup:

```bash
ls ~/.claude/settings.json.bak.*
cp ~/.claude/settings.json.bak.<latest> ~/.claude/settings.json
```

Then re-run the installer. If you have a tricky existing `hooks` block (e.g., from another tool), edit the file by hand using [`examples/settings-hooks.json`](../examples/settings-hooks.json) as the reference for our block.

## Pending tool calls that never complete

Look at `queries/08_orphaned_calls.sql`. Two common causes:

1. **Long-running Bash** that Claude Code killed (timeout, `Esc`, session quit). PostToolUse never fires.
2. **MCP tool calls** that hung. Some MCP servers don't honor cancellation.

These get marked `orphaned` on the next `SessionEnd` for that session. They're not data loss — the PreToolUse row is preserved with everything we know.

## "Queue full" warnings in the log

The in-memory queue maxes at 10,000 events. Hitting it means the DB writer fell behind for a sustained period. Check:

- Is the DB slow? Look at recent `tool_calls.received_at - tool_calls.started_at` lag.
- Is the worker stuck on a single event? Check the err log for repeating exceptions.

If you've genuinely got a bursty workload, restart the service to drain. Future work: spill to disk on overflow (PR 9 of the OSS prep plan, deferred).

## Resetting everything

```bash
# Stop service
./scripts/uninstall.sh
python scripts/install-hooks.py --uninstall

# Wipe DB (Docker)
docker compose down -v

# Or wipe DB rows but keep schema (any Postgres)
psql "$DATABASE_URL" -c "TRUNCATE artifacts, tool_calls, agent_invocations, sessions CASCADE;"

# Reinstall
docker compose up -d
docker compose exec cc-logger python migrations/001_initial_schema.py --apply
./scripts/install.sh
python scripts/install-hooks.py
```
