#!/usr/bin/env bash
# Reverse install.sh — unload/disable and remove the unit file.
# Does NOT drop the database; data stays put.
set -euo pipefail

case "$(uname -s)" in
    Darwin)
        TARGET="$HOME/Library/LaunchAgents/com.cclogger.plist"
        if [ -f "$TARGET" ]; then
            launchctl unload "$TARGET" 2>/dev/null || true
            rm "$TARGET"
            echo "Removed $TARGET"
        else
            echo "Nothing to uninstall (no plist at $TARGET)"
        fi
        ;;
    Linux)
        if systemctl --user list-unit-files cclogger.service &>/dev/null; then
            systemctl --user disable --now cclogger.service 2>/dev/null || true
            rm -f "$HOME/.config/systemd/user/cclogger.service"
            systemctl --user daemon-reload
            echo "Removed cclogger.service"
        else
            echo "Nothing to uninstall (no cclogger.service registered)"
        fi
        ;;
    *)
        echo "Unsupported OS: $(uname -s)" >&2
        exit 1
        ;;
esac

echo
echo "To also remove the Claude Code hooks: python scripts/install-hooks.py --uninstall"
echo "Your captured data in Postgres is unchanged. To drop it, drop the cclogger database."
