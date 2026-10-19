# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for transcripts.iter_peer_events (cross-session message parsing).

Record shapes match real Claude Code 2.1.280 transcripts; values are made up.
"""
import json
from pathlib import Path

from cc_logger.transcripts import iter_peer_events

SOCK = "uds:/tmp/cc-socks/12345.sock"
MSG_ID = "00000000-0000-4000-8000-000000000001"


def _write(tmp_path: Path, records: list[dict]) -> Path:
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return p


def _origin(**extra) -> dict:
    return {"kind": "peer", "from": SOCK, "verifiedPeerPid": 12345, "msg_id": MSG_ID,
            "name": "repo-3b", "fromMode": "prompting", "body": "not deploying", **extra}


def test_message_arriving_while_idle_is_a_meta_user_record(tmp_path):
    p = _write(tmp_path, [{
        "type": "user", "isMeta": True, "timestamp": "2026-10-13T12:00:03Z",
        "message": {"role": "user", "content": "<cross-session-message ...>"},
        "origin": _origin(hopChain=["hop-0001"]),
    }])
    [ev] = list(iter_peer_events(p))
    assert ev["event"] == "received"
    assert ev["msg_id"] == MSG_ID
    assert ev["delivery"] == "new_turn"
    assert ev["sender_address"] == SOCK
    assert ev["sender_name"] == "repo-3b"
    assert ev["hop_chain"] == ["hop-0001"]
    assert ev["body"] == "not deploying"


def test_message_arriving_mid_turn_is_a_queued_attachment(tmp_path):
    """Real transcripts store some messages only this way; a parser reading
    top-level `origin` alone misses them."""
    p = _write(tmp_path, [
        {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-10-13T12:00:08Z",
         "content": "<cross-session-message from=\"uds:...\">not deploying</cross-session-message>"},
        {"type": "attachment", "timestamp": "2026-10-13T12:00:08Z",
         "attachment": {"type": "queued_command", "prompt": "<cross-session-message ...>",
                        "commandMode": "prompt", "origin": _origin()}},
    ])
    [ev] = list(iter_peer_events(p))
    assert ev["event"] == "received"
    assert ev["msg_id"] == MSG_ID
    assert ev["delivery"] == "mid_turn"


def test_subagent_handbacks_and_background_subagents_are_not_cross_session(tmp_path):
    """Both share origin.kind == 'peer'. Counting them makes sub-agent traffic look like
    cross-session traffic."""
    p = _write(tmp_path, [
        {"type": "user", "isMeta": True, "timestamp": "2026-09-25T10:00:00Z",
         "origin": {"kind": "peer", "from": "a1111111111111111", "senderTaskId": "a1111111111111111",
                    "body": "[Subagent hand-back] ...", "handback": True}},
        {"type": "user", "isMeta": True, "timestamp": "2026-09-25T10:00:00Z",
         "origin": {"kind": "peer", "from": "a2222222222222222", "senderTaskId": "a2222222222222222",
                    "name": "general-purpose", "body": "progress update"}},
    ])
    assert list(iter_peer_events(p)) == []


def test_cross_session_send_carries_msg_id_in_its_result(tmp_path):
    p = _write(tmp_path, [
        {"type": "assistant", "timestamp": "2026-10-13T12:00:06Z",
         "message": {"content": [{"type": "tool_use", "id": "toolu_1", "name": "SendMessage",
                                  "input": {"to": SOCK, "summary": "Reply", "message": "not deploying",
                                            "notify_when_idle": True}}]}},
        {"type": "user", "timestamp": "2026-10-13T12:00:07Z",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "..."}]},
         "toolUseResult": {"success": True, "message": "Reply -> uds:... queued there",
                           "msg_id": MSG_ID}},
    ])
    [ev] = list(iter_peer_events(p))
    assert ev["event"] == "sent"
    assert ev["msg_id"] == MSG_ID
    assert ev["addressed_to"] == SOCK
    assert ev["summary"] == "Reply"
    assert ev["notify_when_idle"] is True
    assert ev["send_tool_use_id"] == "toolu_1"


def test_send_to_a_subagent_has_no_msg_id_and_is_skipped(tmp_path):
    p = _write(tmp_path, [
        {"type": "assistant", "timestamp": "2026-10-01T10:00:00Z",
         "message": {"content": [{"type": "tool_use", "id": "toolu_2", "name": "SendMessage",
                                  "input": {"to": "researcher", "message": "continue"}}]}},
        {"type": "user", "timestamp": "2026-10-01T10:00:01Z",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "toolu_2", "content": "ok"}]},
         "toolUseResult": {"success": True, "message": "delivered to researcher"}},
    ])
    assert list(iter_peer_events(p)) == []


def test_idle_notice_is_parsed_from_queue_operation_and_meta_record(tmp_path):
    text = ("[Cross-session idle notice] \"repo-3b\", which you asked to be notified "
            "about, is idle now — it finished a turn at 14:21. Its harness reports: «done».")
    p = _write(tmp_path, [
        {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-10-13T12:00:16Z",
         "content": text},
        {"type": "user", "isMeta": True, "timestamp": "2026-10-13T12:00:58Z",
         "message": {"role": "user", "content": text}},
    ])
    evs = list(iter_peer_events(p))
    assert [e["event"] for e in evs] == ["notice", "notice"]  # caller dedupes on text
    assert evs[0]["kind"] == "idle"
    assert evs[0]["about_name"] == "repo-3b"


def test_notice_text_inside_a_tool_result_is_not_a_notice(tmp_path):
    """The SendMessage tool result and tool descriptions quote the notice tags."""
    p = _write(tmp_path, [
        {"type": "user", "timestamp": "2026-10-13T12:00:06Z",
         "message": {"content": [{"type": "tool_result", "tool_use_id": "x",
                                  "content": "a [Cross-session delivery notice] follows if held"}]}},
    ])
    assert list(iter_peer_events(p)) == []


def test_received_message_without_msg_id_gets_a_stable_synthetic_id(tmp_path):
    origin = _origin()
    del origin["msg_id"]
    rec = {"type": "user", "isMeta": True, "timestamp": "2026-10-01T10:00:00Z", "origin": origin}
    a = list(iter_peer_events(_write(tmp_path, [rec])))[0]["msg_id"]
    b = list(iter_peer_events(_write(tmp_path, [rec])))[0]["msg_id"]
    assert a == b and a.startswith("nomsgid:")


def test_installer_adds_peer_address_command_hook_and_recognizes_it_as_ours():
    import importlib.util
    import sys

    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / "src"))
    spec = importlib.util.spec_from_file_location("ih", root / "scripts" / "install-hooks.py")
    ih = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ih)
    block = ih._build_hooks_block(8787)
    commands = [h for e in block["SessionStart"] for h in e["hooks"] if h["type"] == "command"]
    assert len(commands) == 1 and "peer-address-hook.py" in commands[0]["command"]
    assert ih._strip_ours(block["SessionStart"], 8787) == ([], 2)


def test_reinstall_keeps_another_tools_hook_that_shares_our_entry():
    """Another tool's SessionStart command sat in the same entry as our HTTP hook;
    filtering whole entries silently uninstalled it."""
    import importlib.util
    import sys

    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / "src"))
    spec = importlib.util.spec_from_file_location("ih", root / "scripts" / "install-hooks.py")
    ih = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ih)
    other = {"type": "command", "command": "python3 -m othertool.hooks session-start"}
    shared = [{"hooks": [{"type": "http", "url": "http://127.0.0.1:8787/hook", "timeout": 5}, other]}]
    kept, removed = ih._strip_ours(shared, 8787)
    assert removed == 1
    assert kept == [{"hooks": [other]}]


def test_peer_hook_command_quotes_interpreter_and_script_paths():
    """Windows paths can contain spaces; the command must survive Git Bash and cmd."""
    import importlib.util
    import sys

    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / "src"))
    spec = importlib.util.spec_from_file_location("ih", root / "scripts" / "install-hooks.py")
    ih = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ih)
    cmd = ih._peer_hook(8787)["command"]
    assert cmd.startswith('"') and f'"{ih.PEER_HOOK_SCRIPT}"' in cmd
