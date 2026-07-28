"""The model picker must say WHICH Claude subscription it will use.

With the claude_code_sdk runtime on, every Anthropic model routes through the
CLI against one specific CLAUDE_CONFIG_DIR. The picker showed a bare
"Anthropic" heading, so a user with several subscriptions could not tell which
one a model would bill/run against.
"""
from __future__ import annotations

import pytest

from hermes_cli.inventory import _active_claude_code_account_label


def _cfg(runtime=None, config_dir=None, accounts=None):
    model = {"default": "claude-fable-5", "provider": "anthropic"}
    if runtime:
        model["anthropic_runtime"] = runtime
    cfg = {"model": model}
    if config_dir is not None:
        cfg["claude_code"] = {"config_dir": config_dir}
    if accounts is not None:
        cfg["cli_accounts"] = accounts
    return cfg


ACCOUNTS = {
    "personal": {"provider": "claude_code_sdk", "config_dir": "/home/u/.claude"},
    "work": {"provider": "claude_code_sdk", "config_dir": "/home/u/.claude-work"},
}


def test_none_when_runtime_is_off():
    """Without the CLI runtime, Anthropic is the plain HTTP API — no account
    is involved and a label would be a lie."""
    assert _active_claude_code_account_label(
        _cfg(config_dir="/home/u/.claude", accounts=ACCOUNTS)
    ) is None


def test_names_the_matching_account():
    assert _active_claude_code_account_label(
        _cfg("claude_code_sdk", "/home/u/.claude-work", ACCOUNTS)
    ) == "work"


def test_matches_through_a_tilde_path():
    """A tilde in either place must still resolve to the same account — both
    sides go through expanduser/realpath, so the comparison is on real paths."""
    tilde_accounts = {
        "work": {"provider": "claude_code_sdk", "config_dir": "~/.claude-work"},
    }
    assert _active_claude_code_account_label(
        _cfg("claude_code_sdk", "~/.claude-work", tilde_accounts)
    ) == "work"


def test_falls_back_to_the_directory_when_unregistered():
    """A config_dir set by hand, with no matching cli_accounts entry, should
    still say something concrete rather than nothing."""
    label = _active_claude_code_account_label(
        _cfg("claude_code_sdk", "/home/u/.claude-adhoc", ACCOUNTS)
    )
    assert label and "adhoc" in label


def test_default_home_when_no_config_dir_set():
    label = _active_claude_code_account_label(_cfg("claude_code_sdk", None, ACCOUNTS))
    assert label == "default"


def test_never_raises_on_junk_config():
    for junk in ({"model": "bare-string"}, {}, {"model": {"anthropic_runtime": "claude_code_sdk"}, "cli_accounts": "nope"}):
        _active_claude_code_account_label(junk)
