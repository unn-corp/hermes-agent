"""/cli-account must work in the gateway (Desktop, Telegram, Discord…), not
just the terminal CLI.

Phase 2 wired the command into cli.py only, so switching CLI accounts was
impossible from the Desktop app — the command fell through to the agent as a
plain message.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


class _Runner:
    """Minimal stand-in exposing just the handler under test."""

    def __init__(self, agent=None):
        from gateway.slash_commands import GatewaySlashCommandsMixin as SlashCommandMixin

        self._agent = agent
        self._evicted = []
        for name in ("_handle_cli_account_command",):
            setattr(self, name, getattr(SlashCommandMixin, name).__get__(self))

    def _session_key_for_source(self, _source):
        return "sess-key"

    def _cached_agent_for_session(self, _key):
        return self._agent


def _event(args: str):
    return SimpleNamespace(
        get_command_args=lambda: args,
        source=SimpleNamespace(platform="desktop", chat_id="1"),
    )


@pytest.mark.asyncio
async def test_lists_accounts_with_no_args(monkeypatch):
    from agent.cli_accounts import CliAccount

    monkeypatch.setattr(
        "agent.cli_accounts.load_cli_accounts",
        lambda: [CliAccount(name="work", provider="claude_code_sdk", config_dir="/w")],
    )
    out = await _Runner()._handle_cli_account_command(_event(""))
    assert "work" in out and "/w" in out


@pytest.mark.asyncio
async def test_rejects_unknown_provider():
    out = await _Runner()._handle_cli_account_command(_event("anthropic work"))
    assert "Unknown provider" in out


@pytest.mark.asyncio
async def test_switches_the_live_agent():
    agent = MagicMock()
    out = await _Runner(agent)._handle_cli_account_command(
        _event("claude_code_sdk work")
    )
    agent.switch_cli_account.assert_called_once_with("claude_code_sdk", "work")
    assert "work" in out


@pytest.mark.asyncio
async def test_surfaces_an_unregistered_account_error():
    agent = MagicMock()
    agent.switch_cli_account.side_effect = ValueError(
        "No cli_accounts entry named 'nope' for provider 'claude_code_sdk'"
    )
    out = await _Runner(agent)._handle_cli_account_command(
        _event("claude_code_sdk nope")
    )
    assert "No cli_accounts entry" in out


@pytest.mark.asyncio
async def test_reports_when_no_agent_is_live_yet():
    """Before the first turn there is no agent to switch — say so rather than
    silently succeeding."""
    out = await _Runner(None)._handle_cli_account_command(
        _event("claude_code_sdk work")
    )
    assert "No active agent" in out
