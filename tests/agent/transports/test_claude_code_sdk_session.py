from unittest.mock import MagicMock

from agent.transports.claude_code_sdk_session import ClaudeCodeSdkTurnSession


class TextBlock:
    """Named to match claude_agent_sdk.types.TextBlock exactly —
    run_turn()'s dispatch keys off type(x).__name__ as a plain string (see
    Step 7's run_turn()), so a `_Fake`-prefixed class name would silently
    never match and this test would exercise nothing."""

    def __init__(self, text):
        self.text = text


class ToolUseBlock:
    """Named to match claude_agent_sdk.types.ToolUseBlock exactly — see
    TextBlock's docstring above for why the class name (not a `_Fake`
    prefix) matters for run_turn()'s type(x).__name__ dispatch."""

    def __init__(self, tool_use_id, name="tool", input=None):
        self.id = tool_use_id
        self.name = name
        self.input = input or {}


class ToolResultBlock:
    """Named to match claude_agent_sdk.types.ToolResultBlock exactly —
    see TextBlock's docstring above."""

    def __init__(self, tool_use_id, content=None):
        self.tool_use_id = tool_use_id
        self.content = content


class AssistantMessage:
    def __init__(self, content):
        self.content = content


class ResultMessage:
    def __init__(self):
        self.usage = {"inputTokens": 10, "outputTokens": 5}
        self.model_usage = None
        self.total_cost_usd = 0.001
        self.is_error = False


def test_run_turn_sends_input_and_collects_final_text():
    fake_client = MagicMock()
    fake_client.is_alive.return_value = True
    events = [
        {"type": "raw_message", "message": AssistantMessage(
            content=[TextBlock("assembled final text")]
        )},
        {"type": "raw_message", "message": ResultMessage()},
    ]
    fake_client.take_event.side_effect = lambda timeout=0.0: (
        events.pop(0) if events else None
    )

    session = ClaudeCodeSdkTurnSession(client_factory=lambda **kw: fake_client)
    result = session.run_turn(user_input="hello", turn_timeout=2.0)

    fake_client.start.assert_called_once()
    fake_client.send_turn.assert_called_once_with("hello", session_id="default")
    assert result.final_text == "assembled final text"
    assert result.error is None
    assert result.should_retire is False


def test_run_turn_marks_retire_on_transport_error():
    fake_client = MagicMock()
    fake_client.is_alive.return_value = True
    fake_client.take_event.side_effect = [
        {"type": "transport_error", "error": "boom"},
        None,
    ]

    session = ClaudeCodeSdkTurnSession(client_factory=lambda **kw: fake_client)
    result = session.run_turn(user_input="hello", turn_timeout=2.0)

    assert result.error == "boom"
    assert result.should_retire is True


def test_run_turn_counts_one_tool_iteration_per_completed_tool_call():
    """One ToolUseBlock (start) + its matching ToolResultBlock (completion)
    is a single tool call round-trip and must increment tool_iterations
    exactly once — not once per block. Regression test for a review
    finding where run_turn() incremented on both ToolUseBlock and
    ToolResultBlock, double-counting every tool call."""
    fake_client = MagicMock()
    fake_client.is_alive.return_value = True
    events = [
        {"type": "raw_message", "message": AssistantMessage(
            content=[ToolUseBlock("tool-1", name="Bash", input={"command": "ls"})]
        )},
        {"type": "raw_message", "message": AssistantMessage(
            content=[ToolResultBlock("tool-1", content="ok")]
        )},
        {"type": "raw_message", "message": AssistantMessage(
            content=[TextBlock("done")]
        )},
        {"type": "raw_message", "message": ResultMessage()},
    ]
    fake_client.take_event.side_effect = lambda timeout=0.0: (
        events.pop(0) if events else None
    )

    session = ClaudeCodeSdkTurnSession(client_factory=lambda **kw: fake_client)
    result = session.run_turn(user_input="hello", turn_timeout=2.0)

    assert result.tool_iterations == 1
    assert result.final_text == "done"


def test_run_turn_projects_tool_call_then_result_then_final_text():
    """The projected_messages list is what run_claude_code_sdk_turn()
    splices into Hermes' own messages list (agent/claude_code_runtime.py's
    messages.extend(turn.projected_messages)) — this is what actually
    persists the assistant's turn to Hermes' session DB / transcript.
    Regression test for a bug where projected_messages was declared on
    TurnResult but never populated, so every assistant turn silently
    vanished from Hermes' own history despite streaming fine to the user."""
    fake_client = MagicMock()
    fake_client.is_alive.return_value = True
    events = [
        {"type": "raw_message", "message": AssistantMessage(
            content=[ToolUseBlock("tool-1", name="Bash", input={"command": "ls"})]
        )},
        {"type": "raw_message", "message": AssistantMessage(
            content=[ToolResultBlock("tool-1", content="ok")]
        )},
        {"type": "raw_message", "message": AssistantMessage(
            content=[TextBlock("echo: hello")]
        )},
        {"type": "raw_message", "message": ResultMessage()},
    ]
    fake_client.take_event.side_effect = lambda timeout=0.0: (
        events.pop(0) if events else None
    )

    session = ClaudeCodeSdkTurnSession(client_factory=lambda **kw: fake_client)
    result = session.run_turn(user_input="hello", turn_timeout=2.0)

    assert result.projected_messages == [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "tool-1",
                    "type": "function",
                    "function": {
                        "name": "Bash",
                        "arguments": '{"command": "ls"}',
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tool-1", "content": "ok"},
        {"role": "assistant", "content": "echo: hello"},
    ]


def test_run_turn_projects_list_content_tool_results_using_bridge_logic():
    """ToolResultBlock.content can be a list of dict parts (matching the
    real claude-agent-sdk shape) — the stringification must match
    agent.claude_code_runtime's _fire_tool_completed exactly, joining parts
    by `.get("text", part)`, not a subtly different re-implementation."""
    fake_client = MagicMock()
    fake_client.is_alive.return_value = True
    events = [
        {"type": "raw_message", "message": AssistantMessage(
            content=[ToolUseBlock("tool-1", name="Read", input={"path": "f"})]
        )},
        {"type": "raw_message", "message": AssistantMessage(
            content=[ToolResultBlock(
                "tool-1",
                content=[{"type": "text", "text": "line one"}, "line two"],
            )]
        )},
        {"type": "raw_message", "message": ResultMessage()},
    ]
    fake_client.take_event.side_effect = lambda timeout=0.0: (
        events.pop(0) if events else None
    )

    session = ClaudeCodeSdkTurnSession(client_factory=lambda **kw: fake_client)
    result = session.run_turn(user_input="hello", turn_timeout=2.0)

    tool_msg = result.projected_messages[1]
    assert tool_msg == {
        "role": "tool",
        "tool_call_id": "tool-1",
        "content": "line one\nline two",
    }
    # A tool-only turn (no final TextBlock) must NOT add a bogus empty
    # trailing assistant message.
    assert result.projected_messages[-1] == tool_msg


def test_run_turn_text_only_turn_projects_single_final_assistant_message():
    """A turn with no tool calls at all should produce projected_messages
    containing exactly one entry: the final assistant text message."""
    fake_client = MagicMock()
    fake_client.is_alive.return_value = True
    events = [
        {"type": "raw_message", "message": AssistantMessage(
            content=[TextBlock("just a text reply")]
        )},
        {"type": "raw_message", "message": ResultMessage()},
    ]
    fake_client.take_event.side_effect = lambda timeout=0.0: (
        events.pop(0) if events else None
    )

    session = ClaudeCodeSdkTurnSession(client_factory=lambda **kw: fake_client)
    result = session.run_turn(user_input="hello", turn_timeout=2.0)

    assert result.projected_messages == [
        {"role": "assistant", "content": "just a text reply"}
    ]


def test_request_interrupt_calls_client_interrupt():
    fake_client = MagicMock()
    session = ClaudeCodeSdkTurnSession(client_factory=lambda **kw: fake_client)
    session.ensure_started()

    session.request_interrupt()

    fake_client.interrupt.assert_called_once()


def test_request_interrupt_is_a_noop_before_start():
    session = ClaudeCodeSdkTurnSession(client_factory=lambda **kw: MagicMock())
    session.request_interrupt()  # must not raise
