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

# SessionStart also gets a COMMAND hook: the session's cross-session inbox socket is
# only exported as an environment variable, which an HTTP hook cannot see. Stdlib-only
# script, so the system interpreter is enough.
PEER_HOOK_SCRIPT = Path(__file__).resolve().parent / "peer-address-hook.py"
# /usr/bin/python3 on macOS/Linux; on Windows (no such path, and `python3` can be the
# Microsoft Store stub) the interpreter running this installer.
PEER_HOOK_PYTHON = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else sys.executable


def _hook_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/hook"


def _our_hook(port: int) -> dict:
    return {"type": "http", "url": _hook_url(port), "timeout": 5}


def _peer_hook(port: int) -> dict:
    return {
        "type": "command",
        "command": f'"{PEER_HOOK_PYTHON}" "{PEER_HOOK_SCRIPT}" --port {port}',
        "timeout": 5,
    }


def _build_hooks_block(port: int) -> dict:
    block: dict = {}
    for ev in SIMPLE_EVENTS:
        block[ev] = [{"hooks": [_our_hook(port)]}]
    block["SessionStart"].append({"hooks": [_peer_hook(port)]})
    for ev in TOOL_EVENTS:
        block[ev] = [
            {"matcher": m, "hooks": [_our_hook(port)]} for m in TOOL_MATCHERS
        ]
    return block


def _is_our_hook(h: dict, port: int) -> bool:
    """One hook we installed: the HTTP hook at our URL, or the peer-address command."""
    return isinstance(h, dict) and (
        (h.get("type") == "http" and h.get("url") == _hook_url(port))
        or (h.get("type") == "command" and PEER_HOOK_SCRIPT.name in str(h.get("command", "")))
    )


def _strip_ours(entries: list, port: int) -> tuple[list, int]:
    """Remove our hooks from a list of matcher entries, hook by hook.

    Filtering whole ENTRIES was wrong: another tool can put its own hook in the
    same entry as ours (another tool's SessionStart/SessionEnd commands sat beside our
    HTTP hook), and dropping the entry silently uninstalled that tool. Only an
    entry left with no hooks is removed.
    """
    kept, removed = [], 0
    for e in entries:
        if not isinstance(e, dict):
            kept.append(e)
            continue
        hooks = e.get("hooks", [])
        others = [h for h in hooks if not _is_our_hook(h, port)]
        removed += len(hooks) - len(others)
        if others:
            kept.append({**e, "hooks": others})
        elif not hooks:
            kept.append(e)
    return kept, removed


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
        # Remove our previous hooks (so re-running is idempotent), keep everyone else's
        kept, _ = _strip_ours(hooks.get(ev, []), port)
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
        hooks[ev], n = _strip_ours(hooks[ev], port)
        removed += n
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
