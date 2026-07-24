from unittest.mock import MagicMock

from agent.claude_code_runtime import run_claude_code_sdk_turn


def test_run_claude_code_sdk_turn_lazily_creates_session():
    agent = MagicMock()
    agent._claude_code_session = None
    agent.session_cwd = "/tmp"
    agent._session_db = None

    fake_session = MagicMock()
    fake_session.run_turn.return_value = MagicMock(
        final_text="hi from claude",
        interrupted=False,
        error=None,
        should_retire=False,
        projected_messages=[],
        tool_iterations=0,
    )

    def _session_factory(**kwargs):
        agent._claude_code_session = fake_session
        return fake_session

    result = run_claude_code_sdk_turn(
        agent,
        user_message="hello",
        original_user_message="hello",
        messages=[],
        effective_task_id="task-1",
        session_factory=_session_factory,
    )

    assert result["final_response"] == "hi from claude"
    assert result["completed"] is True
    fake_session.run_turn.assert_called_once_with(user_input="hello")


def test_run_claude_code_sdk_turn_reuses_existing_session():
    agent = MagicMock()
    fake_session = MagicMock()
    fake_session.run_turn.return_value = MagicMock(
        final_text="second turn",
        interrupted=False,
        error=None,
        should_retire=False,
        projected_messages=[],
        tool_iterations=0,
    )
    agent._claude_code_session = fake_session

    result = run_claude_code_sdk_turn(
        agent,
        user_message="again",
        original_user_message="again",
        messages=[],
        effective_task_id="task-2",
        session_factory=MagicMock(side_effect=AssertionError("should not be called")),
    )

    assert result["final_response"] == "second turn"


def test_run_claude_code_sdk_turn_does_not_report_non_user_interrupt_as_interrupted():
    """Mirrors run_codex_app_server_turn's interrupt handoff: a
    turn.interrupted that fires WITHOUT a paired agent._interrupt_requested
    (e.g. ClaudeCodeSdkTurnSession's own internal turn-timeout deadline,
    not a user Ctrl+C) must not surface as "interrupted": True to the
    caller, and must not clear an interrupt request that was never made."""
    agent = MagicMock()
    agent._claude_code_session = None
    agent.session_cwd = "/tmp"
    agent._session_db = None
    agent._interrupt_requested = False

    fake_session = MagicMock()
    fake_session.run_turn.return_value = MagicMock(
        final_text="partial before timeout",
        interrupted=True,
        error="turn timed out after 600.0s",
        should_retire=True,
        projected_messages=[],
        tool_iterations=0,
    )

    def _session_factory(**kwargs):
        agent._claude_code_session = fake_session
        return fake_session

    result = run_claude_code_sdk_turn(
        agent,
        user_message="hello",
        original_user_message="hello",
        messages=[],
        effective_task_id="task-3",
        session_factory=_session_factory,
    )

    assert result["interrupted"] is False
    assert "interrupt_message" not in result
    agent.clear_interrupt.assert_not_called()


def test_run_claude_code_sdk_turn_reports_user_driven_interrupt():
    """Contrast case: when the interrupt WAS user-driven (agent._interrupt_
    requested is True), the same turn.interrupted=True must surface as
    "interrupted": True, and clear_interrupt() must be called — proving the
    prior test genuinely distinguishes the two cases rather than always
    returning False."""
    agent = MagicMock()
    agent._claude_code_session = None
    agent.session_cwd = "/tmp"
    agent._session_db = None
    agent._interrupt_requested = True
    agent._interrupt_message = "user hit stop"

    fake_session = MagicMock()
    fake_session.run_turn.return_value = MagicMock(
        final_text="partial before stop",
        interrupted=True,
        error=None,
        should_retire=False,
        projected_messages=[],
        tool_iterations=0,
    )

    def _session_factory(**kwargs):
        agent._claude_code_session = fake_session
        return fake_session

    result = run_claude_code_sdk_turn(
        agent,
        user_message="hello",
        original_user_message="hello",
        messages=[],
        effective_task_id="task-4",
        session_factory=_session_factory,
    )

    assert result["interrupted"] is True
    assert result["interrupt_message"] == "user hit stop"
    agent.clear_interrupt.assert_called_once()


def test_aiagent_forwarder_delegates_to_run_claude_code_sdk_turn(monkeypatch):
    from run_agent import AIAgent

    called = {}

    def _fake_run_claude_code_sdk_turn(agent, **kwargs):
        called["kwargs"] = kwargs
        return {"final_response": "ok", "messages": [], "api_calls": 1,
                "completed": True, "partial": False, "interrupted": False,
                "error": None}

    monkeypatch.setattr(
        "agent.claude_code_runtime.run_claude_code_sdk_turn",
        _fake_run_claude_code_sdk_turn,
    )

    agent = AIAgent.__new__(AIAgent)  # bypass __init__; forwarder only touches self
    result = agent._run_claude_code_sdk_turn(
        user_message="hi",
        original_user_message="hi",
        messages=[],
        effective_task_id="t-1",
    )
    assert result["final_response"] == "ok"
    assert called["kwargs"]["user_message"] == "hi"
