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
