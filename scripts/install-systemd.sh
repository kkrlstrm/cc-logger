#!/usr/bin/env bash
# Install cc-logger as a systemd user service on Linux.
# - Substitutes paths into systemd/cclogger.service.template
# - Writes ~/.config/systemd/user/cclogger.service
# - Enables and starts the service
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TEMPLATE="$REPO_DIR/systemd/cclogger.service.template"
TARGET_DIR="$HOME/.config/systemd/user"
TARGET="$TARGET_DIR/cclogger.service"

if [ ! -f "$TEMPLATE" ]; then
    echo "ERROR: template not found at $TEMPLATE" >&2
    exit 1
fi

UV_PATH="$(command -v uv || true)"
if [ -z "$UV_PATH" ]; then
    echo "ERROR: uv not found in PATH. Install it first: https://github.com/astral-sh/uv" >&2
    exit 1
fi

mkdir -p "$TARGET_DIR" "$HOME/.local/state/cc-logger"

SYS_PATH="$(dirname "$UV_PATH"):/usr/local/bin:/usr/bin:/bin"

sed \
    -e "s|__WORKING_DIR__|$REPO_DIR|g" \
    -e "s|__UV_PATH__|$UV_PATH|g" \
    -e "s|__HOME__|$HOME|g" \
    -e "s|__PATH__|$SYS_PATH|g" \
    "$TEMPLATE" > "$TARGET"

echo "Wrote $TARGET"

systemctl --user daemon-reload
systemctl --user enable cclogger.service
systemctl --user restart cclogger.service

# Linger so the service runs even when no user session is active
if ! loginctl show-user "$USER" 2>/dev/null | grep -q "Linger=yes"; then
    echo "Tip: enable linger so the service keeps running across logout:"
    echo "  sudo loginctl enable-linger $USER"
fi

sleep 2
if systemctl --user is-active --quiet cclogger.service; then
    echo "cc-logger is running under systemd (user)."
    echo "Logs: ~/.local/state/cc-logger/cc-logger.{out,err}.log"
    echo "Health: curl -s http://127.0.0.1:8787/healthz"
else
    echo "WARNING: cclogger.service is not active. Check: journalctl --user -u cclogger.service -e" >&2
    exit 1
fi
