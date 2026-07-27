"""Tests for AIAgent.switch_cli_account() — the live mid-conversation
CLI-account hot-swap (docs/design/claude-code-integration.md, "Live account
hot-swap"). Constructs a real AIAgent the same way
tests/run_agent/test_codex_app_server_integration.py does, so no real
provider/subprocess is ever contacted."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import run_agent


def _make_agent(**kwargs):
    return run_agent.AIAgent(
        api_key="stub",
        base_url="https://stub.invalid",
        provider="openai",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        **kwargs,
    )


def test_switch_cli_account_raises_for_unknown_provider():
    agent = _make_agent()
    with pytest.raises(ValueError, match="Unknown cli account provider"):
        agent.switch_cli_account("bogus", "work")


def test_switch_cli_account_raises_when_account_not_registered(monkeypatch):
    monkeypatch.setattr("agent.cli_accounts.resolve_cli_account", lambda name, provider: None)
    agent = _make_agent()
    with pytest.raises(ValueError, match="No cli_accounts entry"):
        agent.switch_cli_account("codex", "missing")


def test_switch_cli_account_closes_and_clears_live_codex_session(monkeypatch):
    from agent.cli_accounts import CliAccount

    account = CliAccount(name="work", provider="codex", config_dir="/home/u/.codex-work")
    monkeypatch.setattr("agent.cli_accounts.resolve_cli_account", lambda name, provider: account)

    agent = _make_agent()
    fake_session = MagicMock()
    agent._codex_session = fake_session

    agent.switch_cli_account("codex", "work")

    fake_session.request_interrupt.assert_called_once()
    fake_session.close.assert_called_once()
    assert agent._codex_session is None
    assert agent._active_cli_accounts["codex"] == account


def test_switch_cli_account_closes_and_clears_live_claude_code_session(monkeypatch):
    from agent.cli_accounts import CliAccount

    account = CliAccount(name="personal", provider="claude_code_sdk", config_dir="/home/u/.claude-personal")
    monkeypatch.setattr("agent.cli_accounts.resolve_cli_account", lambda name, provider: account)

    agent = _make_agent()
    fake_session = MagicMock()
    agent._claude_code_session = fake_session

    agent.switch_cli_account("claude_code_sdk", "personal")

    fake_session.request_interrupt.assert_called_once()
    fake_session.close.assert_called_once()
    assert agent._claude_code_session is None
    assert agent._active_cli_accounts["claude_code_sdk"] == account


def test_switch_cli_account_is_safe_with_no_live_session(monkeypatch):
    from agent.cli_accounts import CliAccount

    account = CliAccount(name="work", provider="codex", config_dir="/home/u/.codex-work")
    monkeypatch.setattr("agent.cli_accounts.resolve_cli_account", lambda name, provider: account)

    agent = _make_agent()
    agent.switch_cli_account("codex", "work")  # must not raise

    assert agent._active_cli_accounts["codex"] == account


def test_interrupt_calls_claude_code_session_request_interrupt():
    agent = _make_agent()
    agent.api_mode = "claude_code_sdk"  # set directly — decoupled from agent_init.py's gate
    fake_session = MagicMock()
    agent._claude_code_session = fake_session

    agent.interrupt("stop")

    fake_session.request_interrupt.assert_called_once()
