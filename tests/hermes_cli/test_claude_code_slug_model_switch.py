"""switch_model must accept the synthetic per-subscription slug.

Desktop's composer picker routes a switch WITH a live session through the
gateway's config.set → switch_model (the /model path), not through
/api/model/set. That path has its own provider validation, so selecting a
second model from a Claude Code section failed with

    Unknown provider 'claude-code:work'

and — because selectModel rolls back on error — the picker silently snapped
back to the previous model with nothing persisted.
"""
from __future__ import annotations

import pytest

from hermes_cli.model_switch import switch_model


def _switch(provider: str, model: str = "claude-haiku-4-5-20251001"):
    return switch_model(
        raw_input=model,
        current_provider="anthropic",
        current_model="claude-sonnet-5",
        explicit_provider=provider,
        user_providers={},
        custom_providers=[],
    )


def test_synthetic_slug_is_accepted():
    result = _switch("claude-code:work")
    assert result.success, result.error_message
    assert "Unknown provider" not in (result.error_message or "")


def test_synthetic_slug_resolves_to_anthropic():
    result = _switch("claude-code:work")
    assert result.target_provider.lower() == "anthropic"


def test_selected_model_survives_the_translation():
    result = _switch("claude-code:work", model="claude-haiku-4-5-20251001")
    assert "haiku" in result.new_model.lower(), result.new_model


def test_real_provider_still_works():
    result = _switch("anthropic")
    assert result.success, result.error_message


def test_unknown_provider_still_errors():
    result = _switch("definitely-not-a-provider")
    assert not result.success
    assert "Unknown provider" in result.error_message


class TestRuntimeSurvivesTheSwitch:
    """A /model switch must not silently drop the Claude Code runtime.

    switch_model recomputes api_mode from (provider, base_url) via
    determine_api_mode, which knows nothing about model.anthropic_runtime. So
    an in-place switch flipped a session off the CLI and back onto the
    Messages API — observed live as

        Streaming failed … base_url=https://api.anthropic.com
        400 "You're out of extra usage. Add more at claude.ai/settings/usage"

    i.e. it stopped using the subscription and started billing OAuth extra
    usage, which is the exact opposite of what enabling the runtime is for.
    """

    def test_anthropic_switch_keeps_claude_code_sdk(self, monkeypatch):
        import hermes_cli.config as cfgmod

        monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: {
            "model": {
                "default": "claude-sonnet-5",
                "provider": "anthropic",
                "anthropic_runtime": "claude_code_sdk",
            }
        })
        result = _switch("claude-code:work", model="claude-haiku-4-5-20251001")
        assert result.success, result.error_message
        assert result.api_mode == "claude_code_sdk", result.api_mode

    def test_runtime_off_leaves_api_mode_alone(self, monkeypatch):
        import hermes_cli.config as cfgmod

        monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: {
            "model": {"default": "claude-sonnet-5", "provider": "anthropic"}
        })
        result = _switch("anthropic", model="claude-haiku-4-5-20251001")
        assert result.success
        assert result.api_mode != "claude_code_sdk"

    def test_non_anthropic_provider_is_unaffected(self, monkeypatch):
        import hermes_cli.config as cfgmod

        monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: {
            "model": {"default": "x", "provider": "anthropic",
                      "anthropic_runtime": "claude_code_sdk"}
        })
        result = _switch("openrouter", model="anthropic/claude-sonnet-5")
        assert result.api_mode != "claude_code_sdk"
