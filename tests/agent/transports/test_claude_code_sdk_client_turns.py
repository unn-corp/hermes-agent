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
