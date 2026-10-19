# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Remote transcript upload: what the sender collects, and which paths the
recorder will write. The recorder writes wherever a request says, so the path
check is the security boundary and is tested from the attacker's side."""
import importlib.util
import json
from pathlib import Path

from cc_logger.transcripts import mirror_path

SID = "11111111-2222-4333-8444-555555555555"


def _ship():
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location("ship", root / "scripts" / "ship-transcript.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_four_claude_code_shapes_are_accepted(tmp_path):
    for rel in (f"{SID}.jsonl",
                f"{SID}/subagents/agent-a0000000000000001.jsonl",
                f"{SID}/subagents/agent-a0000000000000001.meta.json",
                f"{SID}/subagents/workflows/wf_0000aaaa-111/agent-a0000000000000002.jsonl",
                f"{SID}/workflows/wf_0000aaaa-111.json"):
        target = mirror_path("-app", rel, SID, tmp_path)
        assert target is not None and target.is_relative_to(tmp_path.resolve()), rel


def test_traversal_other_sessions_and_odd_names_are_refused(tmp_path):
    bad = [
        ("-app", f"{SID}/../../../etc/passwd"),
        ("-app", f"../{SID}.jsonl"),
        ("..", f"{SID}.jsonl"),
        ("-app/../x", f"{SID}.jsonl"),
        ("-app", "/etc/passwd"),
        ("-app", f"{SID}/subagents/agent-x.sh"),
        ("-app", f"{SID}/memory/MEMORY.md"),
        ("-app", "22222222-3333-4444-5555-666666666666.jsonl"),   # another session's file
    ]
    for project, rel in bad:
        assert mirror_path(project, rel, SID, tmp_path) is None, (project, rel)


def _session(tmp_path: Path) -> Path:
    proj = tmp_path / "-app"
    (proj / SID / "subagents" / "workflows" / "wf_abc").mkdir(parents=True)
    (proj / SID / "workflows").mkdir(parents=True)
    (proj / f"{SID}.jsonl").write_text('{"type":"user"}\n')
    (proj / SID / "workflows" / "wf_abc.json").write_text('{"runId":"wf_abc"}')
    (proj / SID / "subagents" / "agent-a1.jsonl").write_text('{"type":"user"}\n')
    (proj / SID / "subagents" / "agent-a1.meta.json").write_text('{"agentType":"Explore"}')
    (proj / SID / "subagents" / "workflows" / "wf_abc" / "agent-a2.jsonl").write_text("{}\n")
    return proj


def test_stop_sends_root_transcript_and_workflow_runs(tmp_path):
    proj = _session(tmp_path)
    project, files = _ship().collect({"hook_event_name": "Stop", "session_id": SID,
                                      "transcript_path": str(proj / f"{SID}.jsonl")})
    assert project == "-app"
    assert set(files) == {f"{SID}.jsonl", f"{SID}/workflows/wf_abc.json"}
    assert all(mirror_path(project, r, SID, tmp_path / "m") for r in files)


def test_subagent_stop_sends_that_agents_transcript_and_meta(tmp_path):
    proj = _session(tmp_path)
    ship = _ship()
    _, files = ship.collect({"hook_event_name": "SubagentStop", "session_id": SID,
                             "agent_id": "a1", "transcript_path": str(proj / f"{SID}.jsonl")})
    assert set(files) == {f"{SID}/subagents/agent-a1.jsonl", f"{SID}/subagents/agent-a1.meta.json"}
    _, files = ship.collect({"hook_event_name": "SubagentStop", "session_id": SID,
                             "agent_id": "a2", "transcript_path": str(proj / f"{SID}.jsonl")})
    assert set(files) == {f"{SID}/subagents/workflows/wf_abc/agent-a2.jsonl"}


def test_sender_ignores_a_transcript_that_is_not_this_sessions(tmp_path):
    proj = _session(tmp_path)
    assert _ship().collect({"hook_event_name": "Stop", "session_id": SID,
                            "transcript_path": str(proj / "other.jsonl")}) is None
