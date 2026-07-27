"""Tests for the codex_home account-switch wiring in agent/codex_runtime.py
(the "in-scope, non-free work on the Codex side" fix from
docs/design/claude-code-integration.md component 2)."""
from __future__ import annotations

from types import SimpleNamespace

import run_agent
from agent.cli_accounts import CliAccount
from agent.codex_runtime import _resolve_codex_home
import agent.transports.codex_app_server_session as codex_session_mod
from agent.transports.codex_app_server_session import TurnResult


def test_resolve_codex_home_returns_none_when_no_account_selected():
    agent = SimpleNamespace()
    assert _resolve_codex_home(agent) is None


def test_resolve_codex_home_returns_none_when_only_claude_account_active():
    agent = SimpleNamespace(_active_cli_accounts={
        "claude_code_sdk": CliAccount(name="p", provider="claude_code_sdk", config_dir="/x"),
    })
    assert _resolve_codex_home(agent) is None


def test_resolve_codex_home_returns_config_dir_when_codex_account_active():
    agent = SimpleNamespace(_active_cli_accounts={
        "codex": CliAccount(name="work", provider="codex", config_dir="/home/u/.codex-work"),
    })
    assert _resolve_codex_home(agent) == "/home/u/.codex-work"


def _make_codex_agent(**kwargs):
    return run_agent.AIAgent(
        api_key="stub",
        base_url="https://stub.invalid",
        provider="openai",
        api_mode="codex_app_server",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        **kwargs,
    )


def test_run_codex_app_server_turn_threads_codex_home_from_active_account(monkeypatch):
    captured = {}
    _original_init = codex_session_mod.CodexAppServerSession.__init__

    def _recording_init(self, **kwargs):
        captured.update(kwargs)
        _original_init(self, **kwargs)

    def _fake_run_turn(self, user_input: str, **kwargs):
        return TurnResult(final_text=f"echo: {user_input}", projected_messages=[])

    monkeypatch.setattr(codex_session_mod.CodexAppServerSession, "__init__", _recording_init)
    monkeypatch.setattr(codex_session_mod.CodexAppServerSession, "run_turn", _fake_run_turn)
    monkeypatch.setattr(
        codex_session_mod.CodexAppServerSession, "ensure_started", lambda self: "thread-stub"
    )

    agent = _make_codex_agent()
    agent._active_cli_accounts = {
        "codex": CliAccount(name="work", provider="codex", config_dir="/home/u/.codex-work"),
    }

    agent._run_codex_app_server_turn(
        user_message="hello",
        original_user_message="hello",
        messages=[],
        effective_task_id="task-1",
    )

    assert captured["codex_home"] == "/home/u/.codex-work"


def test_run_codex_app_server_turn_passes_none_codex_home_by_default(monkeypatch):
    captured = {}
    _original_init = codex_session_mod.CodexAppServerSession.__init__

    def _recording_init(self, **kwargs):
        captured.update(kwargs)
        _original_init(self, **kwargs)

    def _fake_run_turn(self, user_input: str, **kwargs):
        return TurnResult(final_text=f"echo: {user_input}", projected_messages=[])

    monkeypatch.setattr(codex_session_mod.CodexAppServerSession, "__init__", _recording_init)
    monkeypatch.setattr(codex_session_mod.CodexAppServerSession, "run_turn", _fake_run_turn)
    monkeypatch.setattr(
        codex_session_mod.CodexAppServerSession, "ensure_started", lambda self: "thread-stub"
    )

    agent = _make_codex_agent()

    agent._run_codex_app_server_turn(
        user_message="hello",
        original_user_message="hello",
        messages=[],
        effective_task_id="task-1",
    )

    assert captured["codex_home"] is None
