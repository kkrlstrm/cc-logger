#!/usr/bin/env python3
# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Install (or uninstall) cc-logger HTTP hooks in ~/.claude/settings.json.

Performs a deep merge: existing hooks for OTHER events are preserved; existing
hooks for OUR events (SessionStart, PreToolUse, etc.) pointing at OUR URL are
deduplicated; everything else stays. Always writes a `.bak.<timestamp>` first.

Usage:
    python scripts/install-hooks.py             # install
    python scripts/install-hooks.py --uninstall # remove ONLY our hooks
    python scripts/install-hooks.py --port 8787 # override default port
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

SETTINGS = Path.home() / ".claude" / "settings.json"

DEFAULT_PORT = 8787

# Tool matchers used by our hooks.
#
# DERIVED from cc_logger.filters.CAPTURE_TOOLS rather than restated: the matcher and the
# server-side allowlist are the same fact in two places, and a hand-maintained copy drifts.
# When it drifts silently the failure is invisible — the hook simply never fires for the
# tool you thought you were capturing. tests/test_filters.py asserts they agree.
TOOL_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure")
SIMPLE_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "SubagentStart",
    "SubagentStop",
    "Stop",
    "SessionEnd",
)
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from cc_logger.filters import CAPTURE_TOOLS

    _NAMED = "|".join(sorted(CAPTURE_TOOLS))
except Exception:  # noqa: BLE001 — installer must work from a bare checkout
    _NAMED = "Agent|Bash|Edit|Read|Skill|WebFetch|WebSearch|Write"

TOOL_MATCHERS = (_NAMED, "mcp__.*")


def _hook_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/hook"


def _our_hook(port: int) -> dict:
    return {"type": "http", "url": _hook_url(port), "timeout": 5}


def _build_hooks_block(port: int) -> dict:
    block: dict = {}
    for ev in SIMPLE_EVENTS:
        block[ev] = [{"hooks": [_our_hook(port)]}]
    for ev in TOOL_EVENTS:
        block[ev] = [
            {"matcher": m, "hooks": [_our_hook(port)]} for m in TOOL_MATCHERS
        ]
    return block


def _is_our_entry(entry: dict, port: int) -> bool:
    """Identify entries we previously installed (so we can replace/remove them)."""
    if not isinstance(entry, dict):
        return False
    hooks = entry.get("hooks", [])
    return any(
        isinstance(h, dict) and h.get("url") == _hook_url(port) and h.get("type") == "http"
        for h in hooks
    )


def install(port: int) -> None:
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if SETTINGS.exists():
        existing = json.loads(SETTINGS.read_text())
        backup = SETTINGS.with_suffix(f".json.bak.{int(time.time())}")
        shutil.copy(SETTINGS, backup)
        print(f"Backed up existing settings to {backup}")
    else:
        print(f"No existing {SETTINGS} — creating new one.")

    hooks = existing.setdefault("hooks", {})
    our = _build_hooks_block(port)

    for ev, ours in our.items():
        current = hooks.get(ev, [])
        # Drop any pre-existing entries that point at our URL (so re-running is idempotent)
        kept = [e for e in current if not _is_our_entry(e, port)]
        hooks[ev] = kept + ours

    SETTINGS.write_text(json.dumps(existing, indent=2) + "\n")
    print(f"Installed cc-logger hooks for 8 events → {_hook_url(port)}")
    print(f"Wrote {SETTINGS}")


def uninstall(port: int) -> None:
    if not SETTINGS.exists():
        print(f"Nothing to remove ({SETTINGS} does not exist).")
        return
    existing = json.loads(SETTINGS.read_text())
    hooks = existing.get("hooks", {})
    backup = SETTINGS.with_suffix(f".json.bak.{int(time.time())}")
    shutil.copy(SETTINGS, backup)
    print(f"Backed up existing settings to {backup}")

    removed = 0
    for ev in SIMPLE_EVENTS + TOOL_EVENTS:
        if ev not in hooks:
            continue
        before = len(hooks[ev])
        hooks[ev] = [e for e in hooks[ev] if not _is_our_entry(e, port)]
        removed += before - len(hooks[ev])
        if not hooks[ev]:
            del hooks[ev]

    if not hooks:
        existing.pop("hooks", None)

    SETTINGS.write_text(json.dumps(existing, indent=2) + "\n")
    print(f"Removed {removed} cc-logger hook entries from {SETTINGS}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--uninstall", action="store_true", help="remove our hooks instead of installing")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"cc-logger port (default {DEFAULT_PORT})")
    args = ap.parse_args()
    if args.uninstall:
        uninstall(args.port)
    else:
        install(args.port)


if __name__ == "__main__":
    main()
