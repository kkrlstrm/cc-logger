"""Tests for transcripts._iter_text_blocks.

DB-side `ingest()` is exercised by the harness; here we just unit-test the
JSONL-parsing logic.
"""
import json
from pathlib import Path

from cc_logger.transcripts import _iter_text_blocks


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
