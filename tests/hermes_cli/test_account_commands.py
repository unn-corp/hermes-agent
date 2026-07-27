"""Tests for hermes_cli/account_commands.py — the `hermes accounts` handlers.

Mirrors tests/hermes_cli/test_auth_commands.py's shape: pure-Python handler
logic tested directly, with config load/save and the probe step mocked out."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli import account_commands


def test_accounts_add_command_probes_before_persisting(monkeypatch):
    persisted = {}
    monkeypatch.setattr(account_commands, "probe_cli_account", lambda account: (True, "codex 0.130.0"))
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    monkeypatch.setattr("hermes_cli.config.save_config", lambda config, **kw: persisted.update(config))

    account_commands.accounts_add_command(SimpleNamespace(
        name="work", provider="codex", config_dir="/home/u/.codex-work",
    ))

    assert persisted["cli_accounts"]["work"] == {
        "provider": "codex", "config_dir": "/home/u/.codex-work",
    }


def test_accounts_add_command_rejects_failed_probe(monkeypatch):
    monkeypatch.setattr(account_commands, "probe_cli_account", lambda account: (False, "codex CLI not found"))

    with pytest.raises(SystemExit, match="codex CLI not found"):
        account_commands.accounts_add_command(SimpleNamespace(
            name="work", provider="codex", config_dir="/home/u/.codex-work",
        ))


def test_accounts_add_command_rejects_unknown_provider():
    with pytest.raises(SystemExit, match="Unknown provider"):
        account_commands.accounts_add_command(SimpleNamespace(
            name="work", provider="bogus", config_dir="/tmp",
        ))


def test_accounts_add_command_requires_config_dir(monkeypatch):
    monkeypatch.setattr(account_commands, "probe_cli_account", lambda account: (True, "ok"))
    with pytest.raises(SystemExit, match="config-dir is required"):
        account_commands.accounts_add_command(SimpleNamespace(
            name="work", provider="codex", config_dir="",
        ))


def test_accounts_list_command_filters_by_provider(monkeypatch, capsys):
    from agent.cli_accounts import CliAccount

    monkeypatch.setattr(account_commands, "load_cli_accounts", lambda: [
        CliAccount(name="work", provider="codex", config_dir="/a"),
        CliAccount(name="personal", provider="claude_code_sdk", config_dir="/b"),
    ])
    account_commands.accounts_list_command(SimpleNamespace(provider="claude_code_sdk"))
    out = capsys.readouterr().out
    assert "personal" in out
    assert "work" not in out


def test_accounts_list_command_reports_empty(monkeypatch, capsys):
    monkeypatch.setattr(account_commands, "load_cli_accounts", lambda: [])
    account_commands.accounts_list_command(SimpleNamespace(provider=""))
    out = capsys.readouterr().out
    assert "No cli accounts registered" in out


def test_accounts_remove_command_removes_entry(monkeypatch):
    persisted = {}
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"cli_accounts": {"work": {"provider": "codex", "config_dir": "/a"}}},
    )
    monkeypatch.setattr("hermes_cli.config.save_config", lambda config, **kw: persisted.update(config))

    account_commands.accounts_remove_command(SimpleNamespace(name="work"))
    assert persisted["cli_accounts"] == {}


def test_accounts_remove_command_rejects_unknown_name(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"cli_accounts": {}})
    with pytest.raises(SystemExit, match='No cli account named "missing"'):
        account_commands.accounts_remove_command(SimpleNamespace(name="missing"))


def test_accounts_command_dispatches_to_add(monkeypatch):
    called = {}
    monkeypatch.setattr(account_commands, "accounts_add_command", lambda args: called.setdefault("action", "add"))
    account_commands.accounts_command(SimpleNamespace(accounts_action="add"))
    assert called["action"] == "add"


def test_accounts_command_prints_usage_when_no_action(capsys):
    account_commands.accounts_command(SimpleNamespace(accounts_action=None))
    err = capsys.readouterr().err
    assert "usage: hermes accounts" in err
