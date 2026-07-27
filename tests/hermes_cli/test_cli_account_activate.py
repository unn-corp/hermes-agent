"""POST /api/cli-accounts/activate — the Providers-panel account selector.

Settings is not session-scoped, so "activate" means: make this account the
persisted default for the next session, and (optionally) move the main model
onto Claude so the CLI runtime actually engages. Live mid-conversation
switching stays on /cli-account.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    import hermes_cli.web_server as ws
    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN

    state = {"config": {
        "model": {"default": "grok-build-0.1", "provider": "xai-oauth"},
        "cli_accounts": {
            "work": {"provider": "claude_code_sdk", "config_dir": "/home/u/.claude-work"},
            "boxed": {"provider": "codex", "config_dir": "/home/u/.codex-work"},
        },
    }}
    monkeypatch.setattr(ws, "load_config", lambda: state["config"])
    monkeypatch.setattr(ws, "save_config", lambda c, **k: state.update(config=c))
    c = TestClient(ws.app)
    c.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    c.state = state
    return c


def test_activate_sets_the_default_config_dir(client):
    r = client.post("/api/cli-accounts/activate", json={"name": "work"})
    assert r.status_code == 200
    assert client.state["config"]["claude_code"]["config_dir"] == "/home/u/.claude-work"


def test_activate_enables_the_runtime(client):
    """Pointing at an account is pointless if the runtime stays off."""
    client.post("/api/cli-accounts/activate", json={"name": "work"})
    assert client.state["config"]["model"]["anthropic_runtime"] == "claude_code_sdk"


def test_activate_switches_the_model_onto_claude(client):
    r = client.post("/api/cli-accounts/activate", json={"name": "work"})
    model = client.state["config"]["model"]
    assert model["provider"] == "anthropic"
    assert "claude" in model["default"].lower()
    assert "claude" in r.json()["model"].lower()


def test_activate_can_leave_the_model_alone(client):
    client.post("/api/cli-accounts/activate", json={"name": "work", "switch_model": False})
    model = client.state["config"]["model"]
    assert model["default"] == "grok-build-0.1"  # untouched
    # …but the account default still applied.
    assert client.state["config"]["claude_code"]["config_dir"] == "/home/u/.claude-work"


def test_activate_unknown_account_is_404(client):
    assert client.post("/api/cli-accounts/activate", json={"name": "nope"}).status_code == 404


def test_activate_rejects_a_codex_account(client):
    """claude_code.config_dir is Claude-specific; a codex account there would
    silently point the Claude CLI at a CODEX_HOME."""
    r = client.post("/api/cli-accounts/activate", json={"name": "boxed"})
    assert r.status_code == 400
    assert "claude_code_sdk" in r.json()["detail"]


def test_list_marks_which_account_is_active(client):
    client.post("/api/cli-accounts/activate", json={"name": "work"})
    import hermes_cli.web_server as ws
    from agent.cli_accounts import CliAccount
    # load_cli_accounts reads the readonly loader, so drive it off the same state.
    ws_accounts = [
        CliAccount(name="work", provider="claude_code_sdk", config_dir="/home/u/.claude-work"),
        CliAccount(name="boxed", provider="codex", config_dir="/home/u/.codex-work"),
    ]
    object.__setattr__(ws, "load_cli_accounts", lambda: ws_accounts)

    rows = client.get("/api/cli-accounts").json()["accounts"]
    by_name = {r["name"]: r for r in rows}
    assert by_name["work"]["active"] is True
    assert by_name["boxed"]["active"] is False
