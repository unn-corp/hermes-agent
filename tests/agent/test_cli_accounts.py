"""Tests for agent/cli_accounts.py — the shared named-account registry used
by both the codex_app_server and claude_code_sdk runtimes."""
from __future__ import annotations

import json

from agent.cli_accounts import (
    CliAccount,
    load_cli_accounts,
    probe_cli_account,
    resolve_cli_account,
)


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


def test_probe_codex_account_fails_when_binary_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "agent.transports.codex_app_server.check_codex_binary",
        lambda: (False, "codex CLI not found"),
    )
    account = CliAccount(name="work", provider="codex", config_dir=str(tmp_path))
    ok, msg = probe_cli_account(account)
    assert ok is False
    assert msg == "codex CLI not found"


def test_probe_codex_account_fails_when_auth_json_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "agent.transports.codex_app_server.check_codex_binary",
        lambda: (True, "0.130.0"),
    )
    account = CliAccount(name="work", provider="codex", config_dir=str(tmp_path))
    ok, msg = probe_cli_account(account)
    assert ok is False
    assert "no auth.json" in msg


def test_probe_codex_account_succeeds_with_valid_auth_json(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "agent.transports.codex_app_server.check_codex_binary",
        lambda: (True, "0.130.0"),
    )
    (tmp_path / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "tok", "refresh_token": "ref"}})
    )
    account = CliAccount(name="work", provider="codex", config_dir=str(tmp_path))
    ok, msg = probe_cli_account(account)
    assert ok is True
    assert "0.130.0" in msg


def test_probe_claude_code_sdk_account_succeeds_with_valid_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "agent.transports.claude_code_sdk.check_claude_binary",
        lambda: (True, "2.1.217"),
    )
    (tmp_path / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
    )
    account = CliAccount(name="personal", provider="claude_code_sdk", config_dir=str(tmp_path))
    ok, msg = probe_cli_account(account)
    assert ok is True
    assert "2.1.217" in msg


def test_probe_claude_code_sdk_account_fails_when_credentials_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "agent.transports.claude_code_sdk.check_claude_binary",
        lambda: (True, "2.1.217"),
    )
    account = CliAccount(name="personal", provider="claude_code_sdk", config_dir=str(tmp_path))
    ok, msg = probe_cli_account(account)
    assert ok is False
    assert "no .credentials.json" in msg
