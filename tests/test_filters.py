# Copyright (C) 2026 Kai Karlstrom
# SPDX-License-Identifier: AGPL-3.0-or-later
from cc_logger.filters import CAPTURE_TOOLS, should_capture


def test_known_captured_tools():
    for name in ["Agent", "Bash", "Edit", "Write", "WebFetch", "WebSearch"]:
        assert should_capture(name)


def test_context_attribution_tools_are_captured():
    """`Read` and `Skill` are the only record of which context a run loaded.

    Without them you can see what an agent did but never which instruction, memory, or
    skill it did it from — so "which of my skills is ever used?" and "does loading this
    doc change how a run goes?" have no data behind them. Captured despite the volume.
    """
    assert should_capture("Read")
    assert should_capture("Skill")


def test_known_skipped_tools():
    """Glob/Grep name a *pattern*, not a specific artifact, so they can't be attributed
    to a piece of context — and they are higher-volume than Read. TodoWrite and
    NotebookEdit carry no analytical signal."""
    for name in ["Glob", "Grep", "TodoWrite", "NotebookEdit"]:
        assert not should_capture(name)


def test_capture_set_is_explicit():
    """The allowlist is the documented contract (docs/HOOKS.md) and must match the
    `matcher` lines in settings.json. Pinned so a drive-by edit is visible in review."""
    assert CAPTURE_TOOLS == {
        "Agent",
        "Bash",
        "Edit",
        "Write",
        "WebFetch",
        "WebSearch",
        "Read",
        "Skill",
    }


def test_mcp_prefix_matches():
    assert should_capture("mcp__Neon__list_projects")
    assert should_capture("mcp__claude_ai_Notion__authenticate")
    assert should_capture("mcp__Anything__at_all")


def test_empty_and_none():
    assert not should_capture("")
    assert not should_capture(None)


def test_unknown_tool():
    assert not should_capture("SomeRandomTool")
    assert not should_capture("__mcp_backwards")  # mcp prefix must be at start


def test_matching_is_exact_not_substring():
    """A tool merely containing a captured name is a different surface."""
    assert not should_capture("ReadNotebook")
    assert not should_capture("BashOutput")
    assert not should_capture("SkillJockey")


def test_matcher_sources_agree_with_the_capture_allowlist():
    """The matcher and the server-side allowlist are ONE fact stored in three places:
    `CAPTURE_TOOLS`, the installer's `TOOL_MATCHERS`, and examples/settings-hooks.json.

    Drift between them fails silently and invisibly — the hook simply never fires for a
    tool you believe you're capturing, and the data is just quietly absent. That is how
    `Read` was "captured" by the allowlist while zero Read rows existed. Pinned here so
    the next edit has to update all three.
    """
    import importlib.util
    import json
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    expected = "|".join(sorted(CAPTURE_TOOLS))

    sys.path.insert(0, str(root / "src"))
    spec = importlib.util.spec_from_file_location("ih", root / "scripts" / "install-hooks.py")
    ih = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ih)
    assert ih.TOOL_MATCHERS[0] == expected, "installer matcher drifted from CAPTURE_TOOLS"
    assert ih.TOOL_MATCHERS[1] == "mcp__.*"

    example = json.loads((root / "examples" / "settings-hooks.json").read_text())
    named = {
        block["matcher"]
        for event in example["hooks"].values()
        for block in event
        if "matcher" in block and not block["matcher"].startswith("mcp__")
    }
    assert named == {expected}, f"examples/settings-hooks.json drifted: {named}"
