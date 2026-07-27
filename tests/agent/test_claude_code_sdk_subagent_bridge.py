"""Tests for Phase 3 sub-agent visibility in
agent.claude_code_runtime.make_claude_code_sdk_event_bridge.

Fake message/block classes are named EXACTLY the string the bridge's
type(x).__name__ dispatch checks for (TaskStartedMessage, TextBlock,
ToolUseBlock, ToolResultBlock). A `_Fake`-prefixed alias would silently
defeat that dispatch — see this plan's Global Constraints "Dispatch
convention" note for why.
"""
from __future__ import annotations

import json

from unittest.mock import MagicMock

from agent.claude_code_runtime import make_claude_code_sdk_event_bridge


class TaskStartedMessage:
    def __init__(self, task_id, description, tool_use_id=None, task_type=None):
        self.task_id = task_id
        self.description = description
        self.tool_use_id = tool_use_id
        self.task_type = task_type


class TaskProgressMessage:
    def __init__(self, task_id, description, usage=None, last_tool_name=None):
        self.task_id = task_id
        self.description = description
        self.usage = usage or {"total_tokens": 0, "tool_uses": 0, "duration_ms": 0}
        self.last_tool_name = last_tool_name


class TaskUpdatedMessage:
    def __init__(self, task_id, patch=None, status=None):
        self.task_id = task_id
        self.patch = patch or {}
        self.status = status


class TaskNotificationMessage:
    def __init__(self, task_id, status, summary, output_file=""):
        self.task_id = task_id
        self.status = status
        self.summary = summary
        self.output_file = output_file


class TextBlock:
    def __init__(self, text):
        self.text = text


class ToolUseBlock:
    def __init__(self, id, name, input):
        self.id = id
        self.name = name
        self.input = input


class ToolResultBlock:
    def __init__(self, tool_use_id, content, is_error=False):
        self.tool_use_id = tool_use_id
        self.content = content
        self.is_error = is_error


class AssistantMessage:
    def __init__(self, content, parent_tool_use_id=None):
        self.content = content
        self.parent_tool_use_id = parent_tool_use_id


def _make_agent():
    agent = MagicMock()
    agent.session_id = "sess-1"
    agent._session_db_created = True
    return agent


def _send(bridge, message):
    bridge({"type": "raw_message", "message": message})


# --- TaskStartedMessage ---------------------------------------------------


def test_task_started_fires_tool_start_callback():
    agent = _make_agent()
    bridge = make_claude_code_sdk_event_bridge(agent)

    _send(bridge, TaskStartedMessage(
        task_id="task-1", description="Investigate flaky test", tool_use_id="tu_1",
    ))

    agent.tool_start_callback.assert_called_once_with(
        "task-1",
        "claude_subagent_task",
        {"task_id": "task-1", "description": "Investigate flaky test"},
    )


def test_task_started_persists_initial_transcript_row():
    agent = _make_agent()
    bridge = make_claude_code_sdk_event_bridge(agent)

    _send(bridge, TaskStartedMessage(
        task_id="task-1", description="Investigate flaky test", tool_use_id="tu_1",
    ))

    agent._session_db.upsert_subagent_transcript.assert_called_once_with(
        "sess-1",
        "task-1",
        tool_use_id="tu_1",
        description="Investigate flaky test",
        status="running",
        events=[],
        summary=None,
    )


# --- Suppression of the parent turn's generic "Task" tool bubble ----------


def test_task_tool_use_block_does_not_fire_generic_tool_started():
    agent = _make_agent()
    bridge = make_claude_code_sdk_event_bridge(agent)

    _send(bridge, AssistantMessage(
        content=[ToolUseBlock(id="tu_1", name="Task", input={"description": "go"})],
    ))

    agent.tool_progress_callback.assert_not_called()
    agent.tool_start_callback.assert_not_called()


def test_task_tool_result_block_does_not_fire_generic_tool_completed():
    agent = _make_agent()
    bridge = make_claude_code_sdk_event_bridge(agent)

    _send(bridge, AssistantMessage(
        content=[ToolUseBlock(id="tu_1", name="Task", input={})],
    ))
    _send(bridge, AssistantMessage(
        content=[ToolResultBlock(tool_use_id="tu_1", content="sub-agent summary")],
    ))

    agent.tool_progress_callback.assert_not_called()
    agent.tool_complete_callback.assert_not_called()


def test_non_task_tool_blocks_still_fire_normally():
    """Suppression must be surgical — ordinary tools are unaffected."""
    agent = _make_agent()
    bridge = make_claude_code_sdk_event_bridge(agent)

    _send(bridge, AssistantMessage(
        content=[ToolUseBlock(id="tu_9", name="Bash", input={"command": "ls"})],
    ))

    agent.tool_start_callback.assert_called_once_with(
        "tu_9", "Bash", {"command": "ls"},
    )


# --- Sub-agent's own content accumulation --------------------------------


def test_subagent_own_assistant_text_is_accumulated_not_fired_as_top_level():
    agent = _make_agent()
    bridge = make_claude_code_sdk_event_bridge(agent)

    _send(bridge, TaskStartedMessage(
        task_id="task-1", description="Investigate", tool_use_id="tu_1",
    ))
    agent._session_db.reset_mock()

    _send(bridge, AssistantMessage(
        content=[TextBlock("looking at the logs")],
        parent_tool_use_id="tu_1",
    ))

    # The sub-agent's own text must never hit the top-level stream delta —
    # it belongs in the persisted transcript only.
    agent._fire_stream_delta.assert_not_called()
    agent._session_db.upsert_subagent_transcript.assert_called_once_with(
        "sess-1",
        "task-1",
        tool_use_id="tu_1",
        description="Investigate",
        status="running",
        events=[{"type": "text", "text": "looking at the logs"}],
        summary=None,
    )


def test_subagent_own_tool_blocks_are_projected_into_the_transcript():
    agent = _make_agent()
    bridge = make_claude_code_sdk_event_bridge(agent)

    _send(bridge, TaskStartedMessage(
        task_id="task-1", description="Investigate", tool_use_id="tu_1",
    ))
    _send(bridge, AssistantMessage(
        content=[
            ToolUseBlock(id="inner_1", name="Grep", input={"pattern": "boom"}),
            ToolResultBlock(tool_use_id="inner_1", content="3 matches"),
        ],
        parent_tool_use_id="tu_1",
    ))

    events = agent._session_db.upsert_subagent_transcript.call_args.kwargs["events"]
    assert events == [
        {"type": "tool_use", "name": "Grep", "input": {"pattern": "boom"}},
        {"type": "tool_result", "is_error": False, "content": "3 matches"},
    ]
    # Inner tool calls must not surface as top-level tool cards.
    agent.tool_start_callback.assert_called_once()  # only the Task start
    assert agent.tool_start_callback.call_args.args[1] == "claude_subagent_task"


# --- Progress / non-terminal updates -------------------------------------


def test_task_progress_updates_persisted_transcript_without_completing():
    agent = _make_agent()
    bridge = make_claude_code_sdk_event_bridge(agent)

    _send(bridge, TaskStartedMessage(
        task_id="task-1", description="Investigate", tool_use_id="tu_1",
    ))
    _send(bridge, TaskProgressMessage(
        task_id="task-1", description="Investigate", last_tool_name="Bash",
    ))

    agent.tool_complete_callback.assert_not_called()
    agent._session_db.upsert_subagent_transcript.assert_called_with(
        "sess-1", "task-1", tool_use_id="tu_1", description="Investigate",
        status="running", events=[], summary=None,
    )


def test_non_terminal_task_updated_does_not_complete():
    agent = _make_agent()
    bridge = make_claude_code_sdk_event_bridge(agent)

    _send(bridge, TaskStartedMessage(
        task_id="task-1", description="Investigate", tool_use_id="tu_1",
    ))
    _send(bridge, TaskUpdatedMessage(task_id="task-1", status="paused"))

    agent.tool_complete_callback.assert_not_called()


# --- Terminal completion --------------------------------------------------


def test_task_notification_completed_fires_tool_complete_callback():
    agent = _make_agent()
    bridge = make_claude_code_sdk_event_bridge(agent)

    _send(bridge, TaskStartedMessage(
        task_id="task-1", description="Investigate", tool_use_id="tu_1",
    ))
    _send(bridge, TaskNotificationMessage(
        task_id="task-1", status="completed", summary="Found the root cause",
    ))

    agent.tool_complete_callback.assert_called_once()
    call_args = agent.tool_complete_callback.call_args
    assert call_args.args[0] == "task-1"
    assert call_args.args[1] == "claude_subagent_task"
    assert call_args.args[2] == {"task_id": "task-1", "description": "Investigate"}
    result = json.loads(call_args.args[3])
    assert result == {
        "task_id": "task-1", "status": "completed",
        "summary": "Found the root cause", "is_error": False,
    }


def test_task_updated_killed_with_no_notification_still_completes():
    agent = _make_agent()
    bridge = make_claude_code_sdk_event_bridge(agent)

    _send(bridge, TaskStartedMessage(
        task_id="task-1", description="Investigate", tool_use_id="tu_1",
    ))
    _send(bridge, TaskUpdatedMessage(task_id="task-1", status="killed"))

    agent.tool_complete_callback.assert_called_once()
    result = json.loads(agent.tool_complete_callback.call_args.args[3])
    assert result["status"] == "killed"
    assert result["is_error"] is True


def test_task_result_block_still_suppressed_after_completion():
    agent = _make_agent()
    bridge = make_claude_code_sdk_event_bridge(agent)

    _send(bridge, AssistantMessage(
        content=[ToolUseBlock(id="tu_1", name="Task", input={})],
    ))
    _send(bridge, TaskStartedMessage(
        task_id="task-1", description="Investigate", tool_use_id="tu_1",
    ))
    _send(bridge, TaskNotificationMessage(
        task_id="task-1", status="completed", summary="done",
    ))
    _send(bridge, AssistantMessage(
        content=[ToolResultBlock(tool_use_id="tu_1", content="done")],
    ))

    agent.tool_progress_callback.assert_not_called()


def test_task_message_without_task_id_is_ignored():
    agent = _make_agent()
    bridge = make_claude_code_sdk_event_bridge(agent)

    _send(bridge, TaskStartedMessage(task_id="", description="nope"))

    agent.tool_start_callback.assert_not_called()
    agent._session_db.upsert_subagent_transcript.assert_not_called()


def test_persistence_failure_does_not_propagate():
    """A broken DB must never tear down the turn loop."""
    agent = _make_agent()
    agent._session_db.upsert_subagent_transcript.side_effect = RuntimeError("db down")
    bridge = make_claude_code_sdk_event_bridge(agent)

    _send(bridge, TaskStartedMessage(
        task_id="task-1", description="Investigate", tool_use_id="tu_1",
    ))  # must not raise

    agent.tool_start_callback.assert_called_once()
