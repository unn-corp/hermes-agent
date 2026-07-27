"""Smoke tests for hermes_cli/subcommands/accounts.py's parser builder.
Mirrors tests/hermes_cli/test_subcommands_batch.py's single-handler-builder
pattern (auth.py, mcp.py, hooks.py, profile.py, skills.py all use this exact
single-injected-callable + internal-dispatch shape)."""
from __future__ import annotations

import argparse

import pytest

from hermes_cli.subcommands.accounts import build_accounts_parser


def _handler(args):  # pragma: no cover - identity only
    return args


def test_accounts_add_parses_required_fields():
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    build_accounts_parser(sub, cmd_accounts=_handler)

    ns = parser.parse_args(
        ["accounts", "add", "work", "--provider", "codex", "--config-dir", "/home/u/.codex-work"]
    )
    assert ns.func is _handler
    assert ns.accounts_action == "add"
    assert ns.name == "work"
    assert ns.provider == "codex"
    assert ns.config_dir == "/home/u/.codex-work"


def test_accounts_list_parses_optional_provider():
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    build_accounts_parser(sub, cmd_accounts=_handler)

    ns = parser.parse_args(["accounts", "list"])
    assert ns.accounts_action == "list"
    assert ns.provider == ""

    ns2 = parser.parse_args(["accounts", "list", "--provider", "claude_code_sdk"])
    assert ns2.provider == "claude_code_sdk"


def test_accounts_remove_parses_name():
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    build_accounts_parser(sub, cmd_accounts=_handler)

    ns = parser.parse_args(["accounts", "remove", "work"])
    assert ns.accounts_action == "remove"
    assert ns.name == "work"


def test_accounts_add_rejects_unknown_provider():
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    build_accounts_parser(sub, cmd_accounts=_handler)

    with pytest.raises(SystemExit):
        parser.parse_args(
            ["accounts", "add", "work", "--provider", "bogus", "--config-dir", "/x"]
        )
