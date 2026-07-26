# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tool-name allowlist for PreToolUse/PostToolUse capture.

Keeps volume sane by skipping Glob/Grep/TodoWrite/etc.

`Read` and `Skill` are captured deliberately, despite the volume: they are the only
record of *which context was actually loaded* into a run. Without them you can see what
an agent did but never which instruction, memory, or skill it did it from — so questions
like "which of these 92 skills is ever used" and "does loading REVIEW.md change how a run
goes" have no data behind them. That is the knowledge-effect attribution gap.

Glob/Grep stay out: they are higher-volume still and name a *pattern*, not a specific
artifact, so they can't be attributed to a piece of context.
"""
import re

CAPTURE_TOOLS = {
    "Agent",
    "Bash",
    "Edit",
    "Write",
    "WebFetch",
    "WebSearch",
    "Read",   # attribution: file_path == the context artifact loaded
    "Skill",  # attribution: which skill a run actually invoked
}
CAPTURE_TOOL_PATTERNS = [re.compile(r"^mcp__")]


def should_capture(tool_name: str | None) -> bool:
    if not tool_name:
        return False
    if tool_name in CAPTURE_TOOLS:
        return True
    return any(p.search(tool_name) for p in CAPTURE_TOOL_PATTERNS)
