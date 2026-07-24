"""Claude Code SDK runtime — mirrors agent/codex_runtime.py.

Each function takes the parent AIAgent as its first argument (agent).
AIAgent keeps a thin forwarder method (_run_claude_code_sdk_turn) for
consistency with the Codex app-server pattern.

Status: this task wires the lazy session lifecycle, dispatch path, and
event bridging (make_claude_code_sdk_event_bridge, defined below).
Usage recording (_record_claude_code_sdk_usage) lands in a follow-up task
in this same plan.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


def make_claude_code_sdk_event_bridge(agent) -> Callable[[dict], None]:
    """Build an on_event callback wiring claude-agent-sdk messages into
    Hermes' gateway UI callbacks. Mirrors
    agent.codex_runtime.make_codex_app_server_event_bridge — same four
    target callbacks (tool_progress_callback, tool_start_callback,
    tool_complete_callback, _fire_stream_delta), different source message
    shapes (SDK dataclasses instead of codex JSON-RPC dicts).

    tool_start_callback/tool_complete_callback are fired alongside
    tool_progress_callback, not instead of it — verified against
    tui_gateway/server.py, where tool_progress_callback's "tool.started"
    case is a no-op; tool_start_callback/tool_complete_callback are what
    actually produce the visible, stable-ID tool card in the TUI/Desktop.
    Firing only tool_progress_callback (as an earlier draft of this bridge
    did) would pass unit tests asserting on tool_progress_callback alone
    while never actually rendering anything in the real gateway.

    Scope note: this task handles ordinary AssistantMessage content blocks
    (TextBlock, ToolUseBlock, ToolResultBlock) only. TaskStartedMessage /
    TaskUpdatedMessage / TaskProgressMessage / TaskNotificationMessage
    (sub-agent lifecycle) are handled by a later plan (Phase 3, sub-agent
    visibility) — this bridge silently ignores those message types for now.

    All callback invocations are guarded exactly like the Codex bridge —
    a buggy display callback must not tear down the turn loop.
    """
    # tool_use_id -> (tool_name, args). Populated when a ToolUseBlock is
    # seen; consumed when the matching ToolResultBlock arrives, so the
    # completed-bubble can report the tool name and original args without
    # re-deriving them.
    started: dict[str, tuple[str, dict]] = {}

    def _fire_tool_started(block) -> None:
        name = block.name
        args = block.input if isinstance(block.input, dict) else {}
        started[block.id] = (name, args)
        preview = None
        if isinstance(args, dict):
            command = args.get("command")
            file_path = args.get("file_path")
            preview = command or file_path
            if isinstance(preview, str):
                preview = preview[:120]
        cb = getattr(agent, "tool_progress_callback", None)
        if cb is not None:
            try:
                cb("tool.started", name, preview, args)
            except Exception:
                logger.debug(
                    "tool_progress_callback raised on tool.started for %s",
                    name, exc_info=True,
                )
        # Authoritative stable-ID tool card (TUI / Desktop). Claude's
        # ToolUseBlock already carries a real unique id from the API, so
        # unlike Codex's synthesized _deterministic_call_id, block.id can
        # be used directly as the stable call id.
        start_cb = getattr(agent, "tool_start_callback", None)
        if start_cb is not None:
            try:
                start_cb(block.id, name, args)
            except Exception:
                logger.debug(
                    "tool_start_callback raised for %s", name, exc_info=True,
                )

    def _fire_tool_completed(block) -> None:
        prior = started.pop(block.tool_use_id, None)
        name = prior[0] if prior is not None else "unknown"
        args = prior[1] if prior is not None else {}
        content = block.content
        if isinstance(content, list):
            content = "\n".join(
                str(part.get("text", part)) if isinstance(part, dict) else str(part)
                for part in content
            )
        is_error = bool(getattr(block, "is_error", False))
        cb = getattr(agent, "tool_progress_callback", None)
        if cb is not None:
            try:
                cb("tool.completed", name, None, None,
                   duration=None, is_error=is_error, result=content)
            except Exception:
                logger.debug(
                    "tool_progress_callback raised on tool.completed for %s",
                    name, exc_info=True,
                )
        complete_cb = getattr(agent, "tool_complete_callback", None)
        if complete_cb is not None:
            try:
                complete_cb(block.tool_use_id, name, args, content)
            except Exception:
                logger.debug(
                    "tool_complete_callback raised for %s", name, exc_info=True,
                )

    def _fire_text(text: str) -> None:
        fn = getattr(agent, "_fire_stream_delta", None)
        if fn is None:
            return
        try:
            fn(text)
        except Exception:
            logger.debug("_fire_stream_delta raised", exc_info=True)

    def on_event(event: dict) -> None:
        if not isinstance(event, dict) or event.get("type") != "raw_message":
            return
        message = event.get("message")
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            return
        for block in content:
            block_type = type(block).__name__
            if block_type == "ToolUseBlock":
                _fire_tool_started(block)
            elif block_type == "ToolResultBlock":
                _fire_tool_completed(block)
            elif block_type == "TextBlock":
                text = getattr(block, "text", "")
                if isinstance(text, str) and text:
                    _fire_text(text)

    return on_event


def run_claude_code_sdk_turn(
    agent,
    *,
    user_message: str,
    original_user_message: Any,
    messages: List[Dict[str, Any]],
    effective_task_id: str,
    should_review_memory: bool = False,
    session_factory: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """Claude Code SDK runtime path. Hands the entire turn to the real
    `claude` CLI (via claude-agent-sdk) and projects its events back into
    Hermes' messages list.

    Called from conversation_loop.py when agent.api_mode ==
    "claude_code_sdk". Returns the same dict shape as the chat_completions
    / codex_app_server paths.

    session_factory is an injectable session constructor for tests; production
    callers leave it None and this function constructs a real
    ClaudeCodeSdkTurnSession (added in Task 6/7/8 of this plan).
    """
    if not hasattr(agent, "_claude_code_session") or agent._claude_code_session is None:
        if session_factory is not None:
            agent._claude_code_session = session_factory(agent=agent)
        else:
            from agent.transports.claude_code_sdk_session import (
                ClaudeCodeSdkTurnSession,
            )

            cwd = getattr(agent, "session_cwd", None)
            agent._claude_code_session = ClaudeCodeSdkTurnSession(
                cwd=cwd,
                on_event=make_claude_code_sdk_event_bridge(agent),
            )

    try:
        turn = agent._claude_code_session.run_turn(user_input=user_message)
    except Exception as exc:
        logger.exception("claude code sdk turn failed")
        try:
            agent._claude_code_session.close()
        except Exception:
            pass
        agent._claude_code_session = None
        _user_interrupted = bool(getattr(agent, "_interrupt_requested", False))
        _interrupt_message = (
            getattr(agent, "_interrupt_message", None)
            if _user_interrupted
            else None
        )
        if _user_interrupted:
            agent.clear_interrupt()
        return {
            "final_response": (
                f"Claude Code SDK turn failed: {exc}. "
                f"Fall back to default runtime by unsetting "
                f"model.anthropic_runtime."
            ),
            "messages": messages,
            "api_calls": 0,
            "completed": False,
            "partial": True,
            "interrupted": _user_interrupted,
            **(
                {"interrupt_message": _interrupt_message}
                if _interrupt_message
                else {}
            ),
            "error": str(exc),
        }

    if getattr(turn, "should_retire", False):
        logger.warning(
            "claude code sdk session retired (turn error: %s)", turn.error
        )
        try:
            agent._claude_code_session.close()
        except Exception:
            pass
        agent._claude_code_session = None

    if turn.projected_messages:
        messages.extend(turn.projected_messages)

    # Mirror run_codex_app_server_turn's interrupt handoff: only a
    # user-driven interrupt (Ctrl+C / explicit stop) should surface as
    # "interrupted" to the caller. A turn.interrupted that was NOT paired
    # with agent._interrupt_requested (e.g. this session's own internal
    # turn-timeout deadline tripping in ClaudeCodeSdkTurnSession.run_turn)
    # is a transport-level condition, not a user cancellation, and must not
    # be reported as one — nor should it consume/clear an interrupt request
    # that was never made.
    _user_interrupted = bool(
        turn.interrupted and getattr(agent, "_interrupt_requested", False)
    )
    _interrupt_message = (
        getattr(agent, "_interrupt_message", None) if _user_interrupted else None
    )
    if _user_interrupted:
        agent.clear_interrupt()

    return {
        "final_response": turn.final_text,
        "messages": messages,
        "api_calls": 1,
        "completed": not turn.interrupted and turn.error is None,
        "partial": turn.interrupted or turn.error is not None,
        "interrupted": _user_interrupted,
        **(
            {"interrupt_message": _interrupt_message}
            if _interrupt_message
            else {}
        ),
        "error": turn.error,
        "agent_persisted": False,
    }


__all__ = ["run_claude_code_sdk_turn", "make_claude_code_sdk_event_bridge"]
