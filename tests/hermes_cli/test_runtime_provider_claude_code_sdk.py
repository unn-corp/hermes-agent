from agent.credential_pool import PooledCredential
from hermes_cli.runtime_provider import (
    _VALID_API_MODES,
    _maybe_apply_claude_code_sdk_runtime,
    _resolve_runtime_from_pool_entry,
)


def test_claude_code_sdk_is_a_valid_api_mode():
    assert "claude_code_sdk" in _VALID_API_MODES


def test_maybe_apply_claude_code_sdk_runtime_noop_when_unset():
    result = _maybe_apply_claude_code_sdk_runtime(
        provider="anthropic", api_mode="anthropic_messages", model_cfg={}
    )
    assert result == "anthropic_messages"


def test_maybe_apply_claude_code_sdk_runtime_noop_for_wrong_provider():
    result = _maybe_apply_claude_code_sdk_runtime(
        provider="openai",
        api_mode="chat_completions",
        model_cfg={"anthropic_runtime": "claude_code_sdk"},
    )
    assert result == "chat_completions"


def test_maybe_apply_claude_code_sdk_runtime_rewrites_when_enabled():
    result = _maybe_apply_claude_code_sdk_runtime(
        provider="anthropic",
        api_mode="anthropic_messages",
        model_cfg={"anthropic_runtime": "claude_code_sdk"},
    )
    assert result == "claude_code_sdk"


def test_maybe_apply_claude_code_sdk_runtime_handles_none_config():
    result = _maybe_apply_claude_code_sdk_runtime(
        provider="anthropic", api_mode="anthropic_messages", model_cfg=None
    )
    assert result == "anthropic_messages"


def _make_anthropic_entry() -> PooledCredential:
    return PooledCredential(
        provider="anthropic",
        id="entry-1",
        label="test",
        auth_type="api_key",
        priority=0,
        source="manual",
        access_token="sk-ant-stub",
    )


def test_resolve_runtime_picks_claude_code_sdk_when_enabled():
    resolved = _resolve_runtime_from_pool_entry(
        provider="anthropic",
        entry=_make_anthropic_entry(),
        requested_provider="anthropic",
        model_cfg={"anthropic_runtime": "claude_code_sdk", "default": "claude-sonnet-5"},
    )
    assert resolved["api_mode"] == "claude_code_sdk"


def test_resolve_runtime_keeps_anthropic_messages_when_runtime_unset():
    resolved = _resolve_runtime_from_pool_entry(
        provider="anthropic",
        entry=_make_anthropic_entry(),
        requested_provider="anthropic",
        model_cfg={"default": "claude-sonnet-5"},
    )
    assert resolved["api_mode"] == "anthropic_messages"
