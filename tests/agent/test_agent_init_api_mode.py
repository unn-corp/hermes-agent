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
