"""Pydantic models for Claude Code hook event payloads.

We accept a permissive base envelope (extra=allow) because Claude Code
may add fields over time. Routing is done by hook_event_name.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class HookEnvelope(BaseModel):
    """Common fields present on every hook event payload.

    Be permissive: Claude Code may add fields and may change shapes between
    versions. We capture and route on hook_event_name + a small set of typed
    fields. Everything else flows through via `extra=allow`.
    """
    model_config = ConfigDict(extra="allow")

    hook_event_name: str
    session_id: str
    transcript_path: str | None = None
    cwd: str | None = None
    permission_mode: Any = None
    effort: Any = None
    agent_id: str | None = None
    agent_type: str | None = None


class SessionStart(HookEnvelope):
    hook_event_name: Literal["SessionStart"]
    source: str | None = None
    model: str | None = None


class UserPromptSubmit(HookEnvelope):
    hook_event_name: Literal["UserPromptSubmit"]
    prompt: str = ""


class PreToolUse(HookEnvelope):
    hook_event_name: Literal["PreToolUse"]
    tool_name: str
    tool_use_id: str
    tool_input: dict[str, Any] = Field(default_factory=dict)


class PostToolUse(HookEnvelope):
    hook_event_name: Literal["PostToolUse"]
    tool_name: str
    tool_use_id: str
    tool_input: dict[str, Any] = Field(default_factory=dict)
    tool_response: Any = None


class PostToolUseFailure(HookEnvelope):
    hook_event_name: Literal["PostToolUseFailure"]
    tool_name: str
    tool_use_id: str
    tool_input: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class SubagentStart(HookEnvelope):
    hook_event_name: Literal["SubagentStart"]
    agent_id: str
    agent_type: str
    prompt: str | None = None


class SubagentStop(HookEnvelope):
    hook_event_name: Literal["SubagentStop"]
    agent_id: str
    agent_type: str | None = None
    last_message: str | None = None


class Stop(HookEnvelope):
    """Fires when the root agent finishes a turn. We use it to incrementally
    ingest assistant text blocks from the transcript file."""
    hook_event_name: Literal["Stop"]
    stop_hook_active: bool | None = None


class SessionEnd(HookEnvelope):
    hook_event_name: Literal["SessionEnd"]
    reason: str | None = None
    total_tokens: int | None = None


HOOK_MODELS: dict[str, type[HookEnvelope]] = {
    "SessionStart": SessionStart,
    "UserPromptSubmit": UserPromptSubmit,
    "PreToolUse": PreToolUse,
    "PostToolUse": PostToolUse,
    "PostToolUseFailure": PostToolUseFailure,
    "SubagentStart": SubagentStart,
    "SubagentStop": SubagentStop,
    "Stop": Stop,
    "SessionEnd": SessionEnd,
}


def parse_event(raw: dict) -> HookEnvelope:
    """Route a raw hook payload to its typed model. Falls back to base envelope."""
    name = raw.get("hook_event_name")
    cls = HOOK_MODELS.get(name, HookEnvelope)
    return cls.model_validate(raw)
