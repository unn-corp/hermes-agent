"""Tests for the subagent_transcript.get RPC (Phase 3, sub-agent
visibility). Mirrors tests/tui_gateway/test_billing_rpc.py's direct
_methods[...] invocation pattern."""
from __future__ import annotations

import tui_gateway.server as srv
from hermes_state import SessionDB


def _call(method: str, params: dict) -> dict:
    envelope = srv._methods[method](1, params)
    return envelope


def test_subagent_transcript_get_returns_not_found_when_missing(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(srv, "_get_db", lambda: db)

    envelope = _call("subagent_transcript.get", {"session_id": "s1", "task_id": "task-1"})

    assert envelope["result"]["found"] is False


def test_subagent_transcript_get_returns_persisted_transcript(tmp_path, monkeypatch):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="s1", source="cli")
    db.upsert_subagent_transcript(
        "s1", "task-1", tool_use_id="tu_1", description="Investigate",
        status="completed",
        events=[{"type": "text", "text": "found it"}],
        summary="Root cause found",
    )
    monkeypatch.setattr(srv, "_get_db", lambda: db)

    envelope = _call("subagent_transcript.get", {"session_id": "s1", "task_id": "task-1"})
    result = envelope["result"]

    assert result["found"] is True
    assert result["task_id"] == "task-1"
    assert result["tool_use_id"] == "tu_1"
    assert result["description"] == "Investigate"
    assert result["status"] == "completed"
    assert result["events"] == [{"type": "text", "text": "found it"}]
    assert result["summary"] == "Root cause found"


def test_subagent_transcript_get_requires_session_id_and_task_id():
    envelope = _call("subagent_transcript.get", {"session_id": "", "task_id": ""})
    assert "error" in envelope


def test_subagent_transcript_get_fails_soft_when_db_unavailable(monkeypatch):
    monkeypatch.setattr(srv, "_get_db", lambda: None)

    envelope = _call("subagent_transcript.get", {"session_id": "s1", "task_id": "task-1"})

    assert envelope["result"]["found"] is False


def test_subagent_transcript_get_surfaces_db_errors_as_rpc_error(monkeypatch):
    class _BoomDB:
        def get_subagent_transcript(self, session_id, task_id):
            raise RuntimeError("db exploded")

    monkeypatch.setattr(srv, "_get_db", lambda: _BoomDB())

    envelope = _call("subagent_transcript.get", {"session_id": "s1", "task_id": "task-1"})

    assert "error" in envelope
    assert "db exploded" in envelope["error"]["message"]
