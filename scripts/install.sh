#!/usr/bin/env bash
# OS-aware installer: dispatches to install-launchd.sh (macOS) or install-systemd.sh (Linux).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

case "$(uname -s)" in
    Darwin)
        exec "$SCRIPT_DIR/install-launchd.sh" "$@"
        ;;
    Linux)
        exec "$SCRIPT_DIR/install-systemd.sh" "$@"
        ;;
    *)
        echo "Unsupported OS: $(uname -s)" >&2
        echo "Supported: macOS (launchd), Linux (systemd)" >&2
        echo "Or just run manually: uv run cc-logger serve" >&2
        exit 1
        ;;
esac
