"""Tests for the subagent_transcripts table (Phase 3, sub-agent visibility).

Mirrors tests/test_hermes_state.py's SessionDB(db_path=tmp_path / ...) setup.
"""
from __future__ import annotations

from hermes_state import SessionDB


def _make_db(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="s1", source="cli")
    return db


def test_get_subagent_transcript_returns_none_when_never_recorded(tmp_path):
    db = _make_db(tmp_path)
    assert db.get_subagent_transcript("s1", "task-1") is None


def test_upsert_then_get_round_trips_all_fields(tmp_path):
    db = _make_db(tmp_path)
    db.upsert_subagent_transcript(
        "s1",
        "task-1",
        tool_use_id="tu_1",
        description="Investigate failing test",
        status="running",
        events=[{"type": "text", "text": "looking into it"}],
        summary=None,
    )

    row = db.get_subagent_transcript("s1", "task-1")

    assert row is not None
    assert row["session_id"] == "s1"
    assert row["task_id"] == "task-1"
    assert row["tool_use_id"] == "tu_1"
    assert row["description"] == "Investigate failing test"
    assert row["status"] == "running"
    assert row["events"] == [{"type": "text", "text": "looking into it"}]
    assert row["summary"] is None


def test_upsert_replaces_events_and_status_on_repeated_calls(tmp_path):
    db = _make_db(tmp_path)
    db.upsert_subagent_transcript(
        "s1", "task-1", tool_use_id="tu_1", description="first",
        status="running", events=[{"type": "text", "text": "step 1"}],
    )
    db.upsert_subagent_transcript(
        "s1", "task-1", status="completed",
        events=[
            {"type": "text", "text": "step 1"},
            {"type": "text", "text": "step 2"},
        ],
        summary="Done investigating",
    )

    row = db.get_subagent_transcript("s1", "task-1")

    assert row["status"] == "completed"
    assert row["summary"] == "Done investigating"
    assert len(row["events"]) == 2
    # tool_use_id/description are not re-sent on the second call — the
    # COALESCE-on-conflict keeps the values from the first call rather than
    # clobbering them with NULL.
    assert row["tool_use_id"] == "tu_1"
    assert row["description"] == "first"


def test_two_tasks_in_the_same_session_do_not_collide(tmp_path):
    db = _make_db(tmp_path)
    db.upsert_subagent_transcript("s1", "task-1", description="first task")
    db.upsert_subagent_transcript("s1", "task-2", description="second task")

    assert db.get_subagent_transcript("s1", "task-1")["description"] == "first task"
    assert db.get_subagent_transcript("s1", "task-2")["description"] == "second task"
