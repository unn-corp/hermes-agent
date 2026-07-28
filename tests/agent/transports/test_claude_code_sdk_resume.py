"""resume + CLI-session-id capture on the transport."""
from __future__ import annotations

from unittest.mock import MagicMock

from agent.transports.claude_code_sdk_session import ClaudeCodeSdkTurnSession


class _ResultMessage:
    def __init__(self, session_id):
        self.session_id = session_id
        self.usage = {}


def test_resume_is_passed_to_the_client():
    captured = {}
    ClaudeCodeSdkTurnSession(
        cwd="/tmp", resume="cli-uuid-9",
        client_factory=lambda **kw: (captured.update(kw), MagicMock())[1],
    ).ensure_started()
    assert captured["resume"] == "cli-uuid-9"


def test_no_resume_passes_none():
    captured = {}
    ClaudeCodeSdkTurnSession(
        cwd="/tmp",
        client_factory=lambda **kw: (captured.update(kw), MagicMock())[1],
    ).ensure_started()
    assert captured["resume"] is None


def test_cli_session_id_is_exposed_after_a_turn():
    """Captured from the CLI's own ResultMessage so the runtime can persist it
    and resume this exact conversation next process."""
    session = ClaudeCodeSdkTurnSession(cwd="/tmp", client_factory=lambda **kw: MagicMock())
    assert session.cli_session_id is None
    session._note_cli_session_id(_ResultMessage("cli-uuid-7"))
    assert session.cli_session_id == "cli-uuid-7"


def test_blank_or_missing_session_id_is_ignored():
    session = ClaudeCodeSdkTurnSession(cwd="/tmp", client_factory=lambda **kw: MagicMock())
    session._note_cli_session_id(_ResultMessage("real"))
    session._note_cli_session_id(_ResultMessage(""))
    session._note_cli_session_id(object())          # no attribute at all
    assert session.cli_session_id == "real"
