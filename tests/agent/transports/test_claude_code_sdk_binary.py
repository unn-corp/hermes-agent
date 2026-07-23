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
