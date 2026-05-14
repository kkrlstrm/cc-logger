from cc_logger.filters import should_capture


def test_known_captured_tools():
    for name in ["Agent", "Bash", "Edit", "Write", "WebFetch", "WebSearch"]:
        assert should_capture(name)


def test_known_skipped_tools():
    for name in ["Read", "Glob", "Grep", "TodoWrite", "NotebookEdit"]:
        assert not should_capture(name)


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
