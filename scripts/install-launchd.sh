#!/usr/bin/env bash
# Install cc-logger as a launchd user agent on macOS.
# - Substitutes paths into launchd/com.cclogger.plist.template
# - Copies result to ~/Library/LaunchAgents/
# - Loads the agent so it starts now and on every login
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$REPO_DIR/launchd/com.cclogger.plist.template"
TARGET="$HOME/Library/LaunchAgents/com.cclogger.plist"

if [ ! -f "$TEMPLATE" ]; then
    echo "ERROR: template not found at $TEMPLATE" >&2
    exit 1
fi

UV_PATH="$(command -v uv || true)"
if [ -z "$UV_PATH" ]; then
    echo "ERROR: uv not found in PATH. Install it first: brew install uv" >&2
    exit 1
fi

# Ensure log dir exists
mkdir -p "$HOME/Library/Logs"

# Derive a PATH that's likely to satisfy launchd (it gets a minimal env otherwise)
LAUNCH_PATH="$(dirname "$UV_PATH"):/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

# Substitute and write
sed \
    -e "s|__WORKING_DIR__|$REPO_DIR|g" \
    -e "s|__UV_PATH__|$UV_PATH|g" \
    -e "s|__HOME__|$HOME|g" \
    -e "s|__PATH__|$LAUNCH_PATH|g" \
    "$TEMPLATE" > "$TARGET"

echo "Wrote $TARGET"

# Reload — unload if already loaded (errors are non-fatal)
launchctl unload "$TARGET" 2>/dev/null || true
launchctl load "$TARGET"

sleep 2
if launchctl list | grep -q com.cclogger; then
    echo "cc-logger is running under launchd."
    echo "Logs: ~/Library/Logs/cc-logger.{out,err}.log"
    echo "Health: curl -s http://127.0.0.1:8787/healthz"
else
    echo "WARNING: launchctl list does not show com.cclogger — check ~/Library/Logs/cc-logger.err.log" >&2
    exit 1
fi
