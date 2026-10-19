# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for transcripts._iter_text_blocks.

DB-side `ingest()` is exercised by the harness; here we just unit-test the
JSONL-parsing logic.
"""
import json
from pathlib import Path

import pytest

from cc_logger import transcripts
from cc_logger.transcripts import (
    _iter_text_blocks,
    scan_session_totals,
    scan_transcript_stats,
    session_subagent_transcripts,
)


def _write_transcript(tmp_path: Path, messages: list[dict]) -> Path:
    p = tmp_path / "transcript.jsonl"
    p.write_text("\n".join(json.dumps(m) for m in messages) + "\n")
    return p


def test_extracts_text_blocks_from_assistant_message(tmp_path):
    p = _write_transcript(tmp_path, [
        {
            "type": "assistant",
            "message": {
                "id": "msg-1",
                "content": [
                    {"type": "text", "text": "First decision."},
                    {"type": "text", "text": "Second decision."},
                ],
            },
        },
    ])
    blocks = list(_iter_text_blocks(p))
    assert len(blocks) == 2
    assert blocks[0]["message_id"] == "msg-1"
    assert blocks[0]["block_index"] == 0
    assert blocks[0]["text"] == "First decision."
    assert blocks[1]["block_index"] == 1
    assert blocks[1]["text"] == "Second decision."


def test_skips_thinking_blocks(tmp_path):
    p = _write_transcript(tmp_path, [
        {
            "type": "assistant",
            "message": {
                "id": "msg-1",
                "content": [
                    {"type": "thinking", "thinking": "private reasoning", "signature": "sig"},
                    {"type": "text", "text": "Public narration."},
                ],
            },
        },
    ])
    blocks = list(_iter_text_blocks(p))
    assert len(blocks) == 1
    assert blocks[0]["text"] == "Public narration."


def test_skips_tool_use_blocks(tmp_path):
    p = _write_transcript(tmp_path, [
        {
            "type": "assistant",
            "message": {
                "id": "msg-1",
                "content": [
                    {"type": "text", "text": "I'll grep."},
                    {"type": "tool_use", "id": "tu-1", "name": "Bash", "input": {"command": "grep x"}},
                ],
            },
        },
    ])
    blocks = list(_iter_text_blocks(p))
    assert len(blocks) == 1
    assert blocks[0]["text"] == "I'll grep."


def test_skips_user_messages(tmp_path):
    p = _write_transcript(tmp_path, [
        {"type": "user", "message": {"id": "u-1", "content": [{"type": "text", "text": "do x"}]}},
        {
            "type": "assistant",
            "message": {"id": "a-1", "content": [{"type": "text", "text": "done"}]},
        },
    ])
    blocks = list(_iter_text_blocks(p))
    assert len(blocks) == 1
    assert blocks[0]["message_id"] == "a-1"


def test_skips_empty_text_blocks(tmp_path):
    p = _write_transcript(tmp_path, [
        {
            "type": "assistant",
            "message": {
                "id": "msg-1",
                "content": [
                    {"type": "text", "text": ""},
                    {"type": "text", "text": "   "},
                    {"type": "text", "text": "real content"},
                ],
            },
        },
    ])
    blocks = list(_iter_text_blocks(p))
    assert len(blocks) == 1
    assert blocks[0]["text"] == "real content"


def test_handles_malformed_lines(tmp_path):
    p = tmp_path / "transcript.jsonl"
    p.write_text(
        '{"type":"assistant","message":{"id":"a-1","content":[{"type":"text","text":"first"}]}}\n'
        'not valid json\n'
        '\n'
        '{"type":"assistant","message":{"id":"a-2","content":[{"type":"text","text":"second"}]}}\n'
    )
    blocks = list(_iter_text_blocks(p))
    assert len(blocks) == 2
    assert [b["text"] for b in blocks] == ["first", "second"]


def test_skips_messages_without_id(tmp_path):
    p = _write_transcript(tmp_path, [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "no id"}]}},
    ])
    blocks = list(_iter_text_blocks(p))
    assert blocks == []


def test_extracts_timestamp(tmp_path):
    p = _write_transcript(tmp_path, [
        {
            "type": "assistant",
            "timestamp": "2026-05-13T09:51:02.278Z",
            "message": {"id": "msg-1", "content": [{"type": "text", "text": "decision"}]},
        },
    ])
    blocks = list(_iter_text_blocks(p))
    assert blocks[0]["created_at"] is not None
    assert blocks[0]["created_at"].year == 2026
    assert blocks[0]["created_at"].month == 5
    assert blocks[0]["created_at"].tzinfo is not None  # has timezone


def test_handles_missing_timestamp(tmp_path):
    p = _write_transcript(tmp_path, [
        {
            "type": "assistant",
            "message": {"id": "msg-1", "content": [{"type": "text", "text": "decision"}]},
        },
    ])
    blocks = list(_iter_text_blocks(p))
    assert blocks[0]["created_at"] is None


def test_position_tracks_line_number(tmp_path):
    p = _write_transcript(tmp_path, [
        {"type": "user", "message": {"id": "u-1", "content": []}},
        {"type": "assistant", "message": {"id": "a-1", "content": [{"type": "text", "text": "line 2"}]}},
        {"type": "user", "message": {"id": "u-2", "content": []}},
        {"type": "assistant", "message": {"id": "a-2", "content": [{"type": "text", "text": "line 4"}]}},
    ])
    blocks = list(_iter_text_blocks(p))
    assert blocks[0]["position"] == 2
    assert blocks[1]["position"] == 4


# --- scan_transcript_stats: model + token recovery -------------------------
# Neither the model nor token totals come from the hook stream reliably, so
# these are derived from the transcript. Each assistant message carries usage
# for its own API call; summing across messages = what was billed.

def _assistant_usage(model, usage):
    return {"type": "assistant", "message": {"model": model, "usage": usage}}


def test_stats_sums_usage_across_messages(tmp_path):
    p = _write_transcript(tmp_path, [
        {"type": "user", "message": {"id": "u", "content": []}},
        _assistant_usage("claude-opus-4-8", {
            "input_tokens": 10, "output_tokens": 100,
            "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 5,
        }),
        _assistant_usage("claude-opus-4-8", {
            "input_tokens": 20, "output_tokens": 200,
            "cache_read_input_tokens": 2000, "cache_creation_input_tokens": 0,
        }),
    ])
    stats = scan_transcript_stats(p)
    assert stats["model"] == "claude-opus-4-8"
    assert stats["input_tokens"] == 30
    assert stats["output_tokens"] == 300
    assert stats["cache_read_tokens"] == 3000
    assert stats["cache_creation_tokens"] == 5
    assert stats["total_tokens"] == 30 + 300 + 3000 + 5
    assert stats["assistant_messages"] == 2


def test_stats_model_is_modal_not_last(tmp_path):
    p = _write_transcript(tmp_path, [
        _assistant_usage("claude-opus-4-8", {"output_tokens": 1}),
        _assistant_usage("claude-opus-4-8", {"output_tokens": 1}),
        _assistant_usage("claude-sonnet-4-6", {"output_tokens": 1}),
    ])
    assert scan_transcript_stats(p)["model"] == "claude-opus-4-8"


def test_stats_ignores_synthetic_model_but_counts_usage(tmp_path):
    p = _write_transcript(tmp_path, [
        _assistant_usage("<synthetic>", {"output_tokens": 7}),
    ])
    stats = scan_transcript_stats(p)
    assert stats["model"] is None
    assert stats["output_tokens"] == 7


def test_stats_missing_usage_keys_default_zero(tmp_path):
    p = _write_transcript(tmp_path, [
        _assistant_usage("claude-opus-4-8", {"output_tokens": 5}),
    ])
    stats = scan_transcript_stats(p)
    assert stats["input_tokens"] == 0
    assert stats["cache_read_tokens"] == 0
    assert stats["total_tokens"] == 5


def test_stats_none_on_missing_file(tmp_path):
    assert scan_transcript_stats(tmp_path / "nope.jsonl") is None


def test_stats_none_when_no_assistant_messages(tmp_path):
    p = _write_transcript(tmp_path, [{"type": "user", "message": {"id": "u", "content": []}}])
    assert scan_transcript_stats(p) is None


def test_stats_skips_malformed_lines(tmp_path):
    p = tmp_path / "transcript.jsonl"
    p.write_text(
        '{"bad json\n'
        + json.dumps(_assistant_usage("claude-opus-4-8", {"output_tokens": 42})) + "\n"
    )
    assert scan_transcript_stats(p)["output_tokens"] == 42


def _usage_line(model, out):
    return {"type": "assistant", "message": {"model": model, "usage": {
        "input_tokens": 0, "output_tokens": out,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}


def _build_session_tree(projects_dir, session_id, root_out, subagents):
    """Lay out a realistic on-disk session: root transcript + subagents/ files
    (and a nested workflows/ agent), mirroring Claude Code's structure."""
    proj = projects_dir / "-Users-someone-repo"
    proj.mkdir(parents=True)
    (proj / f"{session_id}.jsonl").write_text(
        json.dumps(_usage_line("claude-opus-4-8", root_out)) + "\n")
    sub_dir = proj / session_id / "subagents"
    sub_dir.mkdir(parents=True)
    for agent_id, model, out in subagents:
        (sub_dir / f"agent-{agent_id}.jsonl").write_text(
            json.dumps(_usage_line(model, out)) + "\n")
    return proj


def test_session_subagent_transcripts_finds_flat_and_nested(tmp_path):
    sid = "11111111-2222-3333-4444-555555555555"
    proj = _build_session_tree(tmp_path, sid, 100, [("aaa111", "claude-sonnet-4-6", 50)])
    # a workflow agent nested one level deeper
    wf = proj / sid / "subagents" / "workflows" / "wf_abc"
    wf.mkdir(parents=True)
    (wf / "agent-bbb222.jsonl").write_text(
        json.dumps(_usage_line("claude-haiku-4-5-20251001", 7)) + "\n")

    found = dict(session_subagent_transcripts(sid, tmp_path))
    assert set(found) == {"aaa111", "bbb222"}  # both flat and workflow-nested


def test_scan_session_totals_sums_root_plus_subagents(tmp_path):
    sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    _build_session_tree(tmp_path, sid, 100, [
        ("agent1", "claude-sonnet-4-6", 50),
        ("agent2", "claude-haiku-4-5-20251001", 25),
    ])
    scan = scan_session_totals(sid, tmp_path)
    assert scan["root"]["output_tokens"] == 100
    assert scan["subagent_files"] == 2
    assert set(scan["per_agent"]) == {"agent1", "agent2"}
    # Authoritative total = root + both sub-agents (the undercount fix).
    assert scan["totals"]["output_tokens"] == 175


def test_scan_session_totals_missing_session(tmp_path):
    scan = scan_session_totals("no-such-session", tmp_path)
    assert scan["root"] is None
    assert scan["totals"]["total_tokens"] == 0
    assert scan["subagent_files"] == 0


# --- sub-agent transcript attribution -------------------------------------
# Regression: SubagentStop's `transcript_path` is the SESSION ROOT, so
# ingesting it against the agent's invocation_id recorded the root's running
# total as that agent's usage. Siblings stopping together got byte-identical
# numbers and summing the rows multiplied a running total by the fan-out width.

def _session_tree(tmp_path, session_id, agent_ids):
    """Build <proj>/<sid>.jsonl + <proj>/<sid>/subagents/agent-<id>.jsonl."""
    proj = tmp_path / "proj"
    (proj / session_id / "subagents").mkdir(parents=True)
    root = proj / f"{session_id}.jsonl"
    root.write_text(json.dumps(_assistant_usage(
        "claude-opus-4-8", {"output_tokens": 999,
                            "cache_read_input_tokens": 999_000})) + "\n")
    for i, aid in enumerate(agent_ids, start=1):
        p = proj / session_id / "subagents" / f"agent-{aid}.jsonl"
        p.write_text(json.dumps(_assistant_usage(
            "claude-sonnet-4-6", {"output_tokens": i,
                                  "cache_read_input_tokens": i * 10})) + "\n")
    return proj, root


def test_subagent_transcript_resolves_own_file(tmp_path):
    proj, _ = _session_tree(tmp_path, "sid1", ["aaa", "bbb"])
    p = transcripts.subagent_transcript("sid1", "bbb", projects_dir=proj.parent)
    assert p is not None and p.name == "agent-bbb.jsonl"
    assert scan_transcript_stats(p)["output_tokens"] == 2


def test_subagent_transcript_none_when_absent(tmp_path):
    proj, _ = _session_tree(tmp_path, "sid1", ["aaa"])
    assert transcripts.subagent_transcript(
        "sid1", "zzz", projects_dir=proj.parent) is None


def test_each_subagent_gets_distinct_totals(tmp_path):
    """The defect's signature: siblings must NOT share a token tuple."""
    proj, _ = _session_tree(tmp_path, "sid1", ["aaa", "bbb", "ccc"])
    got = []
    for aid in ("aaa", "bbb", "ccc"):
        p = transcripts.subagent_transcript("sid1", aid, projects_dir=proj.parent)
        s = scan_transcript_stats(p)
        got.append((s["cache_read_tokens"], s["output_tokens"]))
    assert got == [(10, 1), (20, 2), (30, 3)]
    assert len(set(got)) == 3, "siblings shared a token tuple"


@pytest.mark.asyncio
async def test_ingest_refuses_root_transcript_for_subagent(tmp_path, caplog):
    """A root transcript attributed to a sub-agent must write NOTHING."""
    proj, root = _session_tree(tmp_path, "sid1", ["aaa"])
    n = await transcripts.ingest(None, "sid1", str(root), "aaa")
    assert n == 0
    assert "refusing to attribute session root" in caplog.text


@pytest.mark.asyncio
async def test_ingest_allows_root_transcript_for_root_invocation(tmp_path):
    """The same path IS legitimate for the root invocation -- no false positive."""
    proj, root = _session_tree(tmp_path, "sid1", ["aaa"])
    with pytest.raises(AttributeError):
        # Gets past the guard and reaches the DB cursor (conn=None) -- which is
        # the assertion: the guard did not short-circuit the root case.
        await transcripts.ingest(None, "sid1", str(root), "root::sid1")


# --- agent result recovery -------------------------------------------------
# Regression: SubagentStop.last_message arrives empty, so 100% of invocation
# rows recorded no result. Schema-validated agents (every Workflow fan-out)
# answer with a StructuredOutput tool_use and leave NO final text block, so
# reading text alone returns nothing for exactly the agents that matter most.

def _assistant_blocks(blocks):
    return {"type": "assistant", "message": {"model": "m", "content": blocks}}


def test_agent_result_prefers_structured_output(tmp_path):
    p = _write_transcript(tmp_path, [
        _assistant_blocks([{"type": "text", "text": "let me check that"}]),
        _assistant_blocks([{"type": "tool_use", "name": "StructuredOutput",
                            "input": {"phone": "555-0100", "verified": True}}]),
    ])
    got = transcripts.agent_result(p)
    assert json.loads(got) == {"phone": "555-0100", "verified": True}


def test_agent_result_falls_back_to_last_text(tmp_path):
    p = _write_transcript(tmp_path, [
        _assistant_blocks([{"type": "text", "text": "first"}]),
        _assistant_blocks([{"type": "text", "text": "final answer"}]),
    ])
    assert transcripts.agent_result(p) == "final answer"


def test_agent_result_ignores_empty_trailing_text(tmp_path):
    """The workflow-agent shape: StructuredOutput then a blank text block."""
    p = _write_transcript(tmp_path, [
        _assistant_blocks([{"type": "tool_use", "name": "StructuredOutput",
                            "input": {"ok": 1}}]),
        _assistant_blocks([{"type": "text", "text": "   "}]),
    ])
    assert json.loads(transcripts.agent_result(p)) == {"ok": 1}


def test_agent_result_none_when_nothing_returned(tmp_path):
    p = _write_transcript(tmp_path, [{"type": "user", "message": {"content": []}}])
    assert transcripts.agent_result(p) is None


def test_agent_result_none_on_missing_file(tmp_path):
    assert transcripts.agent_result(tmp_path / "nope.jsonl") is None
