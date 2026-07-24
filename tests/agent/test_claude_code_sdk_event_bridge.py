from unittest.mock import MagicMock

from agent.claude_code_runtime import make_claude_code_sdk_event_bridge


class ToolUseBlock:
    """Named to match claude_agent_sdk.types.ToolUseBlock exactly — the
    bridge's dispatch keys off type(block).__name__ as a plain string (see
    Step 3's on_event()), specifically so this module never needs to import
    the real SDK types at module scope. A fake class named e.g.
    `_FakeToolUseBlock` would silently defeat that dispatch in tests (its
    __name__ would never equal "ToolUseBlock"), so these test doubles use
    the real SDK class names verbatim instead of a `_Fake`-prefixed alias."""

    def __init__(self, id, name, input):
        self.id = id
        self.name = name
        self.input = input


class ToolResultBlock:
    def __init__(self, tool_use_id, content, is_error=False):
        self.tool_use_id = tool_use_id
        self.content = content
        self.is_error = is_error


class TextBlock:
    def __init__(self, text):
        self.text = text


class AssistantMessage:
    def __init__(self, content):
        self.content = content
        self.parent_tool_use_id = None


def test_bridge_fires_tool_progress_for_tool_use_block():
    agent = MagicMock()
    bridge = make_claude_code_sdk_event_bridge(agent)

    message = AssistantMessage(
        content=[ToolUseBlock(id="tu_1", name="Bash", input={"command": "ls"})]
    )
    bridge({"type": "raw_message", "message": message})

    agent.tool_progress_callback.assert_any_call(
        "tool.started", "Bash", "ls", {"command": "ls"}
    )
    agent.tool_start_callback.assert_any_call(
        "tu_1", "Bash", {"command": "ls"}
    )


def test_bridge_fires_tool_completed_for_tool_result_block():
    agent = MagicMock()
    bridge = make_claude_code_sdk_event_bridge(agent)

    started = AssistantMessage(
        content=[ToolUseBlock(id="tu_2", name="Read", input={"file_path": "/x"})]
    )
    bridge({"type": "raw_message", "message": started})

    completed = AssistantMessage(
        content=[ToolResultBlock(tool_use_id="tu_2", content="file contents", is_error=False)]
    )
    bridge({"type": "raw_message", "message": completed})

    agent.tool_progress_callback.assert_any_call(
        "tool.completed", "Read", None, None,
        duration=None, is_error=False, result="file contents",
    )
    agent.tool_complete_callback.assert_any_call(
        "tu_2", "Read", {"file_path": "/x"}, "file contents",
    )


def test_bridge_fires_stream_delta_for_text_block():
    agent = MagicMock()
    bridge = make_claude_code_sdk_event_bridge(agent)

    message = AssistantMessage(content=[TextBlock(text="hello there")])
    bridge({"type": "raw_message", "message": message})

    agent._fire_stream_delta.assert_called_once_with("hello there")


def test_bridge_never_raises_on_callback_exception():
    agent = MagicMock()
    agent.tool_progress_callback.side_effect = RuntimeError("boom")
    agent.tool_start_callback.side_effect = RuntimeError("boom")
    bridge = make_claude_code_sdk_event_bridge(agent)

    message = AssistantMessage(
        content=[ToolUseBlock(id="tu_3", name="Bash", input={})]
    )
    bridge({"type": "raw_message", "message": message})  # must not raise
