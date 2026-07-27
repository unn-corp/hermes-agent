"""Tests for agent/cli_accounts.py — the shared named-account registry used
by both the codex_app_server and claude_code_sdk runtimes."""
from __future__ import annotations

from agent.cli_accounts import CliAccount, load_cli_accounts, resolve_cli_account


def test_load_cli_accounts_returns_typed_entries(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {
            "cli_accounts": {
                "work": {"provider": "codex", "config_dir": "/home/u/.codex-work"},
                "personal": {"provider": "claude_code_sdk", "config_dir": "/home/u/.claude-personal"},
            }
        },
    )
    accounts = load_cli_accounts()
    assert accounts == [
        CliAccount(name="work", provider="codex", config_dir="/home/u/.codex-work"),
        CliAccount(name="personal", provider="claude_code_sdk", config_dir="/home/u/.claude-personal"),
    ]


def test_load_cli_accounts_returns_empty_list_when_unset(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {})
    assert load_cli_accounts() == []


def test_load_cli_accounts_skips_malformed_entries(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {
            "cli_accounts": {
                "bad-provider": {"provider": "anthropic", "config_dir": "/x"},
                "missing-dir": {"provider": "codex"},
                "not-a-dict": "oops",
            }
        },
    )
    assert load_cli_accounts() == []


def test_resolve_cli_account_matches_name_and_provider(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"cli_accounts": {"work": {"provider": "codex", "config_dir": "/x"}}},
    )
    assert resolve_cli_account("work", "codex") == CliAccount(
        name="work", provider="codex", config_dir="/x"
    )
    assert resolve_cli_account("work", "claude_code_sdk") is None
    assert resolve_cli_account("nope", "codex") is None
