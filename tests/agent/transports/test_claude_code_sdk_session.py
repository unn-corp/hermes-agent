from unittest.mock import MagicMock

from agent.transports.claude_code_sdk_session import ClaudeCodeSdkTurnSession


class TextBlock:
    """Named to match claude_agent_sdk.types.TextBlock exactly —
    run_turn()'s dispatch keys off type(x).__name__ as a plain string (see
    Step 7's run_turn()), so a `_Fake`-prefixed class name would silently
    never match and this test would exercise nothing."""

    def __init__(self, text):
        self.text = text


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
