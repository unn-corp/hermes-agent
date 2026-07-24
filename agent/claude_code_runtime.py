"""Claude Code SDK runtime — mirrors agent/codex_runtime.py.

Each function takes the parent AIAgent as its first argument (agent).
AIAgent keeps a thin forwarder method (_run_claude_code_sdk_turn) for
consistency with the Codex app-server pattern.

Status: skeleton only in this task. Event bridging (make_claude_code_sdk_
event_bridge) and usage recording (_record_claude_code_sdk_usage) land in
follow-up tasks in this same plan; this task wires the lazy session
lifecycle and dispatch path only.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


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
            agent._claude_code_session = ClaudeCodeSdkTurnSession(cwd=cwd)

    try:
        turn = agent._claude_code_session.run_turn(user_input=user_message)
    except Exception as exc:
        logger.exception("claude code sdk turn failed")
        try:
            agent._claude_code_session.close()
        except Exception:
            pass
        agent._claude_code_session = None
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
            "interrupted": False,
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

    return {
        "final_response": turn.final_text,
        "messages": messages,
        "api_calls": 1,
        "completed": not turn.interrupted and turn.error is None,
        "partial": turn.interrupted or turn.error is not None,
        "interrupted": bool(turn.interrupted),
        "error": turn.error,
        "agent_persisted": False,
    }


__all__ = ["run_claude_code_sdk_turn"]
