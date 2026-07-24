from unittest.mock import MagicMock

from agent.claude_code_runtime import _record_claude_code_sdk_usage


class _FakeResultMessage:
    def __init__(self, usage):
        self.usage = usage
        self.model_usage = None
        self.total_cost_usd = None
        self.is_error = False


def test_records_usage_with_cache_write_tokens_unlike_codex():
    agent = MagicMock()
    agent.session_prompt_tokens = 0
    agent.session_completion_tokens = 0
    agent.session_total_tokens = 0
    agent.session_input_tokens = 0
    agent.session_output_tokens = 0
    agent.session_cache_read_tokens = 0
    agent.session_cache_write_tokens = 0
    agent.session_reasoning_tokens = 0
    agent.session_api_calls = 0
    agent._session_db = None
    agent.model = "claude-sonnet-5"
    agent.provider = "anthropic"
    agent.base_url = ""

    turn = MagicMock()
    turn.result_message = _FakeResultMessage(
        usage={
            "inputTokens": 100,
            "outputTokens": 50,
            "cacheReadInputTokens": 20,
            "cacheCreationInputTokens": 30,
        }
    )

    usage_dict = _record_claude_code_sdk_usage(agent, turn)

    assert agent.session_api_calls == 1
    assert agent.session_input_tokens == 100
    assert agent.session_output_tokens == 50
    assert agent.session_cache_read_tokens == 20
    # Key behavioral difference from _record_codex_app_server_usage: Claude
    # DOES report cache-write tokens, so this must NOT be zeroed.
    assert agent.session_cache_write_tokens == 30
    assert usage_dict["cache_write_tokens"] == 30


def test_counts_api_call_even_when_result_message_is_none():
    agent = MagicMock()
    agent.session_api_calls = 0
    agent._session_db = None

    turn = MagicMock()
    turn.result_message = None

    usage_dict = _record_claude_code_sdk_usage(agent, turn)

    assert agent.session_api_calls == 1
    assert usage_dict == {}
