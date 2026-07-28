"""The synthetic picker slug must be translated before anything persists it.

If `claude-code:work` ever reached config.yaml as model.provider, model
resolution would fail: no such provider exists.
"""
from __future__ import annotations

import pytest

from hermes_cli.web_server import _resolve_claude_code_provider_slug


ACCOUNTS = {
    "personal": {"provider": "claude_code_sdk", "config_dir": "/home/u/.claude"},
    "work": {"provider": "claude_code_sdk", "config_dir": "/home/u/.claude-work"},
}


def test_real_providers_pass_through_untouched():
    for slug in ("anthropic", "openrouter", "xai-oauth", ""):
        provider, account = _resolve_claude_code_provider_slug(slug, {"cli_accounts": ACCOUNTS})
        assert provider == slug
        assert account is None


def test_synthetic_slug_becomes_anthropic_plus_the_account():
    provider, account = _resolve_claude_code_provider_slug(
        "claude-code:work", {"cli_accounts": ACCOUNTS}
    )
    assert provider == "anthropic"
    assert account == ("work", "/home/u/.claude-work")


def test_unknown_account_still_degrades_to_anthropic():
    """Better to select the right provider with the default home than to
    persist a slug that resolves to nothing."""
    provider, account = _resolve_claude_code_provider_slug(
        "claude-code:ghost", {"cli_accounts": ACCOUNTS}
    )
    assert provider == "anthropic"
    assert account is None


def test_model_set_persists_a_real_provider(monkeypatch, tmp_path):
    """End-to-end guard: POST /api/model/set with a synthetic slug must write
    model.provider=anthropic and point claude_code.config_dir at the account."""
    from fastapi.testclient import TestClient
    import hermes_cli.web_server as ws
    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN

    state = {"config": {
        "model": {"default": "grok-build-0.1", "provider": "xai-oauth"},
        "cli_accounts": ACCOUNTS,
    }}
    monkeypatch.setattr(ws, "load_config", lambda: state["config"])
    monkeypatch.setattr(ws, "read_raw_config", lambda: state["config"])
    monkeypatch.setattr(ws, "save_config", lambda c, **k: state.update(config=c))

    client = TestClient(ws.app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    r = client.post("/api/model/set", json={
        "scope": "main",
        "provider": "claude-code:work",
        "model": "claude-sonnet-5",
        "confirm_expensive_model": True,
    })

    assert r.status_code == 200, r.text
    model_cfg = state["config"]["model"]
    assert model_cfg["provider"] == "anthropic"          # never the synthetic slug
    assert model_cfg["default"] == "claude-sonnet-5"
    assert model_cfg["anthropic_runtime"] == "claude_code_sdk"
    assert state["config"]["claude_code"]["config_dir"] == "/home/u/.claude-work"
