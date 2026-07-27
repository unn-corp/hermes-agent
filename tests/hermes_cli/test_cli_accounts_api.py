"""Tests for the /api/cli-accounts endpoints backing the Providers panel."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    import hermes_cli.web_server as ws
    from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN

    monkeypatch.setattr(ws, "load_config", lambda: {"cli_accounts": {
        "work": {"provider": "codex", "config_dir": "/a"},
        "personal": {"provider": "claude_code_sdk", "config_dir": "/b"},
    }})
    c = TestClient(ws.app)
    # These endpoints mutate config, so they are deliberately NOT in
    # dashboard_auth.public_paths — authenticate like the other API tests.
    c.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return c


def test_list_returns_registered_accounts(client, monkeypatch):
    import hermes_cli.web_server as ws
    from agent.cli_accounts import CliAccount
    monkeypatch.setattr(ws, "load_cli_accounts", lambda: [
        CliAccount(name="work", provider="codex", config_dir="/a"),
        CliAccount(name="personal", provider="claude_code_sdk", config_dir="/b"),
    ])
    body = client.get("/api/cli-accounts").json()
    names = [a["name"] for a in body["accounts"]]
    assert names == ["work", "personal"]
    assert body["accounts"][1]["provider"] == "claude_code_sdk"


def test_list_filters_by_provider(client, monkeypatch):
    import hermes_cli.web_server as ws
    from agent.cli_accounts import CliAccount
    monkeypatch.setattr(ws, "load_cli_accounts", lambda: [
        CliAccount(name="work", provider="codex", config_dir="/a"),
        CliAccount(name="personal", provider="claude_code_sdk", config_dir="/b"),
    ])
    body = client.get("/api/cli-accounts?provider=claude_code_sdk").json()
    assert [a["name"] for a in body["accounts"]] == ["personal"]


def test_add_rejects_unknown_provider(client):
    r = client.post("/api/cli-accounts", json={
        "name": "x", "provider": "bogus", "config_dir": "/tmp",
    })
    assert r.status_code == 400
    assert "provider" in r.json()["detail"].lower()


def test_add_probes_before_persisting(client, monkeypatch):
    """A directory that was never logged into must be rejected up front —
    same probe-before-persist contract as `hermes accounts add`."""
    import hermes_cli.web_server as ws
    monkeypatch.setattr(ws, "probe_cli_account", lambda a: (False, "no .credentials.json"))
    saved = {}
    monkeypatch.setattr(ws, "save_config", lambda c, **k: saved.update(c))

    r = client.post("/api/cli-accounts", json={
        "name": "personal", "provider": "claude_code_sdk", "config_dir": "/nope",
    })
    assert r.status_code == 400
    assert "no .credentials.json" in r.json()["detail"]
    assert saved == {}  # nothing written


def test_add_persists_on_successful_probe(client, monkeypatch):
    import hermes_cli.web_server as ws
    monkeypatch.setattr(ws, "probe_cli_account", lambda a: (True, "claude 2.1.220"))
    saved = {}
    monkeypatch.setattr(ws, "save_config", lambda c, **k: saved.update(c))

    r = client.post("/api/cli-accounts", json={
        "name": "personal", "provider": "claude_code_sdk", "config_dir": "/b",
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert saved["cli_accounts"]["personal"] == {
        "provider": "claude_code_sdk", "config_dir": "/b",
    }


def test_delete_removes_the_entry(client, monkeypatch):
    import hermes_cli.web_server as ws
    saved = {}
    monkeypatch.setattr(ws, "save_config", lambda c, **k: saved.update(c))

    r = client.delete("/api/cli-accounts/work")
    assert r.status_code == 200
    assert "work" not in saved["cli_accounts"]
    assert "personal" in saved["cli_accounts"]  # siblings untouched


def test_delete_unknown_name_is_404(client, monkeypatch):
    import hermes_cli.web_server as ws
    monkeypatch.setattr(ws, "save_config", lambda c, **k: None)
    assert client.delete("/api/cli-accounts/missing").status_code == 404
