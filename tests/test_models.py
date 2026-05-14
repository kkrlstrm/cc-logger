from cc_logger.models import parse_event, HookEnvelope, SessionStart, PreToolUse, PostToolUse


def test_parse_session_start():
    raw = {
        "hook_event_name": "SessionStart",
        "session_id": "abc",
        "cwd": "/tmp",
        "model": "claude-opus-4-7",
    }
    ev = parse_event(raw)
    assert isinstance(ev, SessionStart)
    assert ev.session_id == "abc"


def test_parse_pretooluse():
    raw = {
        "hook_event_name": "PreToolUse",
        "session_id": "abc",
        "tool_name": "Bash",
        "tool_use_id": "tu1",
        "tool_input": {"command": "echo hi"},
    }
    ev = parse_event(raw)
    assert isinstance(ev, PreToolUse)
    assert ev.tool_input == {"command": "echo hi"}


def test_unknown_event_falls_back_to_envelope():
    raw = {
        "hook_event_name": "SomeNewEvent",
        "session_id": "abc",
    }
    ev = parse_event(raw)
    # Falls back to base envelope, does not raise
    assert isinstance(ev, HookEnvelope)
    assert ev.hook_event_name == "SomeNewEvent"


def test_permissive_effort_field():
    # Claude Code emits `effort` as a dict like {"level": "xhigh"}.
    # Our models use Any so this must parse without error.
    raw = {
        "hook_event_name": "PostToolUse",
        "session_id": "abc",
        "tool_name": "Bash",
        "tool_use_id": "tu1",
        "tool_input": {},
        "tool_response": {"stdout": "ok"},
        "effort": {"level": "xhigh"},
    }
    ev = parse_event(raw)
    assert isinstance(ev, PostToolUse)


def test_extra_fields_allowed():
    raw = {
        "hook_event_name": "SessionStart",
        "session_id": "abc",
        "some_new_field": "future_proof",
        "nested_extra": {"a": 1},
    }
    ev = parse_event(raw)
    # extra="allow" means new fields survive on the model
    assert isinstance(ev, SessionStart)
