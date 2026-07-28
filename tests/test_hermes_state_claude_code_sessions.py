"""Persistence for the claude CLI's own conversation id.

The CLI owns the Claude-side context. Without storing its session id, a
backend restart loses that context entirely while Hermes' transcript still
shows the conversation — continuity that isn't there. Storing the id lets the
next session reconnect with resume=<id>.
"""
from __future__ import annotations

from hermes_state import SessionDB


def _db(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="s1", source="cli")
    db.create_session(session_id="s2", source="cli")
    return db


def test_returns_none_when_never_stored(tmp_path):
    assert _db(tmp_path).get_claude_code_session_id("s1") is None


def test_round_trips(tmp_path):
    db = _db(tmp_path)
    db.set_claude_code_session_id("s1", "cli-uuid-1")
    assert db.get_claude_code_session_id("s1") == "cli-uuid-1"


def test_overwrites_on_reconnect(tmp_path):
    """The CLI may hand back a new id (e.g. after fork/compaction); the latest
    one must win or resume would target a stale conversation."""
    db = _db(tmp_path)
    db.set_claude_code_session_id("s1", "cli-uuid-1")
    db.set_claude_code_session_id("s1", "cli-uuid-2")
    assert db.get_claude_code_session_id("s1") == "cli-uuid-2"


def test_sessions_are_isolated(tmp_path):
    db = _db(tmp_path)
    db.set_claude_code_session_id("s1", "a")
    db.set_claude_code_session_id("s2", "b")
    assert db.get_claude_code_session_id("s1") == "a"
    assert db.get_claude_code_session_id("s2") == "b"


def test_blank_id_is_ignored(tmp_path):
    """Never persist an empty id — resume="" would be worse than no resume."""
    db = _db(tmp_path)
    db.set_claude_code_session_id("s1", "real")
    db.set_claude_code_session_id("s1", "")
    assert db.get_claude_code_session_id("s1") == "real"
