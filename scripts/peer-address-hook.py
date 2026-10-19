#!/usr/bin/env python3
# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SessionStart command hook: tell cc-logger which inbox socket this session owns.

Cross-session messages name their sender by socket (`origin.from` is
`uds:/tmp/cc-socks/<pid>.sock`), and nothing in the transcript or the hook
payload records a session's OWN socket. Claude Code exports it only as the
`CLAUDE_CODE_MESSAGING_SOCKET` environment variable, and only command hooks see
it -- an HTTP hook on SessionStart did not fire in a headless `claude -p` test
(2026-10-13), so this cannot be a header on the existing HTTP hook.

Stdlib only, so it runs under /usr/bin/python3. Never prints: SessionStart
stdout is injected into the model's context. Never fails: a logging hook must
not be able to break a session.

Usage (installed by scripts/install-hooks.py):
    /usr/bin/python3 scripts/peer-address-hook.py [--port 8787]
"""
import json
import os
import sys
import urllib.request


def main() -> None:
    sock = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET")
    if not sock:
        return
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    session_id = payload.get("session_id")
    if not session_id:
        return
    port = "8787"
    if "--port" in sys.argv[1:-1]:
        port = sys.argv[sys.argv.index("--port") + 1]
    event = {
        "hook_event_name": "PeerAddress",
        "session_id": session_id,
        "messaging_socket": sock,
        "source": payload.get("source"),
        "session_title": payload.get("session_title"),
        "cwd": payload.get("cwd"),
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/hook",
        data=json.dumps(event).encode(),
        headers={"content-type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=2).read()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    finally:
        sys.exit(0)
