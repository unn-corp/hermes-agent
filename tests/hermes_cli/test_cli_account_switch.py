"""Tests for the /cli-account slash-command shared logic.

These cover the pure-Python state machine, mirroring
tests/hermes_cli/test_codex_runtime_switch.py's split (CLI/gateway handlers
are surface-specific and tested separately, if at all — see that file's own
module docstring)."""
from __future__ import annotations

from unittest.mock import MagicMock

from hermes_cli import cli_account_switch as cas


class TestParseArgs:
    def test_empty_args_returns_none_triple(self):
        assert cas.parse_args("") == (None, None, [])

    def test_whitespace_only_returns_none_triple(self):
        assert cas.parse_args("   ") == (None, None, [])

    def test_valid_args_parse(self):
        assert cas.parse_args("codex work") == ("codex", "work", [])

    def test_valid_args_case_insensitive_provider(self):
        assert cas.parse_args("CLAUDE_CODE_SDK personal") == ("claude_code_sdk", "personal", [])

    def test_unknown_provider_returns_error(self):
        provider, name, errors = cas.parse_args("anthropic work")
        assert provider is None
        assert errors and "Unknown provider" in errors[0]

    def test_missing_account_name_returns_usage_error(self):
        provider, name, errors = cas.parse_args("codex")
        assert provider is None
        assert errors and "Usage:" in errors[0]


class TestApply:
    def test_no_args_lists_accounts(self, monkeypatch):
        monkeypatch.setattr(cas, "list_accounts_message", lambda: "work (codex): /a")
        status = cas.apply(MagicMock(), None, None)
        assert status.success is True
        assert status.message == "work (codex): /a"

    def test_apply_calls_switch_cli_account(self):
        agent = MagicMock()
        status = cas.apply(agent, "codex", "work")
        agent.switch_cli_account.assert_called_once_with("codex", "work")
        assert status.success is True
        assert "Switched codex to account 'work'" in status.message

    def test_apply_surfaces_value_error_as_failure(self):
        agent = MagicMock()
        agent.switch_cli_account.side_effect = ValueError("No cli_accounts entry named 'x'")
        status = cas.apply(agent, "codex", "x")
        assert status.success is False
        assert "No cli_accounts entry" in status.message

    def test_apply_with_no_active_agent_fails_gracefully(self):
        status = cas.apply(None, "codex", "work")
        assert status.success is False
        assert "No active agent" in status.message


class TestListAccountsMessage:
    def test_reports_empty_registry_with_add_hint(self, monkeypatch):
        monkeypatch.setattr("agent.cli_accounts.load_cli_accounts", lambda: [])
        message = cas.list_accounts_message()
        assert "No cli_accounts configured" in message
        assert "hermes accounts add" in message

    def test_renders_one_line_per_account(self, monkeypatch):
        from agent.cli_accounts import CliAccount

        monkeypatch.setattr("agent.cli_accounts.load_cli_accounts", lambda: [
            CliAccount(name="work", provider="codex", config_dir="/a"),
            CliAccount(name="personal", provider="claude_code_sdk", config_dir="/b"),
        ])
        assert cas.list_accounts_message() == (
            "work (codex): /a\npersonal (claude_code_sdk): /b"
        )
