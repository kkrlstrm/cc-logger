#!/usr/bin/env python3
# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Command hook: send this run's transcript files to a REMOTE cc-logger.

A cc-logger on another machine receives the hook events but cannot read the
transcript files, and those files are the only source of Claude's narration,
token counts, a sub-agent's prompt and result, cross-session messages and
workflow run records. Wire this on Stop, SubagentStop and SessionEnd:

    python3 ship-transcript.py http://recorder:8787

  Stop / SessionEnd -> the root transcript + `<session>/workflows/wf_*.json`
  SubagentStop      -> that sub-agent's transcript + its `.meta.json`

The recorder writes them into its own mirror of ~/.claude/projects and runs the
same ingest it would run locally. Whole files are re-sent each time: ingest is
idempotent and headless runs are short.

Stdlib only. Never prints (hook stdout can reach the model), never fails a run.
"""
import json
import sys
import urllib.request
from pathlib import Path

MAX_FILE_BYTES = 32 * 1024 * 1024
EVENTS = ("Stop", "SubagentStop", "SessionEnd")


def collect(payload: dict) -> tuple[str, dict[str, str]] | None:
    """(project dir name, {relpath: text}) for this event, or None."""
    session_id = payload.get("session_id")
    root = Path(payload.get("transcript_path") or "")
    if not session_id or root.name != f"{session_id}.jsonl":
        return None
    proj = root.parent
    if payload.get("hook_event_name") == "SubagentStop":
        agent_id = payload.get("agent_id")
        if not agent_id:
            return None
        hits = list((proj / session_id / "subagents").rglob(f"agent-{agent_id}.jsonl"))
        if not hits:
            return None
        paths = [hits[0], hits[0].with_name(f"agent-{agent_id}.meta.json")]
    else:
        paths = [root] + sorted((proj / session_id / "workflows").glob("wf_*.json"))
    files = {}
    for p in paths:
        try:
            if p.is_file() and p.stat().st_size <= MAX_FILE_BYTES:
                files[p.relative_to(proj).as_posix()] = p.read_text(errors="replace")
        except OSError:
            continue
    return (proj.name, files) if files else None


def main() -> None:
    if len(sys.argv) < 2:
        return
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    if payload.get("hook_event_name") not in EVENTS:
        return
    found = collect(payload)
    if not found:
        return
    project, files = found
    body = {
        "hook_event_name": "TranscriptUpload",
        "session_id": payload["session_id"],
        "agent_id": payload.get("agent_id") if payload.get("hook_event_name") == "SubagentStop" else None,
        "trigger": payload.get("hook_event_name"),
        "project": project,
        "files": files,
    }
    url = sys.argv[1].rstrip("/")
    req = urllib.request.Request(
        url if url.endswith("/hook") else f"{url}/hook",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    finally:
        sys.exit(0)
