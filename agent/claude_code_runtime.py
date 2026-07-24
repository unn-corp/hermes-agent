"""Claude Code SDK runtime — mirrors agent/codex_runtime.py.

Each function takes the parent AIAgent as its first argument (agent).
AIAgent keeps a thin forwarder method (_run_claude_code_sdk_turn) for
consistency with the Codex app-server pattern.

Includes the lazy session lifecycle, dispatch path, event bridging
(make_claude_code_sdk_event_bridge), and usage recording
(_record_claude_code_sdk_usage).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


def _coerce_usage_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float):
        return max(int(value), 0)
    return 0


def _record_claude_code_sdk_usage(agent, turn) -> dict[str, Any]:
    """Translate claude-agent-sdk ResultMessage usage into Hermes
    accounting. Mirrors agent.codex_runtime._record_codex_app_server_usage
    field-for-field, with one deliberate difference: Claude's usage block
    DOES report cache-write tokens (cacheCreationInputTokens), unlike Codex
    app-server, so cache_write_tokens is populated here instead of zeroed.

    Even when there's no result_message for a turn (e.g. it errored before
    a ResultMessage arrived), Hermes still counts the turn as one API call
    for session/status accounting.
    """
    agent.session_api_calls += 1

    result_message = getattr(turn, "result_message", None)
    usage = getattr(result_message, "usage", None) if result_message else None
    if not isinstance(usage, dict) or not usage:
        if agent._session_db and agent.session_id:
            try:
                if not agent._session_db_created:
                    agent._ensure_db_session()
                agent._session_db.update_token_counts(
                    agent.session_id,
                    model=agent.model,
                    billing_provider=agent.provider,
                    billing_base_url=agent.base_url,
                    billing_mode="subscription_included",
                    api_call_count=1,
                )
            except Exception as exc:
                logger.debug(
                    "Claude code sdk api-call persistence failed (session=%s): %s",
                    agent.session_id, exc,
                )
        return {}

    from agent.usage_pricing import CanonicalUsage, estimate_usage_cost

    input_tokens = _coerce_usage_int(usage.get("inputTokens"))
    cache_read_tokens = _coerce_usage_int(usage.get("cacheReadInputTokens"))
    cache_write_tokens = _coerce_usage_int(usage.get("cacheCreationInputTokens"))
    output_tokens = _coerce_usage_int(usage.get("outputTokens"))

    canonical_usage = CanonicalUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        reasoning_tokens=0,
        raw_usage=usage,
    )
    prompt_tokens = canonical_usage.prompt_tokens
    completion_tokens = canonical_usage.output_tokens
    total_tokens = canonical_usage.total_tokens
    usage_dict = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "input_tokens": canonical_usage.input_tokens,
        "output_tokens": canonical_usage.output_tokens,
        "cache_read_tokens": canonical_usage.cache_read_tokens,
        "cache_write_tokens": canonical_usage.cache_write_tokens,
        "reasoning_tokens": canonical_usage.reasoning_tokens,
    }

    agent.session_prompt_tokens += prompt_tokens
    agent.session_completion_tokens += completion_tokens
    agent.session_total_tokens += total_tokens
    agent.session_input_tokens += canonical_usage.input_tokens
    agent.session_output_tokens += canonical_usage.output_tokens
    agent.session_cache_read_tokens += canonical_usage.cache_read_tokens
    agent.session_cache_write_tokens += canonical_usage.cache_write_tokens
    agent.session_reasoning_tokens += canonical_usage.reasoning_tokens

    cost_result = estimate_usage_cost(
        agent.model,
        canonical_usage,
        provider=agent.provider,
        base_url=agent.base_url,
        api_key=getattr(agent, "api_key", ""),
    )
    if cost_result.amount_usd is not None:
        agent.session_estimated_cost_usd += float(cost_result.amount_usd)
    agent.session_cost_status = cost_result.status
    agent.session_cost_source = cost_result.source

    if agent._session_db and agent.session_id:
        try:
            if not agent._session_db_created:
                agent._ensure_db_session()
            agent._session_db.update_token_counts(
                agent.session_id,
                input_tokens=canonical_usage.input_tokens,
                output_tokens=canonical_usage.output_tokens,
                cache_read_tokens=canonical_usage.cache_read_tokens,
                cache_write_tokens=canonical_usage.cache_write_tokens,
                reasoning_tokens=canonical_usage.reasoning_tokens,
                estimated_cost_usd=float(cost_result.amount_usd)
                if cost_result.amount_usd is not None else None,
                cost_status=cost_result.status,
                cost_source=cost_result.source,
                billing_provider=agent.provider,
                billing_base_url=agent.base_url,
                billing_mode="subscription_included"
                if cost_result.status == "included" else None,
                model=agent.model,
                api_call_count=1,
            )
        except Exception as exc:
            logger.debug(
                "Claude code sdk token persistence failed (session=%s, tokens=%d): %s",
                agent.session_id, total_tokens, exc,
            )

    return {
        **usage_dict,
        "last_prompt_tokens": prompt_tokens,
        "estimated_cost_usd": float(cost_result.amount_usd)
        if cost_result.amount_usd is not None else None,
        "cost_status": cost_result.status,
        "cost_source": cost_result.source,
    }


def _make_claude_code_approval_callback(agent):
    """Build a can_use_tool callback for ClaudeAgentOptions, bridging
    Claude Code's own tool-permission prompts through Hermes' existing
    approval flow instead of letting the CLI use its own independent
    permission mode.

    Routes through the CLI-registered approval_callback
    (tools.terminal_tool._get_approval_callback) when one is wired up. When
    no callback is registered, this falls back to
    tools.approval.prompt_dangerous_approval — a CLI-oriented prompt (per
    its own docstring), unlike CodexAppServerSession._decide_exec_approval /
    _decide_apply_patch_approval, which fail closed to "decline" outright
    when no approval_callback is present (gateway/cron contexts have no UI
    to surface an approval request through; see agent.codex_runtime's
    comment on that deliberate fail-closed default). This fallback path has
    not yet been adapted for non-CLI (gateway/cron) contexts — a known
    follow-up, not a security gap, since prompt_dangerous_approval still
    resolves to deny on any error or timeout.
    """

    async def can_use_tool(tool_name, tool_input, context):
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
        from tools.approval import is_approval_bypass_active, prompt_dangerous_approval
        from tools.terminal_tool import _get_approval_callback

        try:
            if is_approval_bypass_active():
                return PermissionResultAllow(behavior="allow")
        except Exception:
            logger.debug(
                "claude code sdk: approval-bypass lookup failed; "
                "keeping fail-closed default",
                exc_info=True,
            )

        command = (
            tool_input.get("command")
            if isinstance(tool_input, dict)
            else None
        ) or tool_name
        description = f"Claude Code requests to use {tool_name}"

        approval_callback = None
        try:
            approval_callback = _get_approval_callback()
        except Exception:
            approval_callback = None

        try:
            if approval_callback is not None:
                choice = approval_callback(command, description, allow_permanent=False)
            else:
                choice = prompt_dangerous_approval(
                    command, description, allow_permanent=False
                )
        except Exception:
            logger.exception("claude code sdk approval callback raised")
            return PermissionResultDeny(
                behavior="deny", message="approval callback raised", interrupt=False
            )

        if choice in {"once", "session", "always"}:
            return PermissionResultAllow(behavior="allow")
        return PermissionResultDeny(
            behavior="deny", message="user declined", interrupt=False
        )

    return can_use_tool


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
                can_use_tool=_make_claude_code_approval_callback(agent),
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

    usage_result = _record_claude_code_sdk_usage(agent, turn)

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
        **usage_result,
    }


__all__ = ["run_claude_code_sdk_turn", "make_claude_code_sdk_event_bridge"]
