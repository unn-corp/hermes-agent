"""Tests for the claude_config_dir account-switch wiring in
agent/claude_code_runtime.py (the Claude-side sibling of Task 8's Codex fix)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.cli_accounts import CliAccount
from agent.claude_code_runtime import (
    _resolve_claude_code_config_dir,
    run_claude_code_sdk_turn,
)


def test_resolve_claude_code_config_dir_returns_none_when_no_account_selected():
    agent = SimpleNamespace()
    assert _resolve_claude_code_config_dir(agent) is None


def test_resolve_claude_code_config_dir_returns_none_when_only_codex_account_active():
    agent = SimpleNamespace(_active_cli_accounts={
        "codex": CliAccount(name="w", provider="codex", config_dir="/x"),
    })
    assert _resolve_claude_code_config_dir(agent) is None


def test_resolve_claude_code_config_dir_returns_config_dir_when_account_active():
    agent = SimpleNamespace(_active_cli_accounts={
        "claude_code_sdk": CliAccount(
            name="personal", provider="claude_code_sdk", config_dir="/home/u/.claude-personal"
        ),
    })
    assert _resolve_claude_code_config_dir(agent) == "/home/u/.claude-personal"


class _RecordingSession:
    """Stands in for ClaudeCodeSdkTurnSession, capturing its constructor
    kwargs so the test can assert on what run_claude_code_sdk_turn threaded
    through."""

    def __init__(self, captured, **kwargs):
        captured.update(kwargs)

    def run_turn(self, user_input):
        return MagicMock(
            final_text=f"echo: {user_input}", interrupted=False, error=None,
            should_retire=False, projected_messages=[], tool_iterations=0,
            result_message=None,
        )


def _make_stub_agent(**extra):
    """Minimal agent stand-in: the attributes run_claude_code_sdk_turn and
    _record_claude_code_sdk_usage actually read, nothing more."""
    return SimpleNamespace(
        _claude_code_session=None,
        session_cwd="/tmp",
        session_api_calls=0,
        session_id=None,
        _session_db=None,
        **extra,
    )


def test_run_claude_code_sdk_turn_threads_claude_config_dir_from_active_account(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        "agent.transports.claude_code_sdk_session.ClaudeCodeSdkTurnSession",
        lambda **kwargs: _RecordingSession(captured, **kwargs),
    )

    agent = _make_stub_agent(_active_cli_accounts={
        "claude_code_sdk": CliAccount(
            name="personal", provider="claude_code_sdk", config_dir="/home/u/.claude-personal"
        ),
    })

    run_claude_code_sdk_turn(
        agent, user_message="hi", original_user_message="hi", messages=[], effective_task_id="t-1",
    )

    assert captured["claude_config_dir"] == "/home/u/.claude-personal"


def test_run_claude_code_sdk_turn_passes_none_config_dir_by_default(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        "agent.transports.claude_code_sdk_session.ClaudeCodeSdkTurnSession",
        lambda **kwargs: _RecordingSession(captured, **kwargs),
    )

    agent = _make_stub_agent()

    run_claude_code_sdk_turn(
        agent, user_message="hi", original_user_message="hi", messages=[], effective_task_id="t-1",
    )

    assert captured["claude_config_dir"] is None
