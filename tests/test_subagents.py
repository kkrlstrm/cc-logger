# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for sub-agent provenance + transcript-rebuilt tool calls.

File layout and record shapes match real Claude Code 2.1.280 sessions; values are made up.
"""
import json
from pathlib import Path

from cc_logger.transcripts import (
    agent_meta,
    agent_prompt,
    iter_tool_calls,
    transcript_span,
    workflow_run_id_for,
)


def _agent(tmp_path: Path, rel: str, records: list[dict], meta: dict | None = None) -> Path:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    if meta is not None:
        p.with_name(p.stem + ".meta.json").write_text(json.dumps(meta))
    return p


def test_prompt_is_the_first_user_message_of_the_agents_own_transcript(tmp_path):
    p = _agent(tmp_path, "proj/sid/subagents/agent-a1.jsonl", [
        {"type": "user", "isSidechain": True, "agentId": "a1",
         "message": {"role": "user", "content": "Summarise the module"}},
        {"type": "user", "message": {"role": "user", "content": "a later turn"}},
    ])
    assert agent_prompt(p) == "Summarise the module"


def test_prompt_skips_meta_records_and_reads_text_blocks(tmp_path):
    p = _agent(tmp_path, "proj/sid/subagents/agent-a1.jsonl", [
        {"type": "user", "isMeta": True, "message": {"content": "<system-reminder>x</system-reminder>"}},
        {"type": "user", "message": {"content": [{"type": "text", "text": "Find the answer"}]}},
    ])
    assert agent_prompt(p) == "Find the answer"


def test_meta_names_the_exact_parent_agent_call(tmp_path):
    p = _agent(tmp_path, "proj/sid/subagents/agent-a1.jsonl", [], meta={
        "agentType": "Explore", "description": "Summarise",
        "toolUseId": "toolu_parent", "spawnDepth": 1})
    m = agent_meta(p)
    assert m["toolUseId"] == "toolu_parent" and m["agentType"] == "Explore"


def test_missing_meta_is_empty_not_an_error(tmp_path):
    p = _agent(tmp_path, "proj/sid/subagents/agent-a1.jsonl", [])
    assert agent_meta(p) == {}


def test_workflow_agent_belongs_to_its_run_directory(tmp_path):
    wf = _agent(tmp_path, "proj/sid/subagents/workflows/wf_0000aaaa-111/agent-a2.jsonl", [])
    plain = _agent(tmp_path, "proj/sid/subagents/agent-a3.jsonl", [])
    assert workflow_run_id_for(wf) == "wf_0000aaaa-111"
    assert workflow_run_id_for(plain) is None


def test_tool_calls_rebuilt_with_status_duration_and_ids(tmp_path):
    p = _agent(tmp_path, "proj/sid/subagents/agent-a1.jsonl", [
        {"type": "assistant", "timestamp": "2026-09-10T10:00:00Z", "message": {"content": [
            {"type": "tool_use", "id": "toolu_ok", "name": "Bash", "input": {"command": "ls"}},
            {"type": "tool_use", "id": "toolu_bad", "name": "WebFetch", "input": {"url": "x"}},
            {"type": "tool_use", "id": "toolu_lost", "name": "Read", "input": {"file_path": "y"}}]}},
        {"type": "user", "timestamp": "2026-09-10T10:00:02Z", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_ok", "content": "a\nb"}]},
         "toolUseResult": {"stdout": "a\nb", "stderr": ""}},
        {"type": "user", "timestamp": "2026-09-10T10:00:05Z", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_bad", "is_error": True,
             "content": "403 Forbidden"}]}},
    ])
    rows = {r["tool_call_id"]: r for r in iter_tool_calls(p)}
    assert rows["toolu_ok"]["status"] == "success"
    assert rows["toolu_ok"]["tool_response"] == {"stdout": "a\nb", "stderr": ""}
    assert rows["toolu_ok"]["duration_ms"] == 2000
    assert rows["toolu_bad"]["status"] == "failure" and rows["toolu_bad"]["error"] == "403 Forbidden"
    assert rows["toolu_lost"]["status"] == "orphaned"


def test_tool_call_filter_keeps_only_named_tools(tmp_path):
    p = _agent(tmp_path, "proj/sid.jsonl", [
        {"type": "assistant", "timestamp": "2026-09-21T19:52:00Z", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Workflow", "input": {"name": "fan-out"}},
            {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "ls"}}]}},
        {"type": "user", "timestamp": "2026-09-21T19:52:01Z", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "launched"}]},
         "toolUseResult": {"status": "async_launched", "runId": "wf_0000aaaa-111"}},
    ])
    [row] = list(iter_tool_calls(p, only={"Workflow"}))
    assert row["tool_response"]["runId"] == "wf_0000aaaa-111"


def test_span_reads_first_last_cwd_and_first_real_prompt(tmp_path):
    p = _agent(tmp_path, "proj/sid.jsonl", [
        {"type": "user", "isMeta": True, "timestamp": "2026-09-01T08:00:00Z", "cwd": "/repo",
         "message": {"content": "meta"}},
        {"type": "user", "timestamp": "2026-09-01T08:00:01Z",
         "message": {"content": "<command-name>/clear</command-name>"}},
        {"type": "user", "timestamp": "2026-09-01T08:00:02Z", "message": {"content": "real ask"}},
        {"type": "assistant", "timestamp": "2026-09-01T09:00:00Z", "message": {"content": []}},
    ])
    s = transcript_span(p)
    assert s["cwd"] == "/repo" and s["prompt"] == "real ask"
    assert s["first"].hour == 8 and s["last"].hour == 9
