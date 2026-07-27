"""Per-instance Claude Code CLI options: binary path, CLAUDE_CONFIG_DIR and
launch arguments, resolved from config and threaded into the transport."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.claude_code_runtime import (
    _parse_claude_extra_args,
    _resolve_claude_code_config_dir,
    _resolve_claude_cli_options,
    run_claude_code_sdk_turn,
)


class TestParseExtraArgs:
    """The UI takes a free-text arg string; the SDK wants
    dict[str, str | None] keyed by flag name WITHOUT leading dashes, where
    None means a valueless boolean flag."""

    def test_empty_is_empty(self):
        assert _parse_claude_extra_args("") == {}
        assert _parse_claude_extra_args(None) == {}
        assert _parse_claude_extra_args("   ") == {}

    def test_bare_boolean_flag(self):
        assert _parse_claude_extra_args("--chrome") == {"chrome": None}

    def test_equals_form_carries_a_value(self):
        assert _parse_claude_extra_args("--model=opus") == {"model": "opus"}

    def test_space_separated_value(self):
        assert _parse_claude_extra_args("--model opus") == {"model": "opus"}

    def test_multiple_flags(self):
        assert _parse_claude_extra_args("--chrome --model opus --verbose") == {
            "chrome": None,
            "model": "opus",
            "verbose": None,
        }

    def test_quoted_value_stays_one_token(self):
        assert _parse_claude_extra_args('--append-system-prompt "be terse"') == {
            "append-system-prompt": "be terse"
        }

    def test_single_dash_flags_are_accepted(self):
        assert _parse_claude_extra_args("-v") == {"v": None}

    def test_bare_word_without_a_flag_is_ignored(self):
        """A stray positional would become a nonsense --flag, so drop it
        rather than corrupt the spawned command line."""
        assert _parse_claude_extra_args("garbage --chrome") == {"chrome": None}

    def test_malformed_input_never_raises(self):
        assert _parse_claude_extra_args('--unbalanced "quote') == {}


class TestResolveCliOptions:
    def test_defaults_when_unconfigured(self, monkeypatch):
        monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {})
        opts = _resolve_claude_cli_options()
        assert opts["claude_bin"] == "claude"
        assert opts["config_dir"] is None
        assert opts["extra_args"] == {}

    def test_reads_all_three_from_config(self, monkeypatch):
        monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {
            "claude_code": {
                "binary_path": "/opt/claude/bin/claude",
                "config_dir": "~/.claude-work",
                "extra_args": "--chrome",
            }
        })
        opts = _resolve_claude_cli_options()
        assert opts["claude_bin"] == "/opt/claude/bin/claude"
        assert opts["config_dir"].endswith(".claude-work")
        assert "~" not in opts["config_dir"]  # tilde expanded
        assert opts["extra_args"] == {"chrome": None}

    def test_blank_strings_fall_back_to_defaults(self, monkeypatch):
        monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {
            "claude_code": {"binary_path": "  ", "config_dir": "", "extra_args": ""}
        })
        opts = _resolve_claude_cli_options()
        assert opts["claude_bin"] == "claude"
        assert opts["config_dir"] is None


class TestConfigDirPrecedence:
    """A live account switch must outrank the configured default — otherwise
    /cli-account would appear to do nothing."""

    def test_active_account_wins_over_configured_default(self, monkeypatch):
        from agent.cli_accounts import CliAccount

        monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {
            "claude_code": {"config_dir": "/configured/default"}
        })
        agent = SimpleNamespace(_active_cli_accounts={
            "claude_code_sdk": CliAccount(
                name="personal", provider="claude_code_sdk", config_dir="/from/account"
            )
        })
        assert _resolve_claude_code_config_dir(agent) == "/from/account"

    def test_configured_default_used_when_no_account_selected(self, monkeypatch):
        monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {
            "claude_code": {"config_dir": "/configured/default"}
        })
        assert _resolve_claude_code_config_dir(SimpleNamespace()) == "/configured/default"

    def test_none_when_neither_is_set(self, monkeypatch):
        monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {})
        assert _resolve_claude_code_config_dir(SimpleNamespace()) is None


def test_options_are_threaded_into_the_session(monkeypatch):
    captured: dict = {}

    class _RecordingSession:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_turn(self, user_input):
            return MagicMock(
                final_text="ok", interrupted=False, error=None, should_retire=False,
                projected_messages=[], tool_iterations=0, result_message=None,
            )

    monkeypatch.setattr(
        "agent.transports.claude_code_sdk_session.ClaudeCodeSdkTurnSession",
        lambda **kw: _RecordingSession(**kw),
    )
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {
        "claude_code": {
            "binary_path": "/opt/claude/bin/claude",
            "extra_args": "--chrome",
        }
    })

    agent = SimpleNamespace(
        _claude_code_session=None, session_cwd="/tmp",
        session_api_calls=0, session_id=None, _session_db=None,
    )
    run_claude_code_sdk_turn(
        agent, user_message="hi", original_user_message="hi",
        messages=[], effective_task_id="t-1",
    )

    assert captured["claude_bin"] == "/opt/claude/bin/claude"
    assert captured["extra_args"] == {"chrome": None}
