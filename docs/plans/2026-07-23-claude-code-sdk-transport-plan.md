# Claude Code CLI Transport (Phase 1 of 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Hermes a first-class `claude_code_sdk` api_mode that drives the real `claude` CLI (subscription/OAuth auth, not a raw API key) via `claude-agent-sdk-python`, wired the same way `codex_app_server` is wired today.

**Architecture:** A new `ClaudeCodeSdkClient` wraps the asyncio-native SDK inside a dedicated background thread + event loop, exposing a synchronous blocking-queue facade (mirrors `CodexAppServerClient`'s shape, different internals). A new `agent/claude_code_runtime.py` drives one turn per call (mirrors `agent/codex_runtime.py`), dispatched via the same early-return pattern in `conversation_loop.py`. Two separate `api_mode` gates (`agent_init.py`, `hermes_cli/runtime_provider.py`) both need the new mode added.

**Tech Stack:** Python 3, `claude-agent-sdk` 0.2.126 (new optional dependency, lazily imported), `pytest` + `pytest-asyncio` (already a dev dependency).

## Global Constraints

- Pin the new dependency exactly: `claude-agent-sdk==0.2.126` (current PyPI release, verified 2026-07-23).
- Import `claude_agent_sdk` lazily inside `agent/transports/claude_code_sdk.py` only — never at module top level — so users who never enable this runtime don't need it installed. This matches how the `anthropic` extra is handled elsewhere in the codebase.
- Never override `HOME` for account isolation — always use `CLAUDE_CONFIG_DIR`. Overriding `HOME` breaks the OS keychain OAuth lookup (this is why `agent/transports/codex_app_server.py` uses `CODEX_HOME`, not `HOME`, for the identical reason on the Codex side).
- All new subprocess environments must go through `tools.environments.local.hermes_subprocess_env(inherit_credentials=True)` — never `os.environ.copy()`. This is the shared Tier-1-secret-stripping helper; do not bypass it.
- `AIAgent.run_conversation()` is synchronous. No `asyncio` may leak past `agent/transports/claude_code_sdk.py`'s internal boundary — every public method on `ClaudeCodeSdkClient` is a plain blocking call.
- This is Phase 1 of 3 (transport + api_mode plumbing only). Multi-account (`cli_accounts.py`, live hot-swap) and sub-agent visibility (Desktop, transcript persistence) are separate, later plans — do not implement them here. The event bridge in this plan handles ordinary assistant text and tool calls only; it does not yet special-case `TaskStartedMessage`/`TaskUpdatedMessage`/`TaskProgressMessage`/`TaskNotificationMessage` (Phase 3's job).
- No AI attribution in any commit message (hard rule — do not add `Co-Authored-By` or similar trailers).
- Full spec: `hermes-agent/docs/design/claude-code-integration.md`.

---

### Task 1: `check_claude_binary()` — CLI presence/version check

**Files:**
- Create: `hermes-agent/agent/transports/claude_code_sdk.py`
- Test: `hermes-agent/tests/agent/transports/test_claude_code_sdk_binary.py`

**Interfaces:**
- Produces: `MIN_CLAUDE_VERSION: tuple[int, int, int]`, `parse_claude_version(output: str) -> Optional[tuple[int, int, int]]`, `check_claude_binary(claude_bin: str = "claude", min_version: tuple[int, int, int] = MIN_CLAUDE_VERSION) -> tuple[bool, str]`

- [ ] **Step 1: Write the failing tests**

```python
# hermes-agent/tests/agent/transports/test_claude_code_sdk_binary.py
from agent.transports.claude_code_sdk import (
    MIN_CLAUDE_VERSION,
    check_claude_binary,
    parse_claude_version,
)


def test_parse_claude_version_extracts_semver():
    assert parse_claude_version("2.1.217 (Claude Code)") == (2, 1, 217)


def test_parse_claude_version_returns_none_for_garbage():
    assert parse_claude_version("not a version string") is None


def test_check_claude_binary_not_found():
    ok, message = check_claude_binary(claude_bin="definitely-not-a-real-binary-xyz")
    assert ok is False
    assert "not found" in message
    assert "npm i -g @anthropic-ai/claude-code" in message


def test_check_claude_binary_rejects_old_version(monkeypatch):
    import subprocess as subprocess_module

    class _FakeCompletedProcess:
        returncode = 0
        stdout = "1.0.0 (Claude Code)"
        stderr = ""

    def _fake_run(*args, **kwargs):
        return _FakeCompletedProcess()

    monkeypatch.setattr(subprocess_module, "run", _fake_run)
    ok, message = check_claude_binary(min_version=(2, 0, 0))
    assert ok is False
    assert "older than required" in message


def test_check_claude_binary_accepts_current_version(monkeypatch):
    import subprocess as subprocess_module

    class _FakeCompletedProcess:
        returncode = 0
        stdout = "2.1.217 (Claude Code)"
        stderr = ""

    def _fake_run(*args, **kwargs):
        return _FakeCompletedProcess()

    monkeypatch.setattr(subprocess_module, "run", _fake_run)
    ok, message = check_claude_binary(min_version=(2, 0, 0))
    assert ok is True
    assert message == "2.1.217"


def test_min_claude_version_is_two_zero_zero():
    assert MIN_CLAUDE_VERSION == (2, 0, 0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd hermes-agent && python -m pytest tests/agent/transports/test_claude_code_sdk_binary.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent.transports.claude_code_sdk'`

- [ ] **Step 3: Write the minimal implementation**

```python
# hermes-agent/agent/transports/claude_code_sdk.py
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

import subprocess
from typing import Optional

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd hermes-agent && python -m pytest tests/agent/transports/test_claude_code_sdk_binary.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Add the dependency to pyproject.toml**

Edit `hermes-agent/pyproject.toml` — in `[project.optional-dependencies]`, immediately after the existing `anthropic = [...]` line (currently line 145), add:

```toml
# Claude Code CLI transport (claude_code_sdk api_mode) — only needed when
# model.anthropic_runtime: claude_code_sdk is enabled. Drives the real
# `claude` CLI via its own agent loop/tools/Task sub-agents, not the raw
# Messages API (see agent/anthropic_adapter.py for that separate path).
claude-code = ["claude-agent-sdk==0.2.126"]
```

- [ ] **Step 6: Commit**

```bash
cd hermes-agent
git add agent/transports/claude_code_sdk.py tests/agent/transports/test_claude_code_sdk_binary.py pyproject.toml
git commit -m "feat: add claude CLI binary/version check for claude_code_sdk transport"
```

---

### Task 2: `ClaudeCodeSdkClient` — background thread + event loop lifecycle

**Files:**
- Modify: `hermes-agent/agent/transports/claude_code_sdk.py`
- Test: `hermes-agent/tests/agent/transports/test_claude_code_sdk_client_lifecycle.py`

**Interfaces:**
- Consumes: nothing new from Task 1 besides the module itself existing.
- Produces: `class ClaudeCodeSdkError(RuntimeError)`, `class ClaudeCodeSdkClient` with `__init__(self, claude_bin="claude", claude_config_dir=None, cwd=None, env=None, can_use_tool=None, client_factory=None)`, `.start(timeout=15.0) -> None`, `.is_alive() -> bool`, `.close(timeout=3.0) -> None`, context-manager support. `client_factory` is an injectable async-client constructor for tests — production code leaves it `None` and the class lazily imports the real `claude_agent_sdk.ClaudeSDKClient`. This task does NOT yet implement `send_turn`/`take_event`/`interrupt` (Task 3) — `start()` in this task only proves the thread+loop+fake-client wiring works.

- [ ] **Step 1: Write the failing tests**

```python
# hermes-agent/tests/agent/transports/test_claude_code_sdk_client_lifecycle.py
import time

import pytest

from agent.transports.claude_code_sdk import ClaudeCodeSdkClient, ClaudeCodeSdkError


class _FakeSdkClient:
    """Stand-in for claude_agent_sdk.ClaudeSDKClient — records connect/
    disconnect calls and never actually spawns a subprocess."""

    def __init__(self, options=None):
        self.options = options
        self.connected = False
        self.disconnected = False

    async def connect(self, prompt=None):
        self.connected = True

    async def disconnect(self):
        self.disconnected = True

    async def receive_messages(self):
        # Async generator that yields nothing and blocks until cancelled —
        # close() should still be able to tear this down cleanly.
        import asyncio

        while True:
            await asyncio.sleep(0.01)
            yield None  # pragma: no cover - never reached in these tests


class _FailingFakeSdkClient(_FakeSdkClient):
    async def connect(self, prompt=None):
        raise RuntimeError("boom: simulated connect failure")


def test_start_spawns_thread_and_connects():
    client = ClaudeCodeSdkClient(client_factory=lambda options: _FakeSdkClient(options))
    client.start(timeout=5.0)
    try:
        assert client.is_alive() is True
    finally:
        client.close(timeout=3.0)
    assert client.is_alive() is False


def test_start_is_idempotent():
    client = ClaudeCodeSdkClient(client_factory=lambda options: _FakeSdkClient(options))
    client.start(timeout=5.0)
    try:
        client.start(timeout=5.0)  # second call must not raise or double-spawn
        assert client.is_alive() is True
    finally:
        client.close(timeout=3.0)


def test_start_raises_claude_code_sdk_error_on_connect_failure():
    client = ClaudeCodeSdkClient(
        client_factory=lambda options: _FailingFakeSdkClient(options)
    )
    with pytest.raises(ClaudeCodeSdkError, match="boom: simulated connect failure"):
        client.start(timeout=5.0)


def test_context_manager_starts_and_closes():
    fake_holder = {}

    def _factory(options):
        fake_holder["client"] = _FakeSdkClient(options)
        return fake_holder["client"]

    with ClaudeCodeSdkClient(client_factory=_factory) as client:
        assert client.is_alive() is True
        time.sleep(0.05)
    assert fake_holder["client"].disconnected is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd hermes-agent && python -m pytest tests/agent/transports/test_claude_code_sdk_client_lifecycle.py -v`
Expected: FAIL with `ImportError: cannot import name 'ClaudeCodeSdkClient'`

- [ ] **Step 3: Write the minimal implementation**

Add to `hermes-agent/agent/transports/claude_code_sdk.py` (after the existing binary-check functions):

```python
import asyncio
import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

from tools.environments.local import hermes_subprocess_env


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
        can_use_tool: Optional[Callable[..., Any]] = None,
        client_factory: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        self._claude_bin = claude_bin
        self._claude_config_dir = claude_config_dir
        self._cwd = cwd
        self._extra_env = env
        self._can_use_tool = can_use_tool
        self._client_factory = client_factory

        self._events: "queue.Queue[dict]" = queue.Queue()
        self._closed = False
        self._started = threading.Event()
        self._start_error: Optional[BaseException] = None

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
        if self._loop is not None and self._client is not None:

            async def _disconnect() -> None:
                await self._client.disconnect()

            try:
                fut = asyncio.run_coroutine_threadsafe(_disconnect(), self._loop)
                fut.result(timeout=timeout)
            except Exception:
                pass
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=timeout)

    def __enter__(self) -> "ClaudeCodeSdkClient":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd hermes-agent && python -m pytest tests/agent/transports/test_claude_code_sdk_client_lifecycle.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
cd hermes-agent
git add agent/transports/claude_code_sdk.py tests/agent/transports/test_claude_code_sdk_client_lifecycle.py
git commit -m "feat: add ClaudeCodeSdkClient thread+event-loop lifecycle"
```

---

### Task 3: `send_turn` / `take_event` / `interrupt` — driving real turns

**Files:**
- Modify: `hermes-agent/agent/transports/claude_code_sdk.py`
- Test: `hermes-agent/tests/agent/transports/test_claude_code_sdk_client_turns.py`

**Interfaces:**
- Consumes: `ClaudeCodeSdkClient` from Task 2 (`_client_factory`, `_loop`, `_events`, `_closed`).
- Produces: `.send_turn(user_input: str, *, session_id: str = "default") -> None`, `.take_event(timeout: float = 0.0) -> Optional[dict]`, `.interrupt() -> None`. `take_event`'s returned dict always has a `"type"` key: `"raw_message"` (wraps an SDK message object under `"message"`) or `"transport_error"` (wraps a string under `"error"`).

- [ ] **Step 1: Write the failing tests**

```python
# hermes-agent/tests/agent/transports/test_claude_code_sdk_client_turns.py
import asyncio

from agent.transports.claude_code_sdk import ClaudeCodeSdkClient


class _RecordingFakeSdkClient:
    def __init__(self, options=None):
        self.options = options
        self.queried_with: list[tuple[str, str]] = []
        self.interrupted = False
        self._messages: "asyncio.Queue" = asyncio.Queue()

    async def connect(self, prompt=None):
        pass

    async def disconnect(self):
        pass

    async def query(self, prompt, session_id="default"):
        self.queried_with.append((prompt, session_id))
        # Simulate the SDK emitting one message per query() call.
        await self._messages.put({"kind": "fake_assistant_text", "text": prompt})

    async def interrupt(self):
        self.interrupted = True

    async def receive_messages(self):
        while True:
            message = await self._messages.get()
            yield message


def test_send_turn_calls_query_and_take_event_returns_it():
    holder = {}

    def _factory(options):
        holder["client"] = _RecordingFakeSdkClient(options)
        return holder["client"]

    client = ClaudeCodeSdkClient(client_factory=_factory)
    client.start(timeout=5.0)
    try:
        client.send_turn("hello claude")
        event = client.take_event(timeout=2.0)
        assert event is not None
        assert event["type"] == "raw_message"
        assert event["message"] == {"kind": "fake_assistant_text", "text": "hello claude"}
        assert holder["client"].queried_with == [("hello claude", "default")]
    finally:
        client.close(timeout=3.0)


def test_take_event_returns_none_when_empty():
    client = ClaudeCodeSdkClient(
        client_factory=lambda options: _RecordingFakeSdkClient(options)
    )
    client.start(timeout=5.0)
    try:
        assert client.take_event(timeout=0.1) is None
    finally:
        client.close(timeout=3.0)


def test_interrupt_calls_sdk_interrupt():
    holder = {}

    def _factory(options):
        holder["client"] = _RecordingFakeSdkClient(options)
        return holder["client"]

    client = ClaudeCodeSdkClient(client_factory=_factory)
    client.start(timeout=5.0)
    try:
        client.interrupt()
        assert holder["client"].interrupted is True
    finally:
        client.close(timeout=3.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd hermes-agent && python -m pytest tests/agent/transports/test_claude_code_sdk_client_turns.py -v`
Expected: FAIL with `AttributeError: 'ClaudeCodeSdkClient' object has no attribute 'send_turn'`

- [ ] **Step 3: Write the minimal implementation**

Add to the `ClaudeCodeSdkClient` class in `hermes-agent/agent/transports/claude_code_sdk.py` (after `close`/`__exit__`, before the `# ---------- internals ----------` marker):

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd hermes-agent && python -m pytest tests/agent/transports/test_claude_code_sdk_client_turns.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the full transport test suite so far**

Run: `cd hermes-agent && python -m pytest tests/agent/transports/test_claude_code_sdk_binary.py tests/agent/transports/test_claude_code_sdk_client_lifecycle.py tests/agent/transports/test_claude_code_sdk_client_turns.py -v`
Expected: PASS (13 tests total)

- [ ] **Step 6: Commit**

```bash
cd hermes-agent
git add agent/transports/claude_code_sdk.py tests/agent/transports/test_claude_code_sdk_client_turns.py
git commit -m "feat: add send_turn/take_event/interrupt to ClaudeCodeSdkClient"
```

---

### Task 4: `api_mode` plumbing — both gates

**Files:**
- Modify: `hermes-agent/agent/agent_init.py:581`
- Modify: `hermes-agent/hermes_cli/runtime_provider.py:349` (add to `_VALID_API_MODES`), and after `_maybe_apply_codex_app_server_runtime` (currently ending ~line 400)
- Test: `hermes-agent/tests/agent/test_agent_init_api_mode.py`
- Test: `hermes-agent/tests/hermes_cli/test_runtime_provider_claude_code_sdk.py`

**Interfaces:**
- Produces: `agent.api_mode` accepts `"claude_code_sdk"`; `hermes_cli.runtime_provider._VALID_API_MODES` includes `"claude_code_sdk"`; `hermes_cli.runtime_provider._maybe_apply_claude_code_sdk_runtime(*, provider: str, api_mode: str, model_cfg: Optional[Dict[str, Any]]) -> str`.

- [ ] **Step 1: Write the failing test for `agent_init.py`**

```python
# hermes-agent/tests/agent/test_agent_init_api_mode.py
import run_agent


def _make_claude_code_agent(**kwargs):
    """Construct an AIAgent in claude_code_sdk mode without contacting any
    real provider. Mirrors tests/run_agent/test_codex_app_server_integration.py's
    _make_codex_agent — api_key/base_url are stubs so the constructor takes
    the fast path for direct credentials instead of resolving a real pool."""
    return run_agent.AIAgent(
        api_key="stub",
        base_url="https://stub.invalid",
        provider="anthropic",
        api_mode="claude_code_sdk",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        **kwargs,
    )


def test_claude_code_sdk_api_mode_is_accepted():
    agent = _make_claude_code_agent()
    assert agent.api_mode == "claude_code_sdk"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd hermes-agent && python -m pytest tests/agent/test_agent_init_api_mode.py -v`
Expected: FAIL — `agent.api_mode` falls through to a different branch (not `"claude_code_sdk"`) because the set at `agent_init.py:581` doesn't include it yet.

- [ ] **Step 3: Write the minimal implementation**

Edit `hermes-agent/agent/agent_init.py` line 581, changing:

```python
    if api_mode in {"chat_completions", "codex_responses", "anthropic_messages", "bedrock_converse", "codex_app_server"}:
```

to:

```python
    if api_mode in {"chat_completions", "codex_responses", "anthropic_messages", "bedrock_converse", "codex_app_server", "claude_code_sdk"}:
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd hermes-agent && python -m pytest tests/agent/test_agent_init_api_mode.py -v`
Expected: PASS

- [ ] **Step 5: Write the failing test for `runtime_provider.py`**

```python
# hermes-agent/tests/hermes_cli/test_runtime_provider_claude_code_sdk.py
from hermes_cli.runtime_provider import (
    _VALID_API_MODES,
    _maybe_apply_claude_code_sdk_runtime,
)


def test_claude_code_sdk_is_a_valid_api_mode():
    assert "claude_code_sdk" in _VALID_API_MODES


def test_maybe_apply_claude_code_sdk_runtime_noop_when_unset():
    result = _maybe_apply_claude_code_sdk_runtime(
        provider="anthropic", api_mode="anthropic_messages", model_cfg={}
    )
    assert result == "anthropic_messages"


def test_maybe_apply_claude_code_sdk_runtime_noop_for_wrong_provider():
    result = _maybe_apply_claude_code_sdk_runtime(
        provider="openai",
        api_mode="chat_completions",
        model_cfg={"anthropic_runtime": "claude_code_sdk"},
    )
    assert result == "chat_completions"


def test_maybe_apply_claude_code_sdk_runtime_rewrites_when_enabled():
    result = _maybe_apply_claude_code_sdk_runtime(
        provider="anthropic",
        api_mode="anthropic_messages",
        model_cfg={"anthropic_runtime": "claude_code_sdk"},
    )
    assert result == "claude_code_sdk"


def test_maybe_apply_claude_code_sdk_runtime_handles_none_config():
    result = _maybe_apply_claude_code_sdk_runtime(
        provider="anthropic", api_mode="anthropic_messages", model_cfg=None
    )
    assert result == "anthropic_messages"
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `cd hermes-agent && python -m pytest tests/hermes_cli/test_runtime_provider_claude_code_sdk.py -v`
Expected: FAIL with `ImportError: cannot import name '_maybe_apply_claude_code_sdk_runtime'`

- [ ] **Step 7: Write the minimal implementation**

Edit `hermes-agent/hermes_cli/runtime_provider.py`. In `_VALID_API_MODES` (line ~349), add the new mode with a comment mirroring the existing `codex_app_server` comment:

```python
_VALID_API_MODES = {
    "chat_completions",
    "codex_responses",
    "anthropic_messages",
    "bedrock_converse",
    # Optional opt-in: hand the entire turn to a `codex app-server` subprocess
    # so terminal/file-ops/patching/sandboxing run inside Codex's own runtime
    # instead of Hermes' tool dispatch. Gated behind config key
    # `model.openai_runtime == "codex_app_server"` AND provider in
    # {"openai", "openai-codex"}. Default is unchanged.
    "codex_app_server",
    # Optional opt-in: hand the entire turn to the real `claude` CLI via
    # claude-agent-sdk, using the CLI's own subscription/OAuth auth instead
    # of a raw Anthropic API key. Gated behind config key
    # `model.anthropic_runtime == "claude_code_sdk"` AND provider ==
    # "anthropic". Default is unchanged.
    "claude_code_sdk",
}
```

Immediately after `_maybe_apply_codex_app_server_runtime` (ends ~line 400, right before `_resolve_runtime_from_pool_entry`), add:

```python
def _maybe_apply_claude_code_sdk_runtime(
    *,
    provider: str,
    api_mode: str,
    model_cfg: Optional[Dict[str, Any]],
) -> str:
    """Optional opt-in: rewrite api_mode -> "claude_code_sdk" for the
    Anthropic provider when the user has explicitly enabled that runtime via
    `model.anthropic_runtime: claude_code_sdk` in config.yaml.

    Direct sibling of _maybe_apply_codex_app_server_runtime — same location
    in config.yaml, same on/off semantics, same no-op-by-default contract.
    Only provider == "anthropic" is eligible.

    Returns the (possibly-rewritten) api_mode."""
    if not model_cfg:
        return api_mode
    if provider != "anthropic":
        return api_mode
    runtime = str(model_cfg.get("anthropic_runtime") or "").strip().lower()
    if runtime == "claude_code_sdk":
        return "claude_code_sdk"
    return api_mode
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `cd hermes-agent && python -m pytest tests/hermes_cli/test_runtime_provider_claude_code_sdk.py -v`
Expected: PASS (4 tests)

- [ ] **Step 9: Wire the new rewrite function into `_resolve_runtime_from_pool_entry`**

Edit `hermes-agent/hermes_cli/runtime_provider.py`. The existing call (currently right before `if provider == "lmstudio":`, ~line 538) is:

```python
    # Optional opt-in: route OpenAI/Codex turns through `codex app-server`.
    # Inert when `model.openai_runtime` is unset or "auto".
    api_mode = _maybe_apply_codex_app_server_runtime(
        provider=provider, api_mode=api_mode, model_cfg=model_cfg
    )
```

Add immediately after it:

```python
    # Optional opt-in: route Anthropic turns through the real `claude` CLI.
    # Inert when `model.anthropic_runtime` is unset or "auto".
    api_mode = _maybe_apply_claude_code_sdk_runtime(
        provider=provider, api_mode=api_mode, model_cfg=model_cfg
    )
```

- [ ] **Step 10: Write and run a test confirming the end-to-end resolution picks `claude_code_sdk`**

Append to `hermes-agent/tests/hermes_cli/test_runtime_provider_claude_code_sdk.py`:

```python
from agent.credential_pool import PooledCredential
from hermes_cli.runtime_provider import _resolve_runtime_from_pool_entry


def _make_anthropic_entry() -> PooledCredential:
    return PooledCredential(
        provider="anthropic",
        id="entry-1",
        label="test",
        auth_type="api_key",
        priority=0,
        source="manual",
        access_token="sk-ant-stub",
    )


def test_resolve_runtime_picks_claude_code_sdk_when_enabled():
    resolved = _resolve_runtime_from_pool_entry(
        provider="anthropic",
        entry=_make_anthropic_entry(),
        requested_provider="anthropic",
        model_cfg={"anthropic_runtime": "claude_code_sdk", "default": "claude-sonnet-5"},
    )
    assert resolved["api_mode"] == "claude_code_sdk"


def test_resolve_runtime_keeps_anthropic_messages_when_runtime_unset():
    resolved = _resolve_runtime_from_pool_entry(
        provider="anthropic",
        entry=_make_anthropic_entry(),
        requested_provider="anthropic",
        model_cfg={"default": "claude-sonnet-5"},
    )
    assert resolved["api_mode"] == "anthropic_messages"
```

(If `PooledCredential` rejects any of these keyword args or `_resolve_runtime_from_pool_entry`'s return dict doesn't have an `"api_mode"` key under that exact name, read the dataclass fields at `agent/credential_pool.py:165-189` and the `return {` block at the end of `_resolve_runtime_from_pool_entry` in `hermes_cli/runtime_provider.py` to correct the field/key names — both were confirmed present at plan-writing time.)

- [ ] **Step 11: Run the full test file and confirm no regressions**

Run: `cd hermes-agent && python -m pytest tests/hermes_cli/test_runtime_provider_claude_code_sdk.py tests/agent/test_agent_init_api_mode.py -v`
Expected: PASS (6 tests total)

- [ ] **Step 12: Commit**

```bash
cd hermes-agent
git add agent/agent_init.py hermes_cli/runtime_provider.py tests/agent/test_agent_init_api_mode.py tests/hermes_cli/test_runtime_provider_claude_code_sdk.py
git commit -m "feat: add claude_code_sdk api_mode gating in agent_init and runtime_provider"
```

---

### Task 5: Dispatch wiring — `conversation_loop.py` + `AIAgent` forwarder + runtime module skeleton

**Files:**
- Modify: `hermes-agent/agent/conversation_loop.py` (near line 813, right after the existing `codex_app_server` early-return block)
- Modify: `hermes-agent/run_agent.py` (near line 6678, right after `_run_codex_app_server_turn`)
- Create: `hermes-agent/agent/claude_code_runtime.py`
- Test: `hermes-agent/tests/agent/test_claude_code_runtime_dispatch.py`

**Interfaces:**
- Consumes: `ClaudeCodeSdkClient` (Task 2/3).
- Produces: `agent.claude_code_runtime.run_claude_code_sdk_turn(agent, *, user_message, original_user_message, messages, effective_task_id, should_review_memory=False) -> Dict[str, Any]` (same signature shape as `run_codex_app_server_turn`), `AIAgent._run_claude_code_sdk_turn(self, ...)` forwarder, dispatch branch in `conversation_loop.py`.
- This task wires the **skeleton only**: a session is created and a turn is attempted, but the event bridge (Task 6) and usage recording (Task 7) aren't wired in yet — `run_claude_code_sdk_turn` calls placeholder no-op stand-ins for those two, replaced in later tasks. This keeps each task's diff reviewable against a single responsibility.

- [ ] **Step 1: Write the failing test**

```python
# hermes-agent/tests/agent/test_claude_code_runtime_dispatch.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd hermes-agent && python -m pytest tests/agent/test_claude_code_runtime_dispatch.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent.claude_code_runtime'`

- [ ] **Step 3: Write the minimal implementation**

```python
# hermes-agent/agent/claude_code_runtime.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd hermes-agent && python -m pytest tests/agent/test_claude_code_runtime_dispatch.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Wire the `conversation_loop.py` dispatch**

Edit `hermes-agent/agent/conversation_loop.py` immediately after the existing block (currently ~line 813-820):

```python
    if agent.api_mode == "codex_app_server":
        return agent._run_codex_app_server_turn(
            user_message=user_message,
            original_user_message=original_user_message,
            messages=messages,
            effective_task_id=effective_task_id,
            should_review_memory=_should_review_memory,
        )
```

add immediately below it:

```python
    if agent.api_mode == "claude_code_sdk":
        return agent._run_claude_code_sdk_turn(
            user_message=user_message,
            original_user_message=original_user_message,
            messages=messages,
            effective_task_id=effective_task_id,
            should_review_memory=_should_review_memory,
        )
```

- [ ] **Step 6: Add the `AIAgent` forwarder method**

Edit `hermes-agent/run_agent.py` immediately after `_run_codex_app_server_turn` (currently ending right before `def main(` at the module level, i.e. still inside the `AIAgent` class body):

```python
    def _run_claude_code_sdk_turn(
        self,
        *,
        user_message: str,
        original_user_message: Any,
        messages: List[Dict[str, Any]],
        effective_task_id: str,
        should_review_memory: bool = False,
    ) -> Dict[str, Any]:
        """Forwarder — see ``agent.claude_code_runtime.run_claude_code_sdk_turn``."""
        from agent.claude_code_runtime import run_claude_code_sdk_turn
        return run_claude_code_sdk_turn(self, user_message=user_message, original_user_message=original_user_message, messages=messages, effective_task_id=effective_task_id, should_review_memory=should_review_memory)
```

- [ ] **Step 7: Write and run an end-to-end dispatch test**

```python
# Append to hermes-agent/tests/agent/test_claude_code_runtime_dispatch.py
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
```

Run: `cd hermes-agent && python -m pytest tests/agent/test_claude_code_runtime_dispatch.py -v`
Expected: PASS (3 tests)

- [ ] **Step 8: Commit**

```bash
cd hermes-agent
git add agent/claude_code_runtime.py agent/conversation_loop.py run_agent.py tests/agent/test_claude_code_runtime_dispatch.py
git commit -m "feat: wire claude_code_sdk turn dispatch and lazy session lifecycle"
```

---

### Task 6: `ClaudeCodeSdkTurnSession` + event bridge — text and tool-call projection

**Files:**
- Create: `hermes-agent/agent/transports/claude_code_sdk_session.py`
- Modify: `hermes-agent/agent/claude_code_runtime.py` (add `make_claude_code_sdk_event_bridge`, wire it into the default `session_factory` path)
- Test: `hermes-agent/tests/agent/transports/test_claude_code_sdk_session.py`
- Test: `hermes-agent/tests/agent/test_claude_code_sdk_event_bridge.py`

**Interfaces:**
- Consumes: `ClaudeCodeSdkClient` (Task 2/3), real SDK dataclasses `AssistantMessage`/`TextBlock`/`ToolUseBlock`/`ToolResultBlock`/`ResultMessage` (verified field shapes: `AssistantMessage.content: list[TextBlock|ToolUseBlock|ToolResultBlock|...]`, `ToolUseBlock.{id,name,input}`, `ToolResultBlock.{tool_use_id,content,is_error}`).
- Produces: `class ClaudeCodeSdkTurnSession` with `__init__(self, *, cwd=None, claude_config_dir=None, on_event=None, client_factory=None)`, `.run_turn(user_input: str, *, turn_timeout: float = 600.0) -> TurnResult`, `.close() -> None`. `TurnResult` dataclass: `final_text: str = ""`, `projected_messages: list[dict] = field(default_factory=list)`, `tool_iterations: int = 0`, `interrupted: bool = False`, `error: Optional[str] = None`, `should_retire: bool = False`, `result_message: Optional[Any] = None` (the raw `ResultMessage`, consumed by Task 7's usage recording). `make_claude_code_sdk_event_bridge(agent) -> Callable[[dict], None]` — takes the same `{"type": "raw_message"/"transport_error", ...}` shape `ClaudeCodeSdkClient.take_event()` returns.

- [ ] **Step 1: Write the failing test for the event bridge**

```python
# hermes-agent/tests/agent/test_claude_code_sdk_event_bridge.py
from unittest.mock import MagicMock

from agent.claude_code_runtime import make_claude_code_sdk_event_bridge


class _FakeToolUseBlock:
    def __init__(self, id, name, input):
        self.id = id
        self.name = name
        self.input = input


class _FakeToolResultBlock:
    def __init__(self, tool_use_id, content, is_error=False):
        self.tool_use_id = tool_use_id
        self.content = content
        self.is_error = is_error


class _FakeTextBlock:
    def __init__(self, text):
        self.text = text


class _FakeAssistantMessage:
    def __init__(self, content):
        self.content = content
        self.parent_tool_use_id = None


def test_bridge_fires_tool_progress_for_tool_use_block():
    agent = MagicMock()
    bridge = make_claude_code_sdk_event_bridge(agent)

    message = _FakeAssistantMessage(
        content=[_FakeToolUseBlock(id="tu_1", name="Bash", input={"command": "ls"})]
    )
    bridge({"type": "raw_message", "message": message})

    agent.tool_progress_callback.assert_any_call(
        "tool.started", "Bash", "ls", {"command": "ls"}
    )


def test_bridge_fires_tool_completed_for_tool_result_block():
    agent = MagicMock()
    bridge = make_claude_code_sdk_event_bridge(agent)

    started = _FakeAssistantMessage(
        content=[_FakeToolUseBlock(id="tu_2", name="Read", input={"file_path": "/x"})]
    )
    bridge({"type": "raw_message", "message": started})

    completed = _FakeAssistantMessage(
        content=[_FakeToolResultBlock(tool_use_id="tu_2", content="file contents", is_error=False)]
    )
    bridge({"type": "raw_message", "message": completed})

    agent.tool_progress_callback.assert_any_call(
        "tool.completed", "Read", None, None,
        duration=None, is_error=False, result="file contents",
    )


def test_bridge_fires_stream_delta_for_text_block():
    agent = MagicMock()
    bridge = make_claude_code_sdk_event_bridge(agent)

    message = _FakeAssistantMessage(content=[_FakeTextBlock(text="hello there")])
    bridge({"type": "raw_message", "message": message})

    agent._fire_stream_delta.assert_called_once_with("hello there")


def test_bridge_never_raises_on_callback_exception():
    agent = MagicMock()
    agent.tool_progress_callback.side_effect = RuntimeError("boom")
    bridge = make_claude_code_sdk_event_bridge(agent)

    message = _FakeAssistantMessage(
        content=[_FakeToolUseBlock(id="tu_3", name="Bash", input={})]
    )
    bridge({"type": "raw_message", "message": message})  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd hermes-agent && python -m pytest tests/agent/test_claude_code_sdk_event_bridge.py -v`
Expected: FAIL with `ImportError: cannot import name 'make_claude_code_sdk_event_bridge'`

- [ ] **Step 3: Write the minimal implementation of the event bridge**

Add to `hermes-agent/agent/claude_code_runtime.py` (`Callable` is already
imported at the top of the file from Task 5's `from typing import Any,
Callable, Dict, List, Optional`):

```python
def make_claude_code_sdk_event_bridge(agent) -> Callable[[dict], None]:
    """Build an on_event callback wiring claude-agent-sdk messages into
    Hermes' gateway UI callbacks. Mirrors
    agent.codex_runtime.make_codex_app_server_event_bridge — same three
    target callbacks (tool_progress_callback, _fire_stream_delta,
    _emit_interim_assistant_message), different source message shapes
    (SDK dataclasses instead of codex JSON-RPC dicts).

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
    # completed-bubble can report the tool name without re-deriving it.
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

    def _fire_tool_completed(block) -> None:
        prior = started.pop(block.tool_use_id, None)
        name = prior[0] if prior is not None else "unknown"
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd hermes-agent && python -m pytest tests/agent/test_claude_code_sdk_event_bridge.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Write the failing test for `ClaudeCodeSdkTurnSession`**

```python
# hermes-agent/tests/agent/transports/test_claude_code_sdk_session.py
from unittest.mock import MagicMock

from agent.transports.claude_code_sdk_session import ClaudeCodeSdkTurnSession


class _FakeResultMessage:
    def __init__(self):
        self.usage = {"inputTokens": 10, "outputTokens": 5}
        self.model_usage = None
        self.total_cost_usd = 0.001
        self.is_error = False


def test_run_turn_sends_input_and_collects_final_text():
    fake_client = MagicMock()
    fake_client.is_alive.return_value = True
    events = [
        {"type": "raw_message", "message": _FakeResultMessage()},
    ]
    fake_client.take_event.side_effect = lambda timeout=0.0: (
        events.pop(0) if events else None
    )

    session = ClaudeCodeSdkTurnSession(client_factory=lambda **kw: fake_client)
    session._final_text_hook = lambda msg: "assembled final text"  # test seam, see Step 3

    result = session.run_turn(user_input="hello", turn_timeout=2.0)

    fake_client.start.assert_called_once()
    fake_client.send_turn.assert_called_once_with("hello", session_id="default")
    assert result.error is None
    assert result.should_retire is False
```

(This test is deliberately loose on `final_text` assembly — Task 6 focuses on wiring the loop and `ResultMessage` termination; exact final-text assembly from streamed `TextBlock`s is exercised by the event-bridge tests in Step 1-4 and by Task 7's usage test. The `_final_text_hook` seam lets this test avoid over-specifying internal text-accumulation implementation.)

- [ ] **Step 6: Run test to verify it fails**

Run: `cd hermes-agent && python -m pytest tests/agent/transports/test_claude_code_sdk_session.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent.transports.claude_code_sdk_session'`

- [ ] **Step 7: Write the minimal implementation**

```python
# hermes-agent/agent/transports/claude_code_sdk_session.py
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
                    elif type(block).__name__ in {"ToolUseBlock", "ToolResultBlock"}:
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
```

- [ ] **Step 8: Adjust the test to match the real implementation (remove the test seam)**

Replace the test written in Step 5 with:

```python
# hermes-agent/tests/agent/transports/test_claude_code_sdk_session.py
from unittest.mock import MagicMock

from agent.transports.claude_code_sdk_session import ClaudeCodeSdkTurnSession


class _FakeTextBlock:
    def __init__(self, text):
        self.text = text


class _FakeAssistantMessage:
    def __init__(self, content):
        self.content = content


class _FakeResultMessage:
    def __init__(self):
        self.usage = {"inputTokens": 10, "outputTokens": 5}
        self.model_usage = None
        self.total_cost_usd = 0.001
        self.is_error = False


def test_run_turn_sends_input_and_collects_final_text():
    fake_client = MagicMock()
    fake_client.is_alive.return_value = True
    events = [
        {"type": "raw_message", "message": _FakeAssistantMessage(
            content=[_FakeTextBlock("assembled final text")]
        )},
        {"type": "raw_message", "message": _FakeResultMessage()},
    ]
    fake_client.take_event.side_effect = lambda timeout=0.0: (
        events.pop(0) if events else None
    )

    session = ClaudeCodeSdkTurnSession(client_factory=lambda **kw: fake_client)
    result = session.run_turn(user_input="hello", turn_timeout=2.0)

    fake_client.start.assert_called_once()
    fake_client.send_turn.assert_called_once_with("hello", session_id="default")
    assert result.final_text == "assembled final text"
    assert result.error is None
    assert result.should_retire is False


def test_run_turn_marks_retire_on_transport_error():
    fake_client = MagicMock()
    fake_client.is_alive.return_value = True
    fake_client.take_event.side_effect = [
        {"type": "transport_error", "error": "boom"},
        None,
    ]

    session = ClaudeCodeSdkTurnSession(client_factory=lambda **kw: fake_client)
    result = session.run_turn(user_input="hello", turn_timeout=2.0)

    assert result.error == "boom"
    assert result.should_retire is True
```

- [ ] **Step 9: Run tests to verify they pass**

Run: `cd hermes-agent && python -m pytest tests/agent/transports/test_claude_code_sdk_session.py -v`
Expected: PASS (2 tests)

- [ ] **Step 10: Wire the session + event bridge into `run_claude_code_sdk_turn`'s default path**

Edit `hermes-agent/agent/claude_code_runtime.py`, replacing the `else` branch inside `run_claude_code_sdk_turn`'s lazy-init block:

```python
        else:
            from agent.transports.claude_code_sdk_session import (
                ClaudeCodeSdkTurnSession,
            )

            cwd = getattr(agent, "session_cwd", None)
            agent._claude_code_session = ClaudeCodeSdkTurnSession(
                cwd=cwd,
                on_event=make_claude_code_sdk_event_bridge(agent),
            )
```

- [ ] **Step 11: Run the full Task 5+6 test files together**

Run: `cd hermes-agent && python -m pytest tests/agent/test_claude_code_runtime_dispatch.py tests/agent/test_claude_code_sdk_event_bridge.py tests/agent/transports/test_claude_code_sdk_session.py -v`
Expected: PASS (9 tests total)

- [ ] **Step 12: Commit**

```bash
cd hermes-agent
git add agent/transports/claude_code_sdk_session.py agent/claude_code_runtime.py tests/agent/transports/test_claude_code_sdk_session.py tests/agent/test_claude_code_sdk_event_bridge.py
git commit -m "feat: add ClaudeCodeSdkTurnSession and event bridge for text/tool projection"
```

---

### Task 7: Usage recording — `_record_claude_code_sdk_usage`

**Files:**
- Modify: `hermes-agent/agent/claude_code_runtime.py`
- Test: `hermes-agent/tests/agent/test_claude_code_sdk_usage.py`

**Interfaces:**
- Consumes: `TurnResult.result_message` (a real `ResultMessage` with `.usage: dict` shaped `{"inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens", ...}` per the verified SDK dataclass), `agent.session_prompt_tokens` / `session_completion_tokens` / etc. (same accounting fields `_record_codex_app_server_usage` writes).
- Produces: `_record_claude_code_sdk_usage(agent, turn) -> dict[str, Any]`, called from `run_claude_code_sdk_turn` after a turn completes.

- [ ] **Step 1: Write the failing test**

```python
# hermes-agent/tests/agent/test_claude_code_sdk_usage.py
from unittest.mock import MagicMock

from agent.claude_code_runtime import _record_claude_code_sdk_usage


class _FakeResultMessage:
    def __init__(self, usage):
        self.usage = usage
        self.model_usage = None
        self.total_cost_usd = None
        self.is_error = False


def test_records_usage_with_cache_write_tokens_unlike_codex():
    agent = MagicMock()
    agent.session_prompt_tokens = 0
    agent.session_completion_tokens = 0
    agent.session_total_tokens = 0
    agent.session_input_tokens = 0
    agent.session_output_tokens = 0
    agent.session_cache_read_tokens = 0
    agent.session_cache_write_tokens = 0
    agent.session_reasoning_tokens = 0
    agent.session_api_calls = 0
    agent._session_db = None
    agent.model = "claude-sonnet-5"
    agent.provider = "anthropic"
    agent.base_url = ""

    turn = MagicMock()
    turn.result_message = _FakeResultMessage(
        usage={
            "inputTokens": 100,
            "outputTokens": 50,
            "cacheReadInputTokens": 20,
            "cacheCreationInputTokens": 30,
        }
    )

    usage_dict = _record_claude_code_sdk_usage(agent, turn)

    assert agent.session_api_calls == 1
    assert agent.session_input_tokens == 100
    assert agent.session_output_tokens == 50
    assert agent.session_cache_read_tokens == 20
    # Key behavioral difference from _record_codex_app_server_usage: Claude
    # DOES report cache-write tokens, so this must NOT be zeroed.
    assert agent.session_cache_write_tokens == 30
    assert usage_dict["cache_write_tokens"] == 30


def test_counts_api_call_even_when_result_message_is_none():
    agent = MagicMock()
    agent.session_api_calls = 0
    agent._session_db = None

    turn = MagicMock()
    turn.result_message = None

    usage_dict = _record_claude_code_sdk_usage(agent, turn)

    assert agent.session_api_calls == 1
    assert usage_dict == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd hermes-agent && python -m pytest tests/agent/test_claude_code_sdk_usage.py -v`
Expected: FAIL with `ImportError: cannot import name '_record_claude_code_sdk_usage'`

- [ ] **Step 3: Write the minimal implementation**

Add to `hermes-agent/agent/claude_code_runtime.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd hermes-agent && python -m pytest tests/agent/test_claude_code_sdk_usage.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Wire usage recording into `run_claude_code_sdk_turn`**

Edit `run_claude_code_sdk_turn` in `hermes-agent/agent/claude_code_runtime.py`: insert a call to `_record_claude_code_sdk_usage` immediately before the final `return` statement, and splice its result into the returned dict via `**usage_result`, so the tail of the function reads:

```python
    usage_result = _record_claude_code_sdk_usage(agent, turn)

    return {
        "final_response": turn.final_text,
        "messages": messages,
        "api_calls": 1,
        "completed": not turn.interrupted and turn.error is None,
        "partial": turn.interrupted or turn.error is not None,
        "interrupted": bool(turn.interrupted),
        "error": turn.error,
        "agent_persisted": False,
        **usage_result,
    }
```

- [ ] **Step 6: Run the full runtime test file to confirm no regressions**

Run: `cd hermes-agent && python -m pytest tests/agent/test_claude_code_runtime_dispatch.py tests/agent/test_claude_code_sdk_event_bridge.py tests/agent/test_claude_code_sdk_usage.py -v`
Expected: PASS (11 tests total)

- [ ] **Step 7: Commit**

```bash
cd hermes-agent
git add agent/claude_code_runtime.py tests/agent/test_claude_code_sdk_usage.py
git commit -m "feat: record claude code sdk turn usage, preserving cache-write tokens"
```

---

### Task 8: Approval bridging — `can_use_tool` hook wired to Hermes's approval flow

**Files:**
- Modify: `hermes-agent/agent/claude_code_runtime.py`
- Modify: `hermes-agent/agent/transports/claude_code_sdk_session.py` (thread `can_use_tool` through to the client)
- Test: `hermes-agent/tests/agent/test_claude_code_sdk_approval.py`

**Interfaces:**
- Consumes: `tools.terminal_tool._get_approval_callback()` (returns a callable or `None`, thread-local), `tools.approval.is_approval_bypass_active() -> bool`, `tools.approval.prompt_dangerous_approval(command, description, allow_permanent=False) -> str` (returns `'once'|'session'|'always'|'deny'`).
- Produces: `_make_claude_code_approval_callback(agent) -> Callable[[str, dict, Any], Awaitable[PermissionResultAllow | PermissionResultDeny]]` — an async function matching `ClaudeAgentOptions.can_use_tool`'s real signature `Callable[[str, dict[str, Any], ToolPermissionContext], Awaitable[PermissionResultAllow | PermissionResultDeny]]` (verified against the installed SDK).

- [ ] **Step 1: Write the failing test**

```python
# hermes-agent/tests/agent/test_claude_code_sdk_approval.py
import asyncio
from unittest.mock import MagicMock, patch

from agent.claude_code_runtime import _make_claude_code_approval_callback


def test_approval_callback_allows_on_once():
    agent = MagicMock()
    callback = _make_claude_code_approval_callback(agent)

    with patch("tools.approval.is_approval_bypass_active", return_value=False), \
         patch("tools.terminal_tool._get_approval_callback", return_value=None), \
         patch("tools.approval.prompt_dangerous_approval", return_value="once"):
        result = asyncio.run(callback("Bash", {"command": "ls"}, MagicMock()))

    assert type(result).__name__ == "PermissionResultAllow"


def test_approval_callback_denies_on_deny():
    agent = MagicMock()
    callback = _make_claude_code_approval_callback(agent)

    with patch("tools.approval.is_approval_bypass_active", return_value=False), \
         patch("tools.terminal_tool._get_approval_callback", return_value=None), \
         patch("tools.approval.prompt_dangerous_approval", return_value="deny"):
        result = asyncio.run(callback("Bash", {"command": "rm -rf /"}, MagicMock()))

    assert type(result).__name__ == "PermissionResultDeny"


def test_approval_callback_auto_allows_when_bypass_active():
    agent = MagicMock()
    callback = _make_claude_code_approval_callback(agent)

    with patch("tools.approval.is_approval_bypass_active", return_value=True):
        result = asyncio.run(callback("Bash", {"command": "ls"}, MagicMock()))

    assert type(result).__name__ == "PermissionResultAllow"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd hermes-agent && python -m pytest tests/agent/test_claude_code_sdk_approval.py -v`
Expected: FAIL with `ImportError: cannot import name '_make_claude_code_approval_callback'`

- [ ] **Step 3: Write the minimal implementation**

Add to `hermes-agent/agent/claude_code_runtime.py`:

```python
def _make_claude_code_approval_callback(agent):
    """Build a can_use_tool callback for ClaudeAgentOptions, bridging
    Claude Code's own tool-permission prompts through Hermes' existing
    approval flow instead of letting the CLI use its own independent
    permission mode. Mirrors CodexAppServerSession._decide_exec_approval /
    _decide_apply_patch_approval's use of tools.approval.
    prompt_dangerous_approval and tools.approval.is_approval_bypass_active.
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd hermes-agent && python -m pytest tests/agent/test_claude_code_sdk_approval.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Thread the callback into `run_claude_code_sdk_turn`'s session construction**

Edit `hermes-agent/agent/claude_code_runtime.py`'s lazy-init block (from Task 6, Step 10) to pass `can_use_tool`:

```python
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
```

- [ ] **Step 6: Confirm `ClaudeCodeSdkTurnSession` already threads `can_use_tool` through**

Verify (no code change expected — `ClaudeCodeSdkTurnSession.__init__` from Task 6 already accepts and forwards `can_use_tool` to its `client_factory` call in `ensure_started()`): re-run `cd hermes-agent && python -m pytest tests/agent/transports/test_claude_code_sdk_session.py -v` and confirm it still passes unmodified.

- [ ] **Step 7: Run the complete Phase 1 test suite**

Run:
```bash
cd hermes-agent
python -m pytest \
  tests/agent/transports/test_claude_code_sdk_binary.py \
  tests/agent/transports/test_claude_code_sdk_client_lifecycle.py \
  tests/agent/transports/test_claude_code_sdk_client_turns.py \
  tests/agent/transports/test_claude_code_sdk_session.py \
  tests/agent/test_agent_init_api_mode.py \
  tests/hermes_cli/test_runtime_provider_claude_code_sdk.py \
  tests/agent/test_claude_code_runtime_dispatch.py \
  tests/agent/test_claude_code_sdk_event_bridge.py \
  tests/agent/test_claude_code_sdk_usage.py \
  tests/agent/test_claude_code_sdk_approval.py \
  -v
```
Expected: PASS (32 tests total, 0 failures)

- [ ] **Step 8: Commit**

```bash
cd hermes-agent
git add agent/claude_code_runtime.py tests/agent/test_claude_code_sdk_approval.py
git commit -m "feat: bridge claude code sdk tool permissions through Hermes approval flow"
```

---

## What's deliberately NOT in this plan

- **Multi-account (`agent/cli_accounts.py`, live `switch_cli_account()` hot-swap, Codex-side `codex_home` threading)** — separate follow-up plan. Today's `ClaudeCodeSdkTurnSession`/`ClaudeCodeSdkClient` already accept a `claude_config_dir` parameter end-to-end (Task 2/6), so the next plan only needs to add the account registry and resolve `config_dir` into that parameter — no transport rework required.
- **Sub-agent visibility (`TaskStartedMessage`/`TaskUpdatedMessage`/`TaskProgressMessage`/`TaskNotificationMessage` handling, `subagent_transcripts` table, `subagent_transcript.get` RPC, Desktop `subagent-task.tsx`)** — separate follow-up plan. Task 6's event bridge explicitly ignores these message types for now (see its docstring); confirmed real dataclass field names from the installed SDK (`task_id`, `description`, `status`, `tool_use_id`, `summary`, `usage: TaskUsage`) should replace the design doc's more generic `task_id`/`subagent_type` assumption when that plan is written.
- Hardening equivalent to Codex's OAuth-failure classification (`_classify_oauth_failure`), post-tool quiet watchdog, and `<turn_aborted>` marker detection — these are real Codex refinements built up over time; add them to the Claude transport as follow-up hardening once the baseline above is live and its actual failure modes are observed, rather than speculatively porting every Codex edge case now.
