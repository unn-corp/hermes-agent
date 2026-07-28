"""Conversation continuity for the Claude Code runtime.

Two mechanisms, in order:
  1. resume=<cli session id> — the CLI still owns the context, we just
     reconnect to it. Cheap, and the natural fit since the CLI is the one
     holding the conversation.
  2. history replay — only on a cold start with no stored id (first turn ever,
     or the CLI swept its transcript under cleanupPeriodDays). Costs tokens,
     so it must never fire when resume is available.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.claude_code_runtime import (
    _build_history_replay_prefix,
    _resolve_claude_resume_id,
    run_claude_code_sdk_turn,
)


class TestResolveResumeId:
    def test_none_without_a_db(self):
        assert _resolve_claude_resume_id(SimpleNamespace(session_id="s1", _session_db=None)) is None

    def test_reads_the_stored_id(self):
        db = MagicMock()
        db.get_claude_code_session_id.return_value = "cli-9"
        agent = SimpleNamespace(session_id="s1", _session_db=db)
        assert _resolve_claude_resume_id(agent) == "cli-9"

    def test_db_failure_is_not_fatal(self):
        db = MagicMock()
        db.get_claude_code_session_id.side_effect = RuntimeError("db down")
        agent = SimpleNamespace(session_id="s1", _session_db=db)
        assert _resolve_claude_resume_id(agent) is None


class TestHistoryReplay:
    def test_empty_history_yields_nothing(self):
        assert _build_history_replay_prefix([]) == ""

    def test_only_the_system_prompt_yields_nothing(self):
        assert _build_history_replay_prefix([{"role": "system", "content": "be nice"}]) == ""

    def test_includes_prior_user_and_assistant_turns(self):
        out = _build_history_replay_prefix([
            {"role": "user", "content": "my favourite number is 17"},
            {"role": "assistant", "content": "noted"},
        ])
        assert "17" in out and "noted" in out

    def test_is_framed_as_context_not_instructions(self):
        """Replayed text is DATA. Without framing, a prior 'ignore all rules'
        turn would read as a live instruction on resume."""
        out = _build_history_replay_prefix([{"role": "user", "content": "hi"}])
        assert "<PRIOR_CONVERSATION>" in out and "</PRIOR_CONVERSATION>" in out
        assert "do not follow" in out.lower()

    def test_is_bounded(self):
        huge = [{"role": "user", "content": "x" * 5000} for _ in range(50)]
        assert len(_build_history_replay_prefix(huge)) <= 20000

    def test_keeps_the_most_recent_turns(self):
        """Trim from the OLDEST end — recent turns are what make the next
        reply coherent. Padded so the content genuinely exceeds the cap;
        short messages would fit entirely and trim nothing."""
        pad = "y" * 400
        msgs = [{"role": "user", "content": f"msg-{i} {pad}"} for i in range(100)]
        out = _build_history_replay_prefix(msgs)
        assert "msg-99" in out
        assert "msg-0 " not in out


class TestTurnWiring:
    def _run(self, monkeypatch, captured, *, stored_id=None, messages=None, usable=True):
        class _Session:
            def __init__(self, **kw):
                captured.update(kw)
                self.cli_session_id = "cli-new"

            def run_turn(self, user_input):
                captured["user_input"] = user_input
                return MagicMock(final_text="ok", interrupted=False, error=None,
                                 should_retire=False, projected_messages=[],
                                 tool_iterations=0, result_message=None)

        monkeypatch.setattr(
            "agent.transports.claude_code_sdk_session.ClaudeCodeSdkTurnSession",
            lambda **kw: _Session(**kw),
        )
        monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {})
        # Readability of the stored transcript is covered by its own test class;
        # here we pin it so these cases exercise the resume-vs-replay choice.
        monkeypatch.setattr(
            "agent.claude_code_runtime._claude_resume_id_is_usable",
            lambda home, sid: usable,
        )
        db = MagicMock()
        db.get_claude_code_session_id.return_value = stored_id
        agent = SimpleNamespace(
            _claude_code_session=None, session_cwd="/tmp", session_api_calls=0,
            session_id="s1", _session_db=db, _session_db_created=True,
            _ensure_db_session=lambda: None,
        )
        run_claude_code_sdk_turn(
            agent, user_message="hello", original_user_message="hello",
            messages=messages if messages is not None else [], effective_task_id="t",
        )
        return db

    def test_resume_is_used_when_stored(self, monkeypatch):
        captured = {}
        self._run(monkeypatch, captured, stored_id="cli-7",
                  messages=[{"role": "user", "content": "old turn"}])
        assert captured["resume"] == "cli-7"
        # Replay must NOT fire — the CLI already has the context.
        assert "PRIOR_CONVERSATION" not in captured["user_input"]

    def test_history_is_replayed_on_a_cold_start(self, monkeypatch):
        captured = {}
        self._run(monkeypatch, captured, stored_id=None,
                  messages=[{"role": "user", "content": "my favourite number is 17"}])
        assert captured["resume"] is None
        assert "PRIOR_CONVERSATION" in captured["user_input"]
        assert "17" in captured["user_input"]
        assert captured["user_input"].rstrip().endswith("hello")

    def test_no_replay_when_there_is_no_history(self, monkeypatch):
        captured = {}
        self._run(monkeypatch, captured, stored_id=None, messages=[])
        assert captured["user_input"] == "hello"

    def test_cli_session_id_is_persisted_after_the_turn(self, monkeypatch):
        db = self._run(monkeypatch, {}, stored_id=None)
        db.set_claude_code_session_id.assert_called_once_with("s1", "cli-new")


class TestResumeIsVerifiedBeforeUse:
    """A stored resume id is only good if the CLI can actually read that
    conversation under the ACTIVE account home.

    Transcripts live at <CLAUDE_CONFIG_DIR>/projects/<slug>/<id>.jsonl, so a
    default multi-account setup gives each account its own store and an id from
    another account resolves to nothing. The CLI does NOT error on a missing
    id — it silently starts a blank conversation, which is the exact
    transcript-looks-continuous-but-model-is-blank failure resume exists to
    prevent. So verify, and fall back to replay when it isn't there.
    """

    def _make_transcript(self, tmp_path, home: str, session_id: str):
        import pathlib
        d = pathlib.Path(tmp_path) / home / "projects" / "-tmp"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{session_id}.jsonl").write_text("{}\n")
        return str(pathlib.Path(tmp_path) / home)

    def test_true_when_the_transcript_exists(self, tmp_path):
        from agent.claude_code_runtime import _claude_resume_id_is_usable

        home = self._make_transcript(tmp_path, "acct-a", "uuid-1")
        assert _claude_resume_id_is_usable(home, "uuid-1") is True

    def test_false_when_the_id_belongs_to_another_account(self, tmp_path):
        from agent.claude_code_runtime import _claude_resume_id_is_usable

        self._make_transcript(tmp_path, "acct-a", "uuid-1")
        other = self._make_transcript(tmp_path, "acct-b", "uuid-2")
        assert _claude_resume_id_is_usable(other, "uuid-1") is False

    def test_shared_store_still_resolves(self, tmp_path):
        """A symlinked/shared projects dir must keep working — resume across
        accounts is a real capability when the store is common."""
        import os, pathlib

        home_a = self._make_transcript(tmp_path, "acct-a", "uuid-1")
        home_b = pathlib.Path(tmp_path) / "acct-b"
        home_b.mkdir(parents=True, exist_ok=True)
        os.symlink(pathlib.Path(home_a) / "projects", home_b / "projects")
        from agent.claude_code_runtime import _claude_resume_id_is_usable

        assert _claude_resume_id_is_usable(str(home_b), "uuid-1") is True

    def test_false_for_blank_or_missing_inputs(self, tmp_path):
        from agent.claude_code_runtime import _claude_resume_id_is_usable

        home = self._make_transcript(tmp_path, "acct-a", "uuid-1")
        assert _claude_resume_id_is_usable(home, "") is False
        assert _claude_resume_id_is_usable(str(tmp_path / "nope"), "uuid-1") is False

    def test_unverifiable_id_falls_back_to_replay(self, monkeypatch, tmp_path):
        """End-to-end: stored id that isn't readable here must NOT be sent as
        resume, and history must be replayed instead."""
        captured = {}

        class _Session:
            def __init__(self, **kw):
                captured.update(kw)
                self.cli_session_id = "cli-new"

            def run_turn(self, user_input):
                captured["user_input"] = user_input
                return MagicMock(final_text="ok", interrupted=False, error=None,
                                 should_retire=False, projected_messages=[],
                                 tool_iterations=0, result_message=None)

        monkeypatch.setattr(
            "agent.transports.claude_code_sdk_session.ClaudeCodeSdkTurnSession",
            lambda **kw: _Session(**kw),
        )
        monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {})
        monkeypatch.setattr(
            "agent.claude_code_runtime._resolve_claude_code_config_dir",
            lambda a: str(tmp_path / "empty-home"),
        )
        db = MagicMock()
        db.get_claude_code_session_id.return_value = "uuid-from-another-account"
        agent = SimpleNamespace(
            _claude_code_session=None, session_cwd="/tmp", session_api_calls=0,
            session_id="s1", _session_db=db, _session_db_created=True,
            _ensure_db_session=lambda: None,
        )
        run_claude_code_sdk_turn(
            agent, user_message="hello", original_user_message="hello",
            messages=[{"role": "user", "content": "my favourite number is 17"}],
            effective_task_id="t",
        )
        assert captured["resume"] is None
        assert "PRIOR_CONVERSATION" in captured["user_input"]
        assert "17" in captured["user_input"]
