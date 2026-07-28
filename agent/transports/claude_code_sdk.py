"""Claude Code CLI transport via claude-agent-sdk-python.

Wraps claude_agent_sdk.ClaudeSDKClient — a real asyncio-native PyPI package
(async def query(), async for message in client.receive_messages()) — inside
a dedicated background thread running its own asyncio event loop, exposing a
synchronous, blocking-queue facade.

This is a different shape than its sibling agent/transports/codex_app_server.py,
which hand-rolls a JSON-RPC-over-stdio wire client with zero package
dependency. That module's own comment explains Hermes deliberately avoids
asyncio in the main call path because AIAgent.run_conversation() is
synchronous. This module resolves the same tension by isolating all asyncio
usage inside its own thread — it never leaks upward.

Status: optional opt-in runtime gated behind `model.anthropic_runtime ==
"claude_code_sdk"`. Hermes' default tool dispatch is unchanged when this
runtime is not selected.
"""

from __future__ import annotations

import asyncio
import queue
import subprocess
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

from tools.environments.local import hermes_subprocess_env

# Floor chosen for stream-json + permission-hook + Task-tracking support in
# the CLI's own protocol surface (verified locally against claude 2.1.217).
# Bump this the same way MIN_CODEX_VERSION gets bumped: a one-line change.
MIN_CLAUDE_VERSION = (2, 0, 0)


def parse_claude_version(output: str) -> Optional[tuple[int, int, int]]:
    """Parse `claude --version` output. Returns (major, minor, patch) or None."""
    import re

    match = re.search(r"(\d+)\.(\d+)\.(\d+)", output or "")
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def check_claude_binary(
    claude_bin: str = "claude", min_version: tuple[int, int, int] = MIN_CLAUDE_VERSION
) -> tuple[bool, str]:
    """Verify the claude CLI is installed and meets the minimum version.

    Returns (ok, message). The claude-agent-sdk package version pin alone
    doesn't guarantee the `claude` CLI binary itself is present or
    compatible — the SDK shells out to whatever binary is on PATH (or
    cli_path) at runtime.
    """
    try:
        proc = subprocess.run(
            [claude_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return False, (
            f"claude CLI not found at {claude_bin!r}. Install with: "
            f"npm i -g @anthropic-ai/claude-code"
        )
    except subprocess.TimeoutExpired:
        return False, "claude --version timed out"
    if proc.returncode != 0:
        return False, f"claude --version exited {proc.returncode}: {proc.stderr.strip()}"
    version = parse_claude_version(proc.stdout)
    if version is None:
        return False, f"could not parse claude version from: {proc.stdout!r}"
    if version < min_version:
        return False, (
            f"claude {'.'.join(map(str, version))} is older than required "
            f"{'.'.join(map(str, min_version))}. Run: "
            f"npm i -g @anthropic-ai/claude-code"
        )
    return True, ".".join(map(str, version))


@dataclass
class ClaudeCodeSdkError(RuntimeError):
    """Raised on claude-agent-sdk connection/protocol errors."""

    message: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"claude code sdk error: {self.message}"


class ClaudeCodeSdkClient:
    """Synchronous facade over claude_agent_sdk.ClaudeSDKClient.

    Threading model: a single background thread runs its own asyncio event
    loop and owns the actual ClaudeSDKClient instance. The calling thread
    (AIAgent.run_conversation(), synchronous) never touches asyncio
    directly — it calls start()/send_turn()/take_event()/interrupt()/
    close(), all plain blocking calls. Mirrors CodexAppServerClient's
    blocking-queue-with-timeout facade (agent/transports/codex_app_server.py)
    even though the underlying wire mechanics are completely different
    (real asyncio SDK vs. hand-rolled JSON-RPC).
    """

    def __init__(
        self,
        claude_bin: str = "claude",
        claude_config_dir: Optional[str] = None,
        cwd: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        extra_args: Optional[dict[str, Optional[str]]] = None,
        resume: Optional[str] = None,
        can_use_tool: Optional[Callable[..., Any]] = None,
        client_factory: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        self._claude_bin = claude_bin
        self._claude_config_dir = claude_config_dir
        self._cwd = cwd
        self._extra_env = env
        # Free-text CLI flags from Settings, already parsed into the SDK's
        # dict[flag, value|None] shape by claude_code_runtime.
        self._extra_args = extra_args or {}
        # Claude-side conversation to reconnect to, so context survives a
        # restart (the CLI owns that history, not Hermes).
        self._resume = resume or None
        self._can_use_tool = can_use_tool
        self._client_factory = client_factory

        self._events: "queue.Queue[dict]" = queue.Queue()
        self._closed = False
        self._started = threading.Event()
        self._start_error: Optional[BaseException] = None
        # Set the instant the `async for message in
        # self._client.receive_messages():` loop in _async_main() exits, for
        # ANY reason — natural completion (e.g. the SDK's subprocess/stream
        # ended on its own), an exception, or the pre-existing self._closed
        # break. This is a general "the loop has stopped pumping" signal:
        # _start_error alone only covers a *failed* start, but the same
        # TOCTOU stall in close() (scheduling a disconnect on a loop that's
        # no longer being pumped but not yet .is_closed()) can also be
        # triggered by natural subprocess/stream exit after a *successful*
        # start, where _start_error stays None forever.
        self._loop_stopped_pumping = threading.Event()

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._client: Optional[Any] = None
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread_started = False

    # ---------- lifecycle ----------

    def start(self, timeout: float = 15.0) -> None:
        """Start the background thread + event loop and connect the SDK
        client. Idempotent — repeated calls are no-ops once started."""
        if self._thread_started:
            return
        self._thread_started = True
        self._thread.start()
        if not self._started.wait(timeout=timeout):
            raise ClaudeCodeSdkError(
                "claude code sdk client failed to start in time"
            )
        if self._start_error is not None:
            raise ClaudeCodeSdkError(str(self._start_error))

    def is_alive(self) -> bool:
        return self._thread.is_alive() and not self._closed

    def close(self, timeout: float = 3.0) -> None:
        if self._closed:
            return
        self._closed = True
        # If start() never completed successfully (_start_error set by
        # _async_main() before self._started.set()), the SDK client never
        # connected — there is nothing to disconnect. Likewise, if the
        # receive_messages() loop in _async_main() has already exited on its
        # own (self._loop_stopped_pumping set — natural subprocess/stream
        # exit, not just a connect failure), the loop is no longer being
        # pumped either. In both cases, skip scheduling the disconnect
        # coroutine entirely rather than trying to detect whether the loop
        # is still being pumped: checking `self._loop is not None and not
        # self._loop.is_closed()` alone is a TOCTOU race — _async_main() can
        # stop pumping (or set _started, unblocking start(), which raises)
        # before run_until_complete() actually returns and _run_loop()'s
        # `finally: loop.close()` runs on the background thread. If close()
        # lands in that window, the loop looks open but is no longer being
        # pumped, so run_coroutine_threadsafe() schedules a coroutine that
        # never executes and fut.result(timeout=timeout) blocks for the
        # full timeout before raising (silently swallowed below). close()
        # must skip the disconnect attempt whenever the loop has stopped
        # pumping for ANY reason, so it returns near-instantly every time.
        if (
            self._start_error is None
            and not self._loop_stopped_pumping.is_set()
            and self._loop is not None
            and not self._loop.is_closed()
            and self._client is not None
        ):

            async def _disconnect() -> None:
                await self._client.disconnect()

            try:
                fut = asyncio.run_coroutine_threadsafe(_disconnect(), self._loop)
                fut.result(timeout=timeout)
            except Exception:
                pass
        if self._loop is not None and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except RuntimeError:
                # Loop closed between the check above and this call (e.g.
                # _async_main() exited and _run_loop()'s finally already
                # tore it down) — nothing left to stop.
                pass
        if self._thread_started:
            self._thread.join(timeout=timeout)

    def __enter__(self) -> "ClaudeCodeSdkClient":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------- send/receive ----------

    def send_turn(self, user_input: str, *, session_id: str = "default") -> None:
        """Send a user message. Non-blocking on the SDK's own response —
        the reply streams in as events consumed via take_event()."""
        if self._loop is None or self._client is None:
            raise ClaudeCodeSdkError("client not started")

        async def _query() -> None:
            await self._client.query(user_input, session_id=session_id)

        fut = asyncio.run_coroutine_threadsafe(_query(), self._loop)
        fut.result(timeout=10.0)

    def take_event(self, timeout: float = 0.0) -> Optional[dict]:
        """Pop the next streamed event, or return None on timeout.

        timeout=0.0 means non-blocking. Use small positive timeouts inside
        the turn loop to interleave reads with interrupt checks, mirroring
        CodexAppServerClient.take_notification()."""
        try:
            if timeout <= 0:
                raw = self._events.get_nowait()
            else:
                raw = self._events.get(timeout=timeout)
        except queue.Empty:
            return None
        if "_raw" in raw:
            return {"type": "raw_message", "message": raw["_raw"]}
        return raw

    def interrupt(self) -> None:
        """Ask the SDK to interrupt the in-flight turn. Best-effort — safe
        to call when nothing is in flight."""
        if self._loop is None or self._client is None:
            return

        async def _interrupt() -> None:
            await self._client.interrupt()

        try:
            fut = asyncio.run_coroutine_threadsafe(_interrupt(), self._loop)
            fut.result(timeout=5.0)
        except Exception:
            pass

    # ---------- internals ----------

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._async_main())
        except BaseException as exc:  # pragma: no cover - defensive
            if not self._started.is_set():
                self._start_error = exc
                self._started.set()
        finally:
            loop.close()

    async def _async_main(self) -> None:
        factory = self._client_factory
        if factory is None:
            from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

            spawn_env = hermes_subprocess_env(inherit_credentials=True)
            if self._extra_env:
                spawn_env.update(self._extra_env)
            if self._claude_config_dir:
                spawn_env["CLAUDE_CONFIG_DIR"] = self._claude_config_dir
            options = ClaudeAgentOptions(
                cwd=self._cwd,
                cli_path=self._claude_bin,
                env=spawn_env,
                extra_args=self._extra_args,
                **({"resume": self._resume} if self._resume else {}),
                include_partial_messages=True,
                can_use_tool=self._can_use_tool,
            )
            self._client = ClaudeSDKClient(options=options)
        else:
            self._client = factory(None)

        try:
            await self._client.connect()
        except BaseException as exc:
            self._start_error = exc
            self._started.set()
            return
        self._started.set()

        try:
            async for message in self._client.receive_messages():
                if self._closed:
                    break
                self._events.put({"_raw": message})
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            pass
        except BaseException as exc:  # pragma: no cover - defensive
            self._events.put({"type": "transport_error", "error": str(exc)})
        finally:
            # Fires whenever this loop exits, for ANY reason: natural
            # completion of receive_messages() (subprocess/stream closed on
            # its own), an exception, or the self._closed break above. See
            # the comment on self._loop_stopped_pumping in __init__.
            self._loop_stopped_pumping.set()
