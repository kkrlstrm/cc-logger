"""Tool-name allowlist for PreToolUse/PostToolUse capture.

Keeps volume sane by skipping Read/Glob/Grep/TodoWrite/etc.
"""
import re

CAPTURE_TOOLS = {"Agent", "Bash", "Edit", "Write", "WebFetch", "WebSearch"}
CAPTURE_TOOL_PATTERNS = [re.compile(r"^mcp__")]


def should_capture(tool_name: str | None) -> bool:
    if not tool_name:
        return False
    if tool_name in CAPTURE_TOOLS:
        return True
    return any(p.search(tool_name) for p in CAPTURE_TOOL_PATTERNS)
