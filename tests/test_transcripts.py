# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for transcripts._iter_text_blocks.

DB-side `ingest()` is exercised by the harness; here we just unit-test the
JSONL-parsing logic.
"""
import json
from pathlib import Path

from cc_logger.transcripts import _iter_text_blocks, scan_transcript_stats


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
