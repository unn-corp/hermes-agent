"""Session adapter for the claude_code_sdk runtime.

Owns one ClaudeCodeSdkClient per Hermes session. Drives send_turn(),
consumes streamed events via an on_event callback (the event bridge from
agent/claude_code_runtime.py), and returns a TurnResult AIAgent can splice
into its messages list. Mirrors
agent/transports/codex_app_server_session.py's CodexAppServerSession —
same lifecycle shape (ensure_started/run_turn/close), different wire
mechanics underneath (SDK message objects instead of JSON-RPC dicts).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from agent.transports.claude_code_sdk import ClaudeCodeSdkClient

logger = logging.getLogger(__name__)


@dataclass
class TurnResult:
    """Result of one user->assistant->tool turn through claude-agent-sdk."""

    final_text: str = ""
    projected_messages: list[dict] = field(default_factory=list)
    tool_iterations: int = 0
    interrupted: bool = False
    error: Optional[str] = None
    should_retire: bool = False
    result_message: Optional[Any] = None


class ClaudeCodeSdkTurnSession:
    """One Claude Code SDK client per Hermes session, lifetime owned by
    AIAgent. Not thread-safe — one caller drives it at a time, matching
    CodexAppServerSession."""

    def __init__(
        self,
        *,
        cwd: Optional[str] = None,
        claude_config_dir: Optional[str] = None,
        on_event: Optional[Callable[[dict], None]] = None,
        can_use_tool: Optional[Callable[..., Any]] = None,
        client_factory: Optional[Callable[..., ClaudeCodeSdkClient]] = None,
    ) -> None:
        self._cwd = cwd
        self._claude_config_dir = claude_config_dir
        self._on_event = on_event
        self._can_use_tool = can_use_tool
        self._client_factory = client_factory or ClaudeCodeSdkClient
        self._client: Optional[Any] = None
        self._closed = False

    def ensure_started(self) -> None:
        if self._client is not None:
            return
        self._client = self._client_factory(
            cwd=self._cwd,
            claude_config_dir=self._claude_config_dir,
            can_use_tool=self._can_use_tool,
        )
        self._client.start()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
            self._client = None

    def run_turn(
        self,
        user_input: str,
        *,
        turn_timeout: float = 600.0,
        event_poll_timeout: float = 0.25,
    ) -> TurnResult:
        result = TurnResult()
        try:
            self.ensure_started()
        except Exception as exc:
            result.error = f"claude code sdk startup failed: {exc}"
            result.should_retire = True
            return result

        assert self._client is not None
        self._client.send_turn(user_input, session_id="default")

        deadline = time.monotonic() + turn_timeout
        text_parts: list[str] = []

        while time.monotonic() < deadline:
            if not self._client.is_alive():
                result.error = "claude code sdk subprocess exited unexpectedly"
                result.should_retire = True
                break

            event = self._client.take_event(timeout=event_poll_timeout)
            if event is None:
                continue

            if self._on_event is not None:
                try:
                    self._on_event(event)
                except Exception:  # pragma: no cover - display callback
                    logger.debug("on_event callback raised", exc_info=True)

            if event.get("type") == "transport_error":
                result.error = event.get("error", "unknown transport error")
                result.should_retire = True
                break

            message = event.get("message")
            type_name = type(message).__name__
            if type_name == "AssistantMessage":
                for block in getattr(message, "content", []) or []:
                    if type(block).__name__ == "TextBlock":
                        text_parts.append(getattr(block, "text", ""))
                    elif type(block).__name__ == "ToolResultBlock":
                        result.tool_iterations += 1
            elif type_name == "ResultMessage":
                result.result_message = message
                result.final_text = "".join(text_parts)
                break

        else:
            result.error = f"turn timed out after {turn_timeout}s"
            result.should_retire = True
            self._client.interrupt()
            result.interrupted = True

        return result


__all__ = ["ClaudeCodeSdkTurnSession", "TurnResult"]
