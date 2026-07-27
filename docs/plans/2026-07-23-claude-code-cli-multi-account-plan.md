# CLI Multi-Account + Live Hot-Swap (Phase 2 of 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Hermes a shared named-account registry (`agent/cli_accounts.py`) for both the Codex and Claude Code CLI transports, `hermes accounts add/list/remove` CLI management, and a live `AIAgent.switch_cli_account()` hot-swap (plus its `/cli-account` slash-command trigger) so a running conversation can move to a different isolated `~/.codex` / `~/.claude` home directory without losing history.

**Architecture:** `agent/cli_accounts.py` is a small, transport-agnostic dataclass + loader/resolver/prober module that both `agent/codex_runtime.py` (existing) and `agent/claude_code_runtime.py` (Phase 1) read `config_dir` from at session-construction time. `AIAgent.switch_cli_account()` tears down whichever session is live for the requested provider (interrupt + close + clear the cached attribute) and records the newly active account in `agent._active_cli_accounts`; the existing lazy-init blocks in both runtime modules pick up the new `config_dir` on the very next turn. `hermes accounts add/list/remove` (new `hermes_cli/account_commands.py` + `hermes_cli/subcommands/accounts.py`) manages the registry the same way `hermes auth add/list/remove` manages pooled provider credentials — probe-before-persist, same parser-builder convention. A new `/cli-account <provider> <name>` slash command (mirroring `/codex-runtime`'s wiring, but calling `switch_cli_account()` directly instead of only persisting a config value for next session) is the CLI-side trigger; a Desktop UI account-picker action is an explicitly out-of-scope stub for Phase 3 to wire up against the same `switch_cli_account()` method.

**Tech Stack:** Python 3, `pytest` (already a dev dependency), no new third-party dependencies.

## Global Constraints

- **Provider literal values are exactly `"codex"` and `"claude_code_sdk"`.** Never use the bare string `"claude_code"` for a `CliAccount.provider` value, a `cli_accounts.<name>.provider` config value, or a CLI/slash-command flag. `agent/credential_sources.py` lines 399-403 already register a RemovalStep with `provider="anthropic", source_id="claude_code"` (meaning "`~/.claude/.credentials.json` read as a bearer token for the *already-shipped* `anthropic_messages` transport") — a completely different, older feature from the new named-account registry this plan adds. Reusing the bare string would make `hermes auth list` / `hermes accounts list` output ambiguous between the two. **Correction to the design doc:** `docs/design/claude-code-integration.md` component 4's own `CliAccount` code sample uses `Literal["codex", "claude_code"]` despite its own naming-collision warning one paragraph above stating the opposite — that sample is wrong; this plan uses `Literal["codex", "claude_code_sdk"]` throughout, matching the string Phase 1 already established for `agent.api_mode` / `model.anthropic_runtime`.
- **`cli_accounts` config key is dict-shaped, keyed by user-chosen account name** (`cli_accounts: {<name>: {provider, config_dir}}`), registered in `hermes_cli/config.py`'s `_OPEN_DICT_TOP_LEVEL_KEYS` frozenset (verified real code at `hermes_cli/config.py:8679-8692`) — the same bucket as `mcp_servers` and `providers`, which also have **no** corresponding `DEFAULT_CONFIG` entry (verified: `mcp_servers` does not appear in `DEFAULT_CONFIG`'s literal key list). Do not add `"cli_accounts"` to `_DYNAMIC_TOP_LEVEL_KEYS` (that bucket is for `custom_providers` only, a list-shaped, position-indexed structure) and do not add a `DEFAULT_CONFIG["cli_accounts"]` entry.
- **Subcommand parser convention (verified real code, not the design doc's paraphrase):** every existing multi-action file under `hermes_cli/subcommands/` (`auth.py`, `mcp.py`, `hooks.py`, `profile.py`, `skills.py`) attaches a **single** injected `cmd_<name>` handler via `<name>_parser.set_defaults(func=cmd_<name>)` on the outer parser, with sub-actions (add/list/remove/...) dispatched **internally** by a `<name>_command(args)` function keyed off `args.<name>_action` (see `hermes_cli/auth_commands.py:auth_command`, called by `hermes_cli/main.py:cmd_auth`). This plan's `hermes_cli/subcommands/accounts.py` follows that exact real convention — **not** the `build_accounts_parser(subparsers, *, cmd_accounts_add, cmd_accounts_list, cmd_accounts_remove)` three-separate-callable shape sketched in `docs/design/claude-code-integration.md` component 5, which does not match any existing file in the codebase.
- **Session-close pattern:** resolve `getattr(session, "request_interrupt", None)` and call it if present, then call `session.close()` — both wrapped in their own `try/except Exception: logger.debug(..., exc_info=True)` so a broken transport can never leave `switch_cli_account()` partially applied. This mirrors the existing cleanup shape at `agent/codex_runtime.py:750-759` and `run_agent.py`'s existing `interrupt()` method's `codex_app_server` branch (~line 2872-2882).
- **Never override `HOME` for account isolation** (carried over from Phase 1) — `CliAccount.config_dir` is threaded through to the existing `CODEX_HOME` (`agent/transports/codex_app_server.py`) / `CLAUDE_CONFIG_DIR` (Phase 1's `agent/transports/claude_code_sdk.py`) env-var mapping, which already exists at the transport layer. This plan only adds the *resolution* (which account is active) and the *threading* (passing `config_dir` into the constructor call sites that don't yet receive it) — it does not touch the env-var mapping itself.
- **Phase 1 dependency:** Task 3's `claude_code_sdk` probe branch, Task 6, and Task 9 modify or import from `agent/transports/claude_code_sdk.py`, `agent/transports/claude_code_sdk_session.py`, and `agent/claude_code_runtime.py` — none of which exist until `docs/plans/2026-07-23-claude-code-sdk-transport-plan.md` (Phase 1) has landed on this branch. If Phase 1 has not landed yet, land it first, then resume at whichever task needs it. Tasks 1, 2, 4, 5, 7, 8, and 10 have no such dependency and can be implemented and merged independently of Phase 1's status.
- **Desktop UI wiring is explicitly out of scope for this plan** — a future Phase 3 UI action calls the same `AIAgent.switch_cli_account()` method this plan implements and tests for real; only the CLI slash-command trigger is built here.
- **Out of scope (Phase 3, not touched here):** sub-agent transcript persistence, the `claude_subagent_task` tool_call name, and `TaskStartedMessage`/`TaskUpdatedMessage`/`TaskProgressMessage`/`TaskNotificationMessage` handling.
- No AI attribution in any commit message (hard rule — do not add `Co-Authored-By` or similar trailers).
- Full spec: `hermes-agent/docs/design/claude-code-integration.md` (components 2, 3, 4, 5). Phase 1's real, confirmed class signatures: `hermes-agent/docs/plans/2026-07-23-claude-code-sdk-transport-plan.md`.

---

### Task 1: `agent/cli_accounts.py` — `CliAccount` dataclass + `load_cli_accounts()` + `resolve_cli_account()`

**Files:**
- Create: `hermes-agent/agent/cli_accounts.py`
- Test: `hermes-agent/tests/agent/test_cli_accounts.py`

**Interfaces:**
- Consumes: `hermes_cli.config.load_config_readonly() -> Dict[str, Any]` (existing, real, read-only cached config loader).
- Produces: `@dataclass(frozen=True) class CliAccount` with fields `name: str`, `provider: Literal["codex", "claude_code_sdk"]`, `config_dir: str`; `load_cli_accounts() -> list[CliAccount]`; `resolve_cli_account(name: str, provider: str) -> Optional[CliAccount]`.

- [ ] **Step 1: Write the failing tests**

```python
# hermes-agent/tests/agent/test_cli_accounts.py
"""Tests for agent/cli_accounts.py — the shared named-account registry used
by both the codex_app_server and claude_code_sdk runtimes."""
from __future__ import annotations

from agent.cli_accounts import CliAccount, load_cli_accounts, resolve_cli_account


def test_load_cli_accounts_returns_typed_entries(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {
            "cli_accounts": {
                "work": {"provider": "codex", "config_dir": "/home/u/.codex-work"},
                "personal": {"provider": "claude_code_sdk", "config_dir": "/home/u/.claude-personal"},
            }
        },
    )
    accounts = load_cli_accounts()
    assert accounts == [
        CliAccount(name="work", provider="codex", config_dir="/home/u/.codex-work"),
        CliAccount(name="personal", provider="claude_code_sdk", config_dir="/home/u/.claude-personal"),
    ]


def test_load_cli_accounts_returns_empty_list_when_unset(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {})
    assert load_cli_accounts() == []


def test_load_cli_accounts_skips_malformed_entries(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {
            "cli_accounts": {
                "bad-provider": {"provider": "anthropic", "config_dir": "/x"},
                "missing-dir": {"provider": "codex"},
                "not-a-dict": "oops",
            }
        },
    )
    assert load_cli_accounts() == []


def test_resolve_cli_account_matches_name_and_provider(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"cli_accounts": {"work": {"provider": "codex", "config_dir": "/x"}}},
    )
    assert resolve_cli_account("work", "codex") == CliAccount(
        name="work", provider="codex", config_dir="/x"
    )
    assert resolve_cli_account("work", "claude_code_sdk") is None
    assert resolve_cli_account("nope", "codex") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/agent/test_cli_accounts.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agent.cli_accounts'`

- [ ] **Step 3: Write the minimal implementation**

```python
# hermes-agent/agent/cli_accounts.py
"""Shared named CLI-account registry for both the Codex and Claude Code CLI
transports (agent/transports/codex_app_server.py, agent/transports/claude_code_sdk.py).

Manages isolated home directories for external CLI subprocesses that own
their own auth files independently (~/.codex, ~/.claude equivalents) — NOT
overlapping with agent/credential_pool.py, which manages API credentials
Hermes itself holds and rotates automatically for failover. Different
trust/data model, correctly kept separate (see
docs/design/claude-code-integration.md component 4).

Config shape (hermes_cli/config.py — cli_accounts is an open-dict top-level
key, see _OPEN_DICT_TOP_LEVEL_KEYS):

    cli_accounts:
      work:
        provider: codex
        config_dir: /home/user/.codex-work
      personal:
        provider: claude_code_sdk
        config_dir: /home/user/.claude-personal

Naming note: "claude_code_sdk" (NOT the bare "claude_code") is the provider
literal used here, to avoid colliding with the pre-existing
agent/credential_sources.py source string "claude_code" (~/.claude/.credentials.json
read as a bearer token for the separate, already-shipped anthropic_messages
transport) — see docs/design/claude-code-integration.md component 4's
naming-collision warning.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional


@dataclass(frozen=True)
class CliAccount:
    """One named external-CLI account. ``config_dir`` resolves to CODEX_HOME
    (provider == "codex") or CLAUDE_CONFIG_DIR (provider == "claude_code_sdk")
    at session-spawn time — the transport layer already maps these env vars;
    this dataclass only carries the resolved path."""

    name: str
    provider: Literal["codex", "claude_code_sdk"]
    config_dir: str


def load_cli_accounts() -> list["CliAccount"]:
    """Load every registered cli_accounts entry from config.yaml.

    Malformed entries (unknown/missing provider, empty config_dir, non-dict
    entry) are skipped rather than raising, so one bad hand-edited entry
    doesn't break every account lookup."""
    from hermes_cli.config import load_config_readonly

    config = load_config_readonly()
    raw = config.get("cli_accounts")
    if not isinstance(raw, dict):
        return []

    accounts: list[CliAccount] = []
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        provider = str(entry.get("provider") or "").strip()
        config_dir = str(entry.get("config_dir") or "").strip()
        if provider not in ("codex", "claude_code_sdk") or not config_dir:
            continue
        accounts.append(CliAccount(name=str(name), provider=provider, config_dir=config_dir))
    return accounts


def resolve_cli_account(name: str, provider: str) -> Optional["CliAccount"]:
    """Find a registered account by exact name AND provider. Returns None on
    no match (unknown name, or the name exists but for the other provider)."""
    for account in load_cli_accounts():
        if account.name == name and account.provider == provider:
            return account
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/agent/test_cli_accounts.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
cd hermes-agent
git add agent/cli_accounts.py tests/agent/test_cli_accounts.py
git commit -m "feat: add shared cli_accounts registry (CliAccount, load/resolve)"
```

---

### Task 2: `cli_accounts` config schema slot

**Files:**
- Modify: `hermes-agent/hermes_cli/config.py:8679-8692` (`_OPEN_DICT_TOP_LEVEL_KEYS`)
- Test: `hermes-agent/tests/hermes_cli/test_set_config_value.py` (extend the existing `TestValidateConfigKey.test_known_keys_pass` parametrize list)

**Interfaces:**
- Consumes: nothing new.
- Produces: `hermes_cli.config._validate_config_key("cli_accounts.work.provider") -> (True, None)`.

- [ ] **Step 1: Write the failing test**

Edit `hermes-agent/tests/hermes_cli/test_set_config_value.py`, changing:

```python
    @pytest.mark.parametrize("key", [
        "model",
        "terminal.backend",
        "agent.max_turns",
        "discord.gateway_restart_notification",
        "telegram.bot_token",
        "mcp_servers.foo.command",
        "providers.openrouter.api_key",
        "gateway.strict",
        "platforms.discord.enabled",
        "gateway.platforms.my_platform.extra.token",
        "approvals.mode",
    ])
    def test_known_keys_pass(self, key):
```

to:

```python
    @pytest.mark.parametrize("key", [
        "model",
        "terminal.backend",
        "agent.max_turns",
        "discord.gateway_restart_notification",
        "telegram.bot_token",
        "mcp_servers.foo.command",
        "providers.openrouter.api_key",
        "gateway.strict",
        "platforms.discord.enabled",
        "gateway.platforms.my_platform.extra.token",
        "approvals.mode",
        "cli_accounts.work.provider",
    ])
    def test_known_keys_pass(self, key):
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd hermes-agent && ./scripts/run_tests.sh "tests/hermes_cli/test_set_config_value.py::TestValidateConfigKey::test_known_keys_pass[cli_accounts.work.provider]" -v`
Expected: FAIL — `assert is_known` fails because `cli_accounts` is not yet in `_known_top_level_keys()`.

- [ ] **Step 3: Write the minimal implementation**

Edit `hermes-agent/hermes_cli/config.py`, changing:

```python
_OPEN_DICT_TOP_LEVEL_KEYS = frozenset({
    "providers",
    "credential_pool_strategies",
    "mcp_servers",
    "hooks",
    "quick_commands",
    "personalities",
    "command_allowlist",
    "model_catalog",
    "channel_prompts",
    "server_actions",
    "secrets",
    "goals",
})
```

to:

```python
_OPEN_DICT_TOP_LEVEL_KEYS = frozenset({
    "providers",
    "credential_pool_strategies",
    "mcp_servers",
    "hooks",
    "quick_commands",
    "personalities",
    "command_allowlist",
    "model_catalog",
    "channel_prompts",
    "server_actions",
    "secrets",
    "goals",
    # Named CLI-account registry (agent/cli_accounts.py) — external CLI
    # subprocess home directories (CODEX_HOME / CLAUDE_CONFIG_DIR), keyed
    # by user-chosen account name. Same open-dict shape as mcp_servers:
    # no DEFAULT_CONFIG entry, users define the inner keys themselves.
    "cli_accounts",
})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd hermes-agent && ./scripts/run_tests.sh "tests/hermes_cli/test_set_config_value.py::TestValidateConfigKey::test_known_keys_pass[cli_accounts.work.provider]" -v`
Expected: PASS

- [ ] **Step 5: Run the whole file to confirm no regressions**

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/hermes_cli/test_set_config_value.py -v`
Expected: PASS (all tests, no regressions)

- [ ] **Step 6: Commit**

```bash
cd hermes-agent
git add hermes_cli/config.py tests/hermes_cli/test_set_config_value.py
git commit -m "feat: register cli_accounts as an open-dict config schema key"
```

---

### Task 3: `probe_cli_account()` — codex + claude_code_sdk liveness checks

> Depends on Phase 1 having landed for the `claude_code_sdk` branch (imports `agent.transports.claude_code_sdk.check_claude_binary`). The `codex` branch has no such dependency.

**Files:**
- Modify: `hermes-agent/agent/cli_accounts.py`
- Test: `hermes-agent/tests/agent/test_cli_accounts.py`

**Interfaces:**
- Consumes: `CliAccount` (Task 1); `agent.transports.codex_app_server.check_codex_binary() -> tuple[bool, str]` (existing, real); `agent.transports.claude_code_sdk.check_claude_binary() -> tuple[bool, str]` (Phase 1, real signature confirmed in `docs/plans/2026-07-23-claude-code-sdk-transport-plan.md` Task 1).
- Produces: `probe_cli_account(account: CliAccount) -> tuple[bool, str]`.

- [ ] **Step 1: Write the failing tests**

```python
# Append to hermes-agent/tests/agent/test_cli_accounts.py
import json

from agent.cli_accounts import probe_cli_account


def test_probe_codex_account_fails_when_binary_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "agent.transports.codex_app_server.check_codex_binary",
        lambda: (False, "codex CLI not found"),
    )
    account = CliAccount(name="work", provider="codex", config_dir=str(tmp_path))
    ok, msg = probe_cli_account(account)
    assert ok is False
    assert msg == "codex CLI not found"


def test_probe_codex_account_fails_when_auth_json_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "agent.transports.codex_app_server.check_codex_binary",
        lambda: (True, "0.130.0"),
    )
    account = CliAccount(name="work", provider="codex", config_dir=str(tmp_path))
    ok, msg = probe_cli_account(account)
    assert ok is False
    assert "no auth.json" in msg


def test_probe_codex_account_succeeds_with_valid_auth_json(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "agent.transports.codex_app_server.check_codex_binary",
        lambda: (True, "0.130.0"),
    )
    (tmp_path / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "tok", "refresh_token": "ref"}})
    )
    account = CliAccount(name="work", provider="codex", config_dir=str(tmp_path))
    ok, msg = probe_cli_account(account)
    assert ok is True
    assert "0.130.0" in msg


def test_probe_claude_code_sdk_account_succeeds_with_valid_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "agent.transports.claude_code_sdk.check_claude_binary",
        lambda: (True, "2.1.217"),
    )
    (tmp_path / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
    )
    account = CliAccount(name="personal", provider="claude_code_sdk", config_dir=str(tmp_path))
    ok, msg = probe_cli_account(account)
    assert ok is True
    assert "2.1.217" in msg


def test_probe_claude_code_sdk_account_fails_when_credentials_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "agent.transports.claude_code_sdk.check_claude_binary",
        lambda: (True, "2.1.217"),
    )
    account = CliAccount(name="personal", provider="claude_code_sdk", config_dir=str(tmp_path))
    ok, msg = probe_cli_account(account)
    assert ok is False
    assert "no .credentials.json" in msg
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/agent/test_cli_accounts.py -v -k probe`
Expected: FAIL with `ImportError: cannot import name 'probe_cli_account'`

- [ ] **Step 3: Write the minimal implementation**

Add to `hermes-agent/agent/cli_accounts.py` (after `resolve_cli_account`):

```python
def probe_cli_account(account: "CliAccount") -> tuple[bool, str]:
    """Best-effort liveness check for a registered account: binary present
    at an acceptable version, AND config_dir looks like it holds real
    credentials for that provider. Returns (ok, message) — never raises,
    mirrors check_codex_binary()/check_claude_binary()'s own (ok, message)
    contract so callers can print the message directly."""
    import json
    import os

    if account.provider == "codex":
        from agent.transports.codex_app_server import check_codex_binary

        binary_ok, binary_msg = check_codex_binary()
        if not binary_ok:
            return False, binary_msg
        auth_path = os.path.join(account.config_dir, "auth.json")
        if not os.path.isfile(auth_path):
            return False, f"no auth.json found under {account.config_dir}"
        try:
            with open(auth_path, encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            return False, f"could not read {auth_path}: {exc}"
        tokens = payload.get("tokens") if isinstance(payload, dict) else None
        if not isinstance(tokens, dict) or not tokens.get("access_token"):
            return False, f"{auth_path} has no access_token"
        return True, f"codex {binary_msg} — {account.config_dir}"

    if account.provider == "claude_code_sdk":
        from agent.transports.claude_code_sdk import check_claude_binary

        binary_ok, binary_msg = check_claude_binary()
        if not binary_ok:
            return False, binary_msg
        cred_path = os.path.join(account.config_dir, ".credentials.json")
        if not os.path.isfile(cred_path):
            return False, f"no .credentials.json found under {account.config_dir}"
        try:
            with open(cred_path, encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            return False, f"could not read {cred_path}: {exc}"
        oauth_data = payload.get("claudeAiOauth") if isinstance(payload, dict) else None
        if not isinstance(oauth_data, dict) or not oauth_data.get("accessToken"):
            return False, f"{cred_path} has no accessToken"
        return True, f"claude {binary_msg} — {account.config_dir}"

    return False, f"unknown provider: {account.provider!r}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/agent/test_cli_accounts.py -v`
Expected: PASS (9 tests total)

- [ ] **Step 5: Commit**

```bash
cd hermes-agent
git add agent/cli_accounts.py tests/agent/test_cli_accounts.py
git commit -m "feat: add probe_cli_account liveness check for codex/claude_code_sdk accounts"
```

---

### Task 4: `hermes_cli/account_commands.py` — add/list/remove handlers

**Files:**
- Create: `hermes-agent/hermes_cli/account_commands.py`
- Test: `hermes-agent/tests/hermes_cli/test_account_commands.py`

**Interfaces:**
- Consumes: `CliAccount`, `load_cli_accounts`, `probe_cli_account` (Task 1/3); `hermes_cli.config.load_config() -> Dict[str, Any]`, `hermes_cli.config.save_config(config: Dict[str, Any]) -> None` (existing, real).
- Produces: `accounts_add_command(args) -> None`, `accounts_list_command(args) -> None`, `accounts_remove_command(args) -> None`, `accounts_command(args) -> None`, `_pick_cli_account_provider(prompt: str = "Provider") -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# hermes-agent/tests/hermes_cli/test_account_commands.py
"""Tests for hermes_cli/account_commands.py — the `hermes accounts` handlers.

Mirrors tests/hermes_cli/test_auth_commands.py's shape: pure-Python handler
logic tested directly, with config load/save and the probe step mocked out."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli import account_commands


def test_accounts_add_command_probes_before_persisting(monkeypatch):
    persisted = {}
    monkeypatch.setattr(account_commands, "probe_cli_account", lambda account: (True, "codex 0.130.0"))
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
    monkeypatch.setattr("hermes_cli.config.save_config", lambda config, **kw: persisted.update(config))

    account_commands.accounts_add_command(SimpleNamespace(
        name="work", provider="codex", config_dir="/home/u/.codex-work",
    ))

    assert persisted["cli_accounts"]["work"] == {
        "provider": "codex", "config_dir": "/home/u/.codex-work",
    }


def test_accounts_add_command_rejects_failed_probe(monkeypatch):
    monkeypatch.setattr(account_commands, "probe_cli_account", lambda account: (False, "codex CLI not found"))

    with pytest.raises(SystemExit, match="codex CLI not found"):
        account_commands.accounts_add_command(SimpleNamespace(
            name="work", provider="codex", config_dir="/home/u/.codex-work",
        ))


def test_accounts_add_command_rejects_unknown_provider():
    with pytest.raises(SystemExit, match="Unknown provider"):
        account_commands.accounts_add_command(SimpleNamespace(
            name="work", provider="bogus", config_dir="/tmp",
        ))


def test_accounts_add_command_requires_config_dir(monkeypatch):
    monkeypatch.setattr(account_commands, "probe_cli_account", lambda account: (True, "ok"))
    with pytest.raises(SystemExit, match="config-dir is required"):
        account_commands.accounts_add_command(SimpleNamespace(
            name="work", provider="codex", config_dir="",
        ))


def test_accounts_list_command_filters_by_provider(monkeypatch, capsys):
    from agent.cli_accounts import CliAccount

    monkeypatch.setattr(account_commands, "load_cli_accounts", lambda: [
        CliAccount(name="work", provider="codex", config_dir="/a"),
        CliAccount(name="personal", provider="claude_code_sdk", config_dir="/b"),
    ])
    account_commands.accounts_list_command(SimpleNamespace(provider="claude_code_sdk"))
    out = capsys.readouterr().out
    assert "personal" in out
    assert "work" not in out


def test_accounts_list_command_reports_empty(monkeypatch, capsys):
    monkeypatch.setattr(account_commands, "load_cli_accounts", lambda: [])
    account_commands.accounts_list_command(SimpleNamespace(provider=""))
    out = capsys.readouterr().out
    assert "No cli accounts registered" in out


def test_accounts_remove_command_removes_entry(monkeypatch):
    persisted = {}
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"cli_accounts": {"work": {"provider": "codex", "config_dir": "/a"}}},
    )
    monkeypatch.setattr("hermes_cli.config.save_config", lambda config, **kw: persisted.update(config))

    account_commands.accounts_remove_command(SimpleNamespace(name="work"))
    assert persisted["cli_accounts"] == {}


def test_accounts_remove_command_rejects_unknown_name(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"cli_accounts": {}})
    with pytest.raises(SystemExit, match='No cli account named "missing"'):
        account_commands.accounts_remove_command(SimpleNamespace(name="missing"))


def test_accounts_command_dispatches_to_add(monkeypatch):
    called = {}
    monkeypatch.setattr(account_commands, "accounts_add_command", lambda args: called.setdefault("action", "add"))
    account_commands.accounts_command(SimpleNamespace(accounts_action="add"))
    assert called["action"] == "add"


def test_accounts_command_prints_usage_when_no_action(capsys):
    account_commands.accounts_command(SimpleNamespace(accounts_action=None))
    err = capsys.readouterr().err
    assert "usage: hermes accounts" in err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/hermes_cli/test_account_commands.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'hermes_cli.account_commands'`

- [ ] **Step 3: Write the minimal implementation**

```python
# hermes-agent/hermes_cli/account_commands.py
"""``hermes accounts`` command handlers.

Mirrors hermes_cli/auth_commands.py's shape (auth_add_command /
auth_list_command / auth_remove_command / _pick_provider / auth_command
dispatcher) for the separate agent/cli_accounts.py registry (external CLI
subprocess home directories) — NOT the credential_pool.py-backed provider
credentials auth_commands.py manages. Also mirrors auth_add_command's
"probe before persisting" pattern (its Anthropic branch calls
run_hermes_oauth_login_pure() and raises SystemExit on failure before
pool.add_entry() — here probe_cli_account() plays that role).
"""

from __future__ import annotations

import sys

from agent.cli_accounts import load_cli_accounts, probe_cli_account

_VALID_PROVIDERS = ("codex", "claude_code_sdk")


def _pick_cli_account_provider(prompt: str = "Provider") -> str:
    """Prompt for codex vs claude_code_sdk. Mirrors auth_commands._pick_provider's
    interactive-prompt shape, but over the small fixed cli-account provider
    set instead of PROVIDER_REGISTRY."""
    print(f"\nKnown cli account providers: {', '.join(_VALID_PROVIDERS)}")
    try:
        raw = input(f"{prompt}: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        raise SystemExit()
    return raw


def accounts_add_command(args) -> None:
    """Register a new named CLI account. Probes before persisting."""
    from agent.cli_accounts import CliAccount
    from hermes_cli.config import load_config, save_config

    name = str(getattr(args, "name", "") or "").strip()
    if not name:
        raise SystemExit("Account name is required.")
    provider = str(getattr(args, "provider", "") or "").strip().lower()
    if not provider:
        provider = _pick_cli_account_provider("Provider for this account")
    if provider not in _VALID_PROVIDERS:
        raise SystemExit(
            f"Unknown provider {provider!r}. Use one of: {', '.join(_VALID_PROVIDERS)}"
        )
    config_dir = str(getattr(args, "config_dir", "") or "").strip()
    if not config_dir:
        raise SystemExit("--config-dir is required.")

    account = CliAccount(name=name, provider=provider, config_dir=config_dir)
    ok, message = probe_cli_account(account)
    if not ok:
        raise SystemExit(f"Cannot add account {name!r}: {message}")

    config = load_config()
    accounts = config.setdefault("cli_accounts", {})
    accounts[name] = {"provider": provider, "config_dir": config_dir}
    save_config(config)
    print(f'Added cli account "{name}" ({provider}): {config_dir}')
    print(f"  probe: {message}")


def accounts_list_command(args) -> None:
    """List registered cli accounts, optionally filtered by provider."""
    provider_filter = str(getattr(args, "provider", "") or "").strip().lower()
    accounts = load_cli_accounts()
    if provider_filter:
        accounts = [a for a in accounts if a.provider == provider_filter]
    if not accounts:
        print("No cli accounts registered.")
        return
    for account in accounts:
        print(f"  {account.name:<20} {account.provider:<16} {account.config_dir}")


def accounts_remove_command(args) -> None:
    """Remove a named cli account from config.yaml."""
    from hermes_cli.config import load_config, save_config

    name = str(getattr(args, "name", "") or "").strip()
    if not name:
        raise SystemExit("Account name is required.")

    config = load_config()
    accounts = config.get("cli_accounts")
    if not isinstance(accounts, dict) or name not in accounts:
        raise SystemExit(f'No cli account named "{name}".')
    removed = accounts.pop(name)
    save_config(config)
    print(f'Removed cli account "{name}" ({removed.get("provider", "?")})')


def accounts_command(args) -> None:
    """Top-level ``hermes accounts`` dispatcher. Mirrors auth_command's
    action-dispatch shape in hermes_cli/auth_commands.py."""
    action = getattr(args, "accounts_action", "")
    if action == "add":
        accounts_add_command(args)
        return
    if action == "list":
        accounts_list_command(args)
        return
    if action == "remove":
        accounts_remove_command(args)
        return
    print(
        "usage: hermes accounts <add|list|remove>\n"
        "\n"
        "subcommands:\n"
        "  add     Register a new named CLI account (probes before saving)\n"
        "  list    List registered CLI accounts\n"
        "  remove  Remove a named CLI account\n"
        "\n"
        "Run `hermes accounts add -h` for details.",
        file=sys.stderr,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/hermes_cli/test_account_commands.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
cd hermes-agent
git add hermes_cli/account_commands.py tests/hermes_cli/test_account_commands.py
git commit -m "feat: add hermes accounts add/list/remove command handlers"
```

---

### Task 5: `hermes_cli/subcommands/accounts.py` parser + `hermes_cli/main.py` wiring

**Files:**
- Create: `hermes-agent/hermes_cli/subcommands/accounts.py`
- Test: `hermes-agent/tests/hermes_cli/test_subcommands_accounts.py`
- Modify: `hermes-agent/hermes_cli/main.py:433` (import), `hermes-agent/hermes_cli/main.py:4439` (add `cmd_accounts`), `hermes-agent/hermes_cli/main.py:14595` (wire parser)

**Interfaces:**
- Consumes: `accounts_command` (Task 4).
- Produces: `build_accounts_parser(subparsers, *, cmd_accounts: Callable) -> None`; `hermes_cli.main.cmd_accounts(args) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# hermes-agent/tests/hermes_cli/test_subcommands_accounts.py
"""Smoke tests for hermes_cli/subcommands/accounts.py's parser builder.
Mirrors tests/hermes_cli/test_subcommands_batch.py's single-handler-builder
pattern (auth.py, mcp.py, hooks.py, profile.py, skills.py all use this exact
single-injected-callable + internal-dispatch shape)."""
from __future__ import annotations

import argparse

import pytest

from hermes_cli.subcommands.accounts import build_accounts_parser


def _handler(args):  # pragma: no cover - identity only
    return args


def test_accounts_add_parses_required_fields():
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    build_accounts_parser(sub, cmd_accounts=_handler)

    ns = parser.parse_args(
        ["accounts", "add", "work", "--provider", "codex", "--config-dir", "/home/u/.codex-work"]
    )
    assert ns.func is _handler
    assert ns.accounts_action == "add"
    assert ns.name == "work"
    assert ns.provider == "codex"
    assert ns.config_dir == "/home/u/.codex-work"


def test_accounts_list_parses_optional_provider():
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    build_accounts_parser(sub, cmd_accounts=_handler)

    ns = parser.parse_args(["accounts", "list"])
    assert ns.accounts_action == "list"
    assert ns.provider == ""

    ns2 = parser.parse_args(["accounts", "list", "--provider", "claude_code_sdk"])
    assert ns2.provider == "claude_code_sdk"


def test_accounts_remove_parses_name():
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    build_accounts_parser(sub, cmd_accounts=_handler)

    ns = parser.parse_args(["accounts", "remove", "work"])
    assert ns.accounts_action == "remove"
    assert ns.name == "work"


def test_accounts_add_rejects_unknown_provider():
    parser = argparse.ArgumentParser(prog="hermes")
    sub = parser.add_subparsers(dest="command")
    build_accounts_parser(sub, cmd_accounts=_handler)

    with pytest.raises(SystemExit):
        parser.parse_args(
            ["accounts", "add", "work", "--provider", "bogus", "--config-dir", "/x"]
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/hermes_cli/test_subcommands_accounts.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'hermes_cli.subcommands.accounts'`

- [ ] **Step 3: Write the minimal implementation**

```python
# hermes-agent/hermes_cli/subcommands/accounts.py
"""``hermes accounts`` subcommand parser.

Mirrors hermes_cli/subcommands/auth.py's shape: a single ``cmd_accounts``
handler wired via ``set_defaults(func=...)`` on the outer parser, with
add/list/remove as argparse subparsers dispatched internally by
hermes_cli.account_commands.accounts_command. Handler injected to avoid
importing main.
"""

from __future__ import annotations

from typing import Callable


def build_accounts_parser(subparsers, *, cmd_accounts: Callable) -> None:
    """Attach the ``accounts`` subcommand to ``subparsers``."""
    accounts_parser = subparsers.add_parser(
        "accounts",
        help="Manage named CLI accounts for codex/claude_code_sdk multi-account switching",
    )
    accounts_subparsers = accounts_parser.add_subparsers(dest="accounts_action")

    accounts_add = accounts_subparsers.add_parser(
        "add", help="Register a new named CLI account"
    )
    accounts_add.add_argument("name", help="Account name (e.g. work, personal)")
    accounts_add.add_argument(
        "--provider",
        choices=["codex", "claude_code_sdk"],
        help="Which CLI this account is for",
    )
    accounts_add.add_argument(
        "--config-dir",
        dest="config_dir",
        required=True,
        help="Path to the isolated CODEX_HOME / CLAUDE_CONFIG_DIR for this account",
    )

    accounts_list = accounts_subparsers.add_parser(
        "list", help="List registered CLI accounts"
    )
    accounts_list.add_argument(
        "--provider",
        choices=["codex", "claude_code_sdk"],
        default="",
        help="Optional provider filter",
    )

    accounts_remove = accounts_subparsers.add_parser(
        "remove", help="Remove a named CLI account"
    )
    accounts_remove.add_argument("name", help="Account name to remove")

    accounts_parser.set_defaults(func=cmd_accounts)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/hermes_cli/test_subcommands_accounts.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Wire into `hermes_cli/main.py`**

Edit `hermes-agent/hermes_cli/main.py`, changing (line ~433):

```python
from hermes_cli.subcommands.auth import build_auth_parser
```

to:

```python
from hermes_cli.subcommands.auth import build_auth_parser
from hermes_cli.subcommands.accounts import build_accounts_parser
```

Add the handler (line ~4439, immediately after `def cmd_auth(args):`'s existing body):

```python
def cmd_auth(args):
    """Manage pooled credentials."""
    from hermes_cli.auth_commands import auth_command

    auth_command(args)


def cmd_accounts(args):
    """Manage named CLI accounts (codex/claude_code_sdk multi-account registry)."""
    from hermes_cli.account_commands import accounts_command

    accounts_command(args)
```

Wire the parser (line ~14595, immediately after the existing `build_auth_parser` call):

```python
    # =========================================================================
    # auth command  (parser built in hermes_cli/subcommands/auth.py)
    # =========================================================================
    build_auth_parser(subparsers, cmd_auth=cmd_auth)

    # =========================================================================
    # accounts command  (parser built in hermes_cli/subcommands/accounts.py)
    # =========================================================================
    build_accounts_parser(subparsers, cmd_accounts=cmd_accounts)
```

- [ ] **Step 6: Run a smoke import to confirm `main.py` still parses**

Run: `cd hermes-agent && python -c "import hermes_cli.main"`
Expected: No output, exit code 0 (import succeeds — confirms no syntax/circular-import errors from the new wiring)

- [ ] **Step 7: Commit**

```bash
cd hermes-agent
git add hermes_cli/subcommands/accounts.py hermes_cli/main.py tests/hermes_cli/test_subcommands_accounts.py
git commit -m "feat: wire hermes accounts subcommand into the CLI parser"
```

---

### Task 6: `ClaudeCodeSdkTurnSession.request_interrupt()`

> Depends on Phase 1 having landed (`agent/transports/claude_code_sdk_session.py` is created by Phase 1 Task 6).

**Files:**
- Modify: `hermes-agent/agent/transports/claude_code_sdk_session.py`
- Test: `hermes-agent/tests/agent/transports/test_claude_code_sdk_session.py`

**Interfaces:**
- Consumes: `ClaudeCodeSdkTurnSession._client` (Phase 1, real — set by `ensure_started()`), `ClaudeCodeSdkClient.interrupt()` (Phase 1 Task 3, real).
- Produces: `ClaudeCodeSdkTurnSession.request_interrupt() -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# Append to hermes-agent/tests/agent/transports/test_claude_code_sdk_session.py
from unittest.mock import MagicMock


def test_request_interrupt_calls_client_interrupt():
    fake_client = MagicMock()
    session = ClaudeCodeSdkTurnSession(client_factory=lambda **kw: fake_client)
    session.ensure_started()

    session.request_interrupt()

    fake_client.interrupt.assert_called_once()


def test_request_interrupt_is_a_noop_before_start():
    session = ClaudeCodeSdkTurnSession(client_factory=lambda **kw: MagicMock())
    session.request_interrupt()  # must not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/agent/transports/test_claude_code_sdk_session.py -v -k request_interrupt`
Expected: FAIL with `AttributeError: 'ClaudeCodeSdkTurnSession' object has no attribute 'request_interrupt'`

- [ ] **Step 3: Write the minimal implementation**

Edit `hermes-agent/agent/transports/claude_code_sdk_session.py`, adding immediately after `close()` (and before `run_turn`):

```python
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

    def request_interrupt(self) -> None:
        """Best-effort interrupt of an in-flight turn. Mirrors
        CodexAppServerSession.request_interrupt() so AIAgent.interrupt() and
        switch_cli_account() can treat both transports identically via
        getattr(session, "request_interrupt", None). Safe to call before a
        client has started (no-op) or after close() (no-op)."""
        if self._client is not None:
            self._client.interrupt()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/agent/transports/test_claude_code_sdk_session.py -v`
Expected: PASS (all tests in the file, including the 2 new ones)

- [ ] **Step 5: Commit**

```bash
cd hermes-agent
git add agent/transports/claude_code_sdk_session.py tests/agent/transports/test_claude_code_sdk_session.py
git commit -m "feat: add ClaudeCodeSdkTurnSession.request_interrupt()"
```

---

### Task 7: `AIAgent.switch_cli_account()` + `interrupt()` claude_code_sdk branch

**Files:**
- Modify: `hermes-agent/run_agent.py` (add `switch_cli_account` method after `_run_codex_app_server_turn`, before `def main(`; add a `claude_code_sdk` branch to `interrupt()` right after its existing `codex_app_server` branch, ~line 2872-2882)
- Test: `hermes-agent/tests/run_agent/test_switch_cli_account.py`

**Interfaces:**
- Consumes: `agent.cli_accounts.resolve_cli_account(name: str, provider: str) -> Optional[CliAccount]` (Task 1); `AIAgent._codex_session` / `AIAgent._claude_code_session` (both already-established lazy attributes — `_codex_session` is real today, `_claude_code_session` is Phase 1's); `session.request_interrupt()` / `session.close()` (Task 6, and `CodexAppServerSession`'s existing real methods).
- Produces: `AIAgent.switch_cli_account(self, provider: str, account_name: str) -> None`; `AIAgent._active_cli_accounts: Dict[str, CliAccount]` (lazily created).

- [ ] **Step 1: Write the failing tests**

```python
# hermes-agent/tests/run_agent/test_switch_cli_account.py
"""Tests for AIAgent.switch_cli_account() — the live mid-conversation
CLI-account hot-swap (docs/design/claude-code-integration.md, "Live account
hot-swap"). Constructs a real AIAgent the same way
tests/run_agent/test_codex_app_server_integration.py does, so no real
provider/subprocess is ever contacted."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import run_agent


def _make_agent(**kwargs):
    return run_agent.AIAgent(
        api_key="stub",
        base_url="https://stub.invalid",
        provider="openai",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        **kwargs,
    )


def test_switch_cli_account_raises_for_unknown_provider():
    agent = _make_agent()
    with pytest.raises(ValueError, match="Unknown cli account provider"):
        agent.switch_cli_account("bogus", "work")


def test_switch_cli_account_raises_when_account_not_registered(monkeypatch):
    monkeypatch.setattr("agent.cli_accounts.resolve_cli_account", lambda name, provider: None)
    agent = _make_agent()
    with pytest.raises(ValueError, match="No cli_accounts entry"):
        agent.switch_cli_account("codex", "missing")


def test_switch_cli_account_closes_and_clears_live_codex_session(monkeypatch):
    from agent.cli_accounts import CliAccount

    account = CliAccount(name="work", provider="codex", config_dir="/home/u/.codex-work")
    monkeypatch.setattr("agent.cli_accounts.resolve_cli_account", lambda name, provider: account)

    agent = _make_agent()
    fake_session = MagicMock()
    agent._codex_session = fake_session

    agent.switch_cli_account("codex", "work")

    fake_session.request_interrupt.assert_called_once()
    fake_session.close.assert_called_once()
    assert agent._codex_session is None
    assert agent._active_cli_accounts["codex"] == account


def test_switch_cli_account_closes_and_clears_live_claude_code_session(monkeypatch):
    from agent.cli_accounts import CliAccount

    account = CliAccount(name="personal", provider="claude_code_sdk", config_dir="/home/u/.claude-personal")
    monkeypatch.setattr("agent.cli_accounts.resolve_cli_account", lambda name, provider: account)

    agent = _make_agent()
    fake_session = MagicMock()
    agent._claude_code_session = fake_session

    agent.switch_cli_account("claude_code_sdk", "personal")

    fake_session.request_interrupt.assert_called_once()
    fake_session.close.assert_called_once()
    assert agent._claude_code_session is None
    assert agent._active_cli_accounts["claude_code_sdk"] == account


def test_switch_cli_account_is_safe_with_no_live_session(monkeypatch):
    from agent.cli_accounts import CliAccount

    account = CliAccount(name="work", provider="codex", config_dir="/home/u/.codex-work")
    monkeypatch.setattr("agent.cli_accounts.resolve_cli_account", lambda name, provider: account)

    agent = _make_agent()
    agent.switch_cli_account("codex", "work")  # must not raise

    assert agent._active_cli_accounts["codex"] == account


def test_interrupt_calls_claude_code_session_request_interrupt():
    agent = _make_agent()
    agent.api_mode = "claude_code_sdk"  # set directly — decoupled from agent_init.py's gate
    fake_session = MagicMock()
    agent._claude_code_session = fake_session

    agent.interrupt("stop")

    fake_session.request_interrupt.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/run_agent/test_switch_cli_account.py -v`
Expected: FAIL with `AttributeError: 'AIAgent' object has no attribute 'switch_cli_account'`

- [ ] **Step 3: Write the minimal implementation**

Edit `hermes-agent/run_agent.py`. In `AIAgent.interrupt()`, immediately after the existing `codex_app_server` branch:

```python
        # Codex app-server owns its model/tool loop and watches a private
        # interrupt event rather than Hermes' per-thread flag.
        if getattr(self, "api_mode", None) == "codex_app_server":
            _codex_session = getattr(self, "_codex_session", None)
            _request_interrupt = getattr(_codex_session, "request_interrupt", None)
            if callable(_request_interrupt):
                try:
                    _request_interrupt()
                except Exception:
                    logger.debug(
                        "Failed to interrupt Codex app-server turn",
                        exc_info=True,
                    )
```

add:

```python
        # Claude Code SDK owns its own background thread + event loop and
        # watches ClaudeCodeSdkClient's internal queue rather than Hermes'
        # per-thread flag — mirrors the codex_app_server branch above.
        if getattr(self, "api_mode", None) == "claude_code_sdk":
            _claude_code_session = getattr(self, "_claude_code_session", None)
            _request_interrupt = getattr(_claude_code_session, "request_interrupt", None)
            if callable(_request_interrupt):
                try:
                    _request_interrupt()
                except Exception:
                    logger.debug(
                        "Failed to interrupt Claude Code SDK turn",
                        exc_info=True,
                    )
```

Then, immediately after `_run_codex_app_server_turn` (currently ending right before `def main(` — if Phase 1's `_run_claude_code_sdk_turn` forwarder has already landed, add this after that instead):

```python
    def switch_cli_account(self, provider: str, account_name: str) -> None:
        """Live mid-conversation hot-swap of the named CLI account used by
        the ``codex_app_server`` / ``claude_code_sdk`` runtimes.

        Resolves the named account via agent.cli_accounts, interrupts +
        closes any live session for that provider, clears the cached
        session attribute, and records the newly active account so the
        next turn's lazy-init block in codex_runtime.py / claude_code_runtime.py
        picks up the new config_dir. Conversation history is untouched —
        both transports pass Hermes's full conversation-so-far as per-turn
        context rather than owning a persistent server-side thread, so
        tearing down and respawning the subprocess does not lose history
        (see docs/design/claude-code-integration.md, "Live account hot-swap").

        Raises ValueError if provider isn't "codex"/"claude_code_sdk", or if
        no matching account is registered.
        """
        from agent.cli_accounts import resolve_cli_account

        if provider not in ("codex", "claude_code_sdk"):
            raise ValueError(f"Unknown cli account provider: {provider!r}")

        account = resolve_cli_account(account_name, provider)
        if account is None:
            raise ValueError(
                f"No cli_accounts entry named {account_name!r} for provider {provider!r}"
            )

        session_attr = "_codex_session" if provider == "codex" else "_claude_code_session"
        session = getattr(self, session_attr, None)
        if session is not None:
            request_interrupt = getattr(session, "request_interrupt", None)
            if callable(request_interrupt):
                try:
                    request_interrupt()
                except Exception:
                    logger.debug(
                        "%s interrupt failed during account switch",
                        session_attr, exc_info=True,
                    )
            try:
                session.close()
            except Exception:
                logger.debug(
                    "%s close failed during account switch",
                    session_attr, exc_info=True,
                )
            setattr(self, session_attr, None)

        if not hasattr(self, "_active_cli_accounts"):
            self._active_cli_accounts = {}
        self._active_cli_accounts[provider] = account
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/run_agent/test_switch_cli_account.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Run the existing interrupt-propagation regression suite to confirm no regressions**

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/run_agent/test_interrupt_propagation.py tests/run_agent/test_concurrent_interrupt.py -v`
Expected: PASS (no regressions from the new `interrupt()` branch)

- [ ] **Step 6: Commit**

```bash
cd hermes-agent
git add run_agent.py tests/run_agent/test_switch_cli_account.py
git commit -m "feat: add AIAgent.switch_cli_account() live account hot-swap"
```

---

### Task 8: Codex-side real fix — thread `codex_home` through `codex_runtime.py`

**Files:**
- Modify: `hermes-agent/agent/codex_runtime.py:23` (typing import), `hermes-agent/agent/codex_runtime.py:680-688` (session construction call site)
- Test: `hermes-agent/tests/agent/test_codex_runtime_account_switch.py`

**Interfaces:**
- Consumes: `AIAgent._active_cli_accounts` (Task 7), `CliAccount.config_dir` (Task 1), `CodexAppServerSession(codex_home: Optional[str] = None, ...)` (already-existing, real — `agent/transports/codex_app_server_session.py:279`).
- Produces: `agent.codex_runtime._resolve_codex_home(agent) -> Optional[str]`.

- [ ] **Step 1: Write the failing tests**

```python
# hermes-agent/tests/agent/test_codex_runtime_account_switch.py
"""Tests for the codex_home account-switch wiring in agent/codex_runtime.py
(the "in-scope, non-free work on the Codex side" fix from
docs/design/claude-code-integration.md component 2)."""
from __future__ import annotations

from types import SimpleNamespace

import run_agent
from agent.cli_accounts import CliAccount
from agent.codex_runtime import _resolve_codex_home
import agent.transports.codex_app_server_session as codex_session_mod
from agent.transports.codex_app_server_session import TurnResult


def test_resolve_codex_home_returns_none_when_no_account_selected():
    agent = SimpleNamespace()
    assert _resolve_codex_home(agent) is None


def test_resolve_codex_home_returns_none_when_only_claude_account_active():
    agent = SimpleNamespace(_active_cli_accounts={
        "claude_code_sdk": CliAccount(name="p", provider="claude_code_sdk", config_dir="/x"),
    })
    assert _resolve_codex_home(agent) is None


def test_resolve_codex_home_returns_config_dir_when_codex_account_active():
    agent = SimpleNamespace(_active_cli_accounts={
        "codex": CliAccount(name="work", provider="codex", config_dir="/home/u/.codex-work"),
    })
    assert _resolve_codex_home(agent) == "/home/u/.codex-work"


def _make_codex_agent(**kwargs):
    return run_agent.AIAgent(
        api_key="stub",
        base_url="https://stub.invalid",
        provider="openai",
        api_mode="codex_app_server",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        **kwargs,
    )


def test_run_codex_app_server_turn_threads_codex_home_from_active_account(monkeypatch):
    captured = {}
    _original_init = codex_session_mod.CodexAppServerSession.__init__

    def _recording_init(self, **kwargs):
        captured.update(kwargs)
        _original_init(self, **kwargs)

    def _fake_run_turn(self, user_input: str, **kwargs):
        return TurnResult(final_text=f"echo: {user_input}", projected_messages=[])

    monkeypatch.setattr(codex_session_mod.CodexAppServerSession, "__init__", _recording_init)
    monkeypatch.setattr(codex_session_mod.CodexAppServerSession, "run_turn", _fake_run_turn)
    monkeypatch.setattr(
        codex_session_mod.CodexAppServerSession, "ensure_started", lambda self: "thread-stub"
    )

    agent = _make_codex_agent()
    agent._active_cli_accounts = {
        "codex": CliAccount(name="work", provider="codex", config_dir="/home/u/.codex-work"),
    }

    agent._run_codex_app_server_turn(
        user_message="hello",
        original_user_message="hello",
        messages=[],
        effective_task_id="task-1",
    )

    assert captured["codex_home"] == "/home/u/.codex-work"


def test_run_codex_app_server_turn_passes_none_codex_home_by_default(monkeypatch):
    captured = {}
    _original_init = codex_session_mod.CodexAppServerSession.__init__

    def _recording_init(self, **kwargs):
        captured.update(kwargs)
        _original_init(self, **kwargs)

    def _fake_run_turn(self, user_input: str, **kwargs):
        return TurnResult(final_text=f"echo: {user_input}", projected_messages=[])

    monkeypatch.setattr(codex_session_mod.CodexAppServerSession, "__init__", _recording_init)
    monkeypatch.setattr(codex_session_mod.CodexAppServerSession, "run_turn", _fake_run_turn)
    monkeypatch.setattr(
        codex_session_mod.CodexAppServerSession, "ensure_started", lambda self: "thread-stub"
    )

    agent = _make_codex_agent()

    agent._run_codex_app_server_turn(
        user_message="hello",
        original_user_message="hello",
        messages=[],
        effective_task_id="task-1",
    )

    assert captured["codex_home"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/agent/test_codex_runtime_account_switch.py -v`
Expected: FAIL with `ImportError: cannot import name '_resolve_codex_home'`

- [ ] **Step 3: Write the minimal implementation**

Edit `hermes-agent/agent/codex_runtime.py`, changing the typing import:

```python
from typing import Any, Callable, Dict, List
```

to:

```python
from typing import Any, Callable, Dict, List, Optional
```

Add, immediately before `def run_codex_app_server_turn(`:

```python
def _resolve_codex_home(agent) -> Optional[str]:
    """Look up the active cli_accounts config_dir for the "codex" provider,
    if AIAgent.switch_cli_account() has recorded one via agent.cli_accounts.
    Returns None when no account has been explicitly selected —
    CodexAppServerSession then falls back to whichever ~/.codex the codex
    CLI is already logged into on the host (unchanged default behavior)."""
    active_accounts = getattr(agent, "_active_cli_accounts", None) or {}
    account = active_accounts.get("codex")
    return account.config_dir if account is not None else None
```

Then change the session construction call site, from:

```python
        agent._codex_session = CodexAppServerSession(
            cwd=cwd,
            approval_callback=approval_callback,
            request_routing=_ServerRequestRouting(
                auto_approve_exec=auto_approve_requests,
                auto_approve_apply_patch=auto_approve_requests,
            ),
            on_event=make_codex_app_server_event_bridge(agent),
        )
```

to:

```python
        agent._codex_session = CodexAppServerSession(
            cwd=cwd,
            codex_home=_resolve_codex_home(agent),
            approval_callback=approval_callback,
            request_routing=_ServerRequestRouting(
                auto_approve_exec=auto_approve_requests,
                auto_approve_apply_patch=auto_approve_requests,
            ),
            on_event=make_codex_app_server_event_bridge(agent),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/agent/test_codex_runtime_account_switch.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Run the existing Codex app-server integration suite to confirm no regressions**

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/run_agent/test_codex_app_server_integration.py -v`
Expected: PASS (no regressions — `codex_home=None` is the same effective behavior as omitting the kwarg, since `CodexAppServerSession.__init__`'s default is already `codex_home: Optional[str] = None`)

- [ ] **Step 6: Commit**

```bash
cd hermes-agent
git add agent/codex_runtime.py tests/agent/test_codex_runtime_account_switch.py
git commit -m "fix: thread codex_home from the active cli account into CodexAppServerSession"
```

---

### Task 9: Claude-side real fix — thread `claude_config_dir` through `claude_code_runtime.py`

> Depends on Phase 1 having landed (`agent/claude_code_runtime.py` is created by Phase 1 Tasks 5/6/8). Read the actual current file first: if Phase 1's approval-bridging task (Task 8) has landed, the lazy-init `else` branch will match the "Old" block below verbatim (`cwd`, `on_event`, `can_use_tool`); if only Phase 1's Tasks 5/6 have landed, it will instead be the simpler `cwd`/`on_event`-only version from Phase 1 Task 6 Step 10 — apply the same `claude_config_dir=` addition to whichever variant is actually present.

**Files:**
- Modify: `hermes-agent/agent/claude_code_runtime.py`
- Test: `hermes-agent/tests/agent/test_claude_code_runtime_account_switch.py`

**Interfaces:**
- Consumes: `AIAgent._active_cli_accounts` (Task 7), `CliAccount.config_dir` (Task 1), `ClaudeCodeSdkTurnSession(claude_config_dir: Optional[str] = None, ...)` (Phase 1, real signature confirmed in `docs/plans/2026-07-23-claude-code-sdk-transport-plan.md` Task 6/8).
- Produces: `agent.claude_code_runtime._resolve_claude_code_config_dir(agent) -> Optional[str]`.

- [ ] **Step 1: Write the failing tests**

```python
# hermes-agent/tests/agent/test_claude_code_runtime_account_switch.py
"""Tests for the claude_config_dir account-switch wiring in
agent/claude_code_runtime.py (the Claude-side sibling of Task 8's Codex fix)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.cli_accounts import CliAccount
from agent.claude_code_runtime import (
    _resolve_claude_code_config_dir,
    run_claude_code_sdk_turn,
)


def test_resolve_claude_code_config_dir_returns_none_when_no_account_selected():
    agent = SimpleNamespace()
    assert _resolve_claude_code_config_dir(agent) is None


def test_resolve_claude_code_config_dir_returns_none_when_only_codex_account_active():
    agent = SimpleNamespace(_active_cli_accounts={
        "codex": CliAccount(name="w", provider="codex", config_dir="/x"),
    })
    assert _resolve_claude_code_config_dir(agent) is None


def test_resolve_claude_code_config_dir_returns_config_dir_when_account_active():
    agent = SimpleNamespace(_active_cli_accounts={
        "claude_code_sdk": CliAccount(
            name="personal", provider="claude_code_sdk", config_dir="/home/u/.claude-personal"
        ),
    })
    assert _resolve_claude_code_config_dir(agent) == "/home/u/.claude-personal"


def test_run_claude_code_sdk_turn_threads_claude_config_dir_from_active_account(monkeypatch):
    captured = {}

    class _RecordingSession:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_turn(self, user_input):
            return MagicMock(
                final_text=f"echo: {user_input}", interrupted=False, error=None,
                should_retire=False, projected_messages=[], tool_iterations=0,
            )

    monkeypatch.setattr(
        "agent.transports.claude_code_sdk_session.ClaudeCodeSdkTurnSession",
        _RecordingSession,
    )

    agent = SimpleNamespace(
        _claude_code_session=None,
        session_cwd="/tmp",
        _active_cli_accounts={
            "claude_code_sdk": CliAccount(
                name="personal", provider="claude_code_sdk", config_dir="/home/u/.claude-personal"
            ),
        },
    )

    run_claude_code_sdk_turn(
        agent, user_message="hi", original_user_message="hi", messages=[], effective_task_id="t-1",
    )

    assert captured["claude_config_dir"] == "/home/u/.claude-personal"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/agent/test_claude_code_runtime_account_switch.py -v`
Expected: FAIL with `ImportError: cannot import name '_resolve_claude_code_config_dir'`

- [ ] **Step 3: Write the minimal implementation**

Add to `hermes-agent/agent/claude_code_runtime.py`, immediately before `def run_claude_code_sdk_turn(`:

```python
def _resolve_claude_code_config_dir(agent) -> Optional[str]:
    """Look up the active cli_accounts config_dir for the "claude_code_sdk"
    provider, if AIAgent.switch_cli_account() has recorded one. Returns None
    when no account has been explicitly selected — ClaudeCodeSdkTurnSession
    then falls back to the claude CLI's default ~/.claude (unchanged default
    behavior)."""
    active_accounts = getattr(agent, "_active_cli_accounts", None) or {}
    account = active_accounts.get("claude_code_sdk")
    return account.config_dir if account is not None else None
```

Read the current lazy-init `else` branch inside `run_claude_code_sdk_turn` and add `claude_config_dir=_resolve_claude_code_config_dir(agent)`. If it matches Phase 1's final (Task 8) shape:

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

change it to:

```python
        else:
            from agent.transports.claude_code_sdk_session import (
                ClaudeCodeSdkTurnSession,
            )

            cwd = getattr(agent, "session_cwd", None)
            agent._claude_code_session = ClaudeCodeSdkTurnSession(
                cwd=cwd,
                claude_config_dir=_resolve_claude_code_config_dir(agent),
                on_event=make_claude_code_sdk_event_bridge(agent),
                can_use_tool=_make_claude_code_approval_callback(agent),
            )
```

(If the current file instead has the earlier, `can_use_tool`-less shape from Phase 1 Task 6 Step 10, add `claude_config_dir=_resolve_claude_code_config_dir(agent),` to that version's `ClaudeCodeSdkTurnSession(...)` call the same way.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/agent/test_claude_code_runtime_account_switch.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the full Phase 1 claude_code_sdk suite to confirm no regressions**

Run:
```bash
cd hermes-agent
./scripts/run_tests.sh \
  tests/agent/test_claude_code_runtime_dispatch.py \
  tests/agent/test_claude_code_sdk_event_bridge.py \
  tests/agent/transports/test_claude_code_sdk_session.py \
  -v
```
Expected: PASS (no regressions)

- [ ] **Step 6: Commit**

```bash
cd hermes-agent
git add agent/claude_code_runtime.py tests/agent/test_claude_code_runtime_account_switch.py
git commit -m "fix: thread claude_config_dir from the active cli account into ClaudeCodeSdkTurnSession"
```

---

### Task 10: `/cli-account` slash command

**Files:**
- Create: `hermes-agent/hermes_cli/cli_account_switch.py`
- Test: `hermes-agent/tests/hermes_cli/test_cli_account_switch.py`
- Modify: `hermes-agent/hermes_cli/commands.py:136-138` (add `CommandDef`)
- Modify: `hermes-agent/cli.py:8814` (add handler), `hermes-agent/cli.py:9180` (add dispatch branch)
- Test: `hermes-agent/tests/hermes_cli/test_commands.py` (confirm the new command resolves)

**Interfaces:**
- Consumes: `AIAgent.switch_cli_account(provider: str, account_name: str) -> None` (Task 7); `load_cli_accounts()` (Task 1).
- Produces: `hermes_cli.cli_account_switch.parse_args(arg_string: str) -> tuple[Optional[str], Optional[str], list[str]]`, `hermes_cli.cli_account_switch.list_accounts_message() -> str`, `hermes_cli.cli_account_switch.apply(agent, provider: Optional[str], account_name: Optional[str]) -> CliAccountSwitchStatus`, `HermesCLI._handle_cli_account_command(self, cmd_original: str) -> None`.

- [ ] **Step 1: Write the failing tests for the shared logic**

```python
# hermes-agent/tests/hermes_cli/test_cli_account_switch.py
"""Tests for the /cli-account slash-command shared logic.

These cover the pure-Python state machine, mirroring
tests/hermes_cli/test_codex_runtime_switch.py's split (CLI/gateway handlers
are surface-specific and tested separately, if at all — see that file's own
module docstring)."""
from __future__ import annotations

from unittest.mock import MagicMock

from hermes_cli import cli_account_switch as cas


class TestParseArgs:
    def test_empty_args_returns_none_triple(self):
        assert cas.parse_args("") == (None, None, [])

    def test_whitespace_only_returns_none_triple(self):
        assert cas.parse_args("   ") == (None, None, [])

    def test_valid_args_parse(self):
        assert cas.parse_args("codex work") == ("codex", "work", [])

    def test_valid_args_case_insensitive_provider(self):
        assert cas.parse_args("CLAUDE_CODE_SDK personal") == ("claude_code_sdk", "personal", [])

    def test_unknown_provider_returns_error(self):
        provider, name, errors = cas.parse_args("anthropic work")
        assert provider is None
        assert errors and "Unknown provider" in errors[0]

    def test_missing_account_name_returns_usage_error(self):
        provider, name, errors = cas.parse_args("codex")
        assert provider is None
        assert errors and "Usage:" in errors[0]


class TestApply:
    def test_no_args_lists_accounts(self, monkeypatch):
        monkeypatch.setattr(cas, "list_accounts_message", lambda: "work (codex): /a")
        status = cas.apply(MagicMock(), None, None)
        assert status.success is True
        assert status.message == "work (codex): /a"

    def test_apply_calls_switch_cli_account(self):
        agent = MagicMock()
        status = cas.apply(agent, "codex", "work")
        agent.switch_cli_account.assert_called_once_with("codex", "work")
        assert status.success is True
        assert "Switched codex to account 'work'" in status.message

    def test_apply_surfaces_value_error_as_failure(self):
        agent = MagicMock()
        agent.switch_cli_account.side_effect = ValueError("No cli_accounts entry named 'x'")
        status = cas.apply(agent, "codex", "x")
        assert status.success is False
        assert "No cli_accounts entry" in status.message

    def test_apply_with_no_active_agent_fails_gracefully(self):
        status = cas.apply(None, "codex", "work")
        assert status.success is False
        assert "No active agent" in status.message
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/hermes_cli/test_cli_account_switch.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'hermes_cli.cli_account_switch'`

- [ ] **Step 3: Write the minimal implementation**

```python
# hermes-agent/hermes_cli/cli_account_switch.py
"""Shared logic for the /cli-account slash command.

Live mid-conversation hot-swap of the named CLI account used by the
codex_app_server / claude_code_sdk runtimes. Unlike /codex-runtime (which
only persists a config value for the *next* session — see
hermes_cli/codex_runtime_switch.py), this command calls
AIAgent.switch_cli_account() directly so the swap takes effect on the very
next turn of the *current* session (see
docs/design/claude-code-integration.md, "Live account hot-swap").

Both CLI (cli.py) and, in future, gateway/Desktop surfaces can call into
this module so the behavior stays identical across surfaces — only the CLI
trigger is wired in this plan; gateway/Desktop wiring is a follow-up that
reuses apply() unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

VALID_PROVIDERS = ("codex", "claude_code_sdk")


@dataclass
class CliAccountSwitchStatus:
    """Result of a /cli-account invocation. Callers render this however
    suits their surface."""

    success: bool
    provider: Optional[str] = None
    account_name: Optional[str] = None
    message: str = ""


def parse_args(arg_string: str) -> tuple[Optional[str], Optional[str], list[str]]:
    """Parse '<provider> <account_name>' slash-command args.

    No args              -> (None, None, []) — caller should list accounts
    '<provider> <name>'  -> (provider, name, [])
    anything else        -> (None, None, [error])
    """
    raw = (arg_string or "").strip()
    if not raw:
        return None, None, []
    parts = raw.split(None, 1)
    if len(parts) != 2:
        return None, None, [
            f"Usage: /cli-account <{'/'.join(VALID_PROVIDERS)}> <account-name>"
        ]
    provider, account_name = parts[0].strip().lower(), parts[1].strip()
    if provider not in VALID_PROVIDERS:
        return None, None, [
            f"Unknown provider {provider!r}. Use one of: {', '.join(VALID_PROVIDERS)}"
        ]
    if not account_name:
        return None, None, ["Account name is required."]
    return provider, account_name, []


def list_accounts_message() -> str:
    """Render every registered cli account for the no-args /cli-account
    invocation."""
    from agent.cli_accounts import load_cli_accounts

    accounts = load_cli_accounts()
    if not accounts:
        return (
            "No cli_accounts configured. Add one with "
            "`hermes accounts add <name> --provider <codex|claude_code_sdk> "
            "--config-dir <path>`."
        )
    return "\n".join(f"{a.name} ({a.provider}): {a.config_dir}" for a in accounts)


def apply(agent, provider: Optional[str], account_name: Optional[str]) -> CliAccountSwitchStatus:
    """Top-level entry point used by the CLI handler. ``agent`` is the live
    AIAgent instance for this session (None if no turn has run yet)."""
    if provider is None or account_name is None:
        return CliAccountSwitchStatus(success=True, message=list_accounts_message())

    if agent is None:
        return CliAccountSwitchStatus(
            success=False, provider=provider, account_name=account_name,
            message="No active agent for this session yet — send a message first.",
        )

    try:
        agent.switch_cli_account(provider, account_name)
    except ValueError as exc:
        return CliAccountSwitchStatus(
            success=False, provider=provider, account_name=account_name, message=str(exc)
        )
    return CliAccountSwitchStatus(
        success=True, provider=provider, account_name=account_name,
        message=f"Switched {provider} to account {account_name!r}. Effective on the next turn.",
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/hermes_cli/test_cli_account_switch.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Register the `CommandDef`**

Edit `hermes-agent/hermes_cli/commands.py`, changing:

```python
    CommandDef("codex-runtime", "Toggle codex app-server runtime for OpenAI/Codex models",
               "Configuration", aliases=("codex_runtime",),
               args_hint="[auto|codex_app_server]"),

    CommandDef("personality", "Set a predefined personality", "Configuration",
```

to:

```python
    CommandDef("codex-runtime", "Toggle codex app-server runtime for OpenAI/Codex models",
               "Configuration", aliases=("codex_runtime",),
               args_hint="[auto|codex_app_server]"),
    CommandDef("cli-account", "Switch the named CLI account for codex/claude_code_sdk mid-conversation",
               "Configuration", aliases=("cli_account",),
               args_hint="[provider account-name]"),

    CommandDef("personality", "Set a predefined personality", "Configuration",
```

- [ ] **Step 6: Write and run a test confirming the command resolves**

Append to `hermes-agent/tests/hermes_cli/test_commands.py`:

```python
def test_cli_account_command_resolves():
    assert resolve_command("cli-account").name == "cli-account"
    assert resolve_command("cli_account").name == "cli-account"
```

Run: `cd hermes-agent && ./scripts/run_tests.sh tests/hermes_cli/test_commands.py -v -k cli_account`
Expected: PASS (1 test)

- [ ] **Step 7: Wire the CLI handler and dispatch branch**

Edit `hermes-agent/cli.py`, adding immediately after `_handle_codex_runtime`'s existing body (before `def _should_handle_model_command_inline`):

```python
    def _handle_cli_account_command(self, cmd_original: str) -> None:
        """Handle /cli-account — live mid-conversation CLI-account hot-swap.

        Usage:
            /cli-account                          — list registered accounts
            /cli-account codex work                — switch codex to "work"
            /cli-account claude_code_sdk personal  — switch claude_code_sdk to "personal"
        """
        from hermes_cli import cli_account_switch as cas

        parts = cmd_original.split(None, 1)
        raw_args = parts[1].strip() if len(parts) > 1 else ""
        provider, account_name, errors = cas.parse_args(raw_args)
        if errors:
            for err in errors:
                _cprint(f"❌ {err}")
            return

        status = cas.apply(self.agent, provider, account_name)
        prefix = "✓" if status.success else "✗"
        for line in status.message.splitlines():
            _cprint(f"  {prefix} {line}")
```

Then change:

```python
        elif canonical == "codex-runtime":
            self._handle_codex_runtime(cmd_original)

        elif canonical == "personality":
```

to:

```python
        elif canonical == "codex-runtime":
            self._handle_codex_runtime(cmd_original)
        elif canonical == "cli-account":
            self._handle_cli_account_command(cmd_original)

        elif canonical == "personality":
```

- [ ] **Step 8: Run a smoke import to confirm `cli.py` still parses**

Run: `cd hermes-agent && python -c "import cli"`
Expected: No output, exit code 0

- [ ] **Step 9: Run the full new-module test suite together**

Run:
```bash
cd hermes-agent
./scripts/run_tests.sh \
  tests/agent/test_cli_accounts.py \
  tests/hermes_cli/test_account_commands.py \
  tests/hermes_cli/test_subcommands_accounts.py \
  tests/hermes_cli/test_cli_account_switch.py \
  tests/run_agent/test_switch_cli_account.py \
  tests/agent/test_codex_runtime_account_switch.py \
  tests/hermes_cli/test_set_config_value.py \
  tests/hermes_cli/test_commands.py \
  -v
```
Expected: PASS (all tests, 0 failures) — omit `tests/agent/test_claude_code_runtime_account_switch.py` and `tests/agent/transports/test_claude_code_sdk_session.py` from this run if Phase 1 has not landed yet on this branch.

- [ ] **Step 10: Commit**

```bash
cd hermes-agent
git add hermes_cli/cli_account_switch.py hermes_cli/commands.py cli.py tests/hermes_cli/test_cli_account_switch.py tests/hermes_cli/test_commands.py
git commit -m "feat: add /cli-account slash command for live CLI-account hot-swap"
```

---

## What's deliberately NOT in this plan

- **Desktop UI account-picker action** — Phase 3 UI work wires a Desktop action against the same `AIAgent.switch_cli_account()` method this plan implements and fully tests; no Desktop code is touched here.
- **Sub-agent transcript persistence, the `claude_subagent_task` tool_call name, and `TaskStartedMessage`/`TaskUpdatedMessage`/`TaskProgressMessage`/`TaskNotificationMessage` handling** — Phase 3's job (sub-agent visibility), not touched here.
- **Gateway/Telegram/Discord/Slack `/cli-account` wiring** — only the CLI (`cli.py`) trigger is built here. A follow-up can call `hermes_cli.cli_account_switch.apply()` (already surface-agnostic — takes an `agent` and returns a `CliAccountSwitchStatus`, same shape `/codex-runtime`'s `codex_runtime_switch.apply()` uses across `cli.py` and `gateway/slash_commands.py`) from those surfaces unchanged.
- **Interactive `hermes accounts add` prompting for name/config-dir** (only the provider prompt is interactive, mirroring `_pick_provider`) — a fuller `_interactive_accounts_add()` matching `auth_commands._interactive_add()` is a reasonable follow-up but not required for a working `hermes accounts add <name> --provider <p> --config-dir <dir>` command.

## Self-review notes

- **Placeholder scan:** no `TBD`/`TODO`/"add error handling"/"similar to Task N" phrasing anywhere in the tasks above; every step shows complete, real code.
- **Type/signature consistency:** `CliAccount(name, provider, config_dir)` (Task 1) is used with identical field names and order in every later task (3, 4, 5, 7, 8, 9). `resolve_cli_account(name, provider)` (Task 1) and `AIAgent.switch_cli_account(provider, account_name)` (Task 7) intentionally have their two string arguments in different orders (matching each function's own real signature) — Task 7's implementation calls `resolve_cli_account(account_name, provider)`, correctly reordering at the call site. `probe_cli_account(account) -> tuple[bool, str]` (Task 3) is called identically in Task 4. `request_interrupt()` (Task 6, and pre-existing on `CodexAppServerSession`) is looked up via `getattr(session, "request_interrupt", None)` identically in Task 7. `_active_cli_accounts` (a plain `Dict[str, CliAccount]`, established in Task 7) is read with the identical `getattr(agent, "_active_cli_accounts", None) or {}` pattern in both Task 8's `_resolve_codex_home` and Task 9's `_resolve_claude_code_config_dir`.
- **Spec-coverage check against "What Phase 2 must cover":**
  1. `agent/cli_accounts.py` (`CliAccount`, `load_cli_accounts`, `resolve_cli_account`, `probe_cli_account`) — Tasks 1, 3.
  2. Config schema slot in `hermes_cli/config.py` — Task 2.
  3. `hermes_cli/subcommands/accounts.py` + `hermes_cli/account_commands.py` — Tasks 4, 5.
  4. Naming-collision avoidance (`agent/credential_sources.py` lines ~399-403) — addressed in Global Constraints and `agent/cli_accounts.py`'s module docstring; `"claude_code_sdk"` used everywhere instead of the colliding bare `"claude_code"`.
  5. `AIAgent.switch_cli_account()` (real, tested) + slash-command trigger (real, tested) + Desktop stub note — Tasks 7, 10 (Desktop noted as explicitly out of scope, per instructions).
  6. Codex-side `codex_home` threading fix in `codex_runtime.py`'s `run_codex_app_server_turn` construction call site — Task 8.
  7. (Bonus, needed for `switch_cli_account`'s interrupt step to be real rather than a no-op on the Claude side) `ClaudeCodeSdkTurnSession.request_interrupt()` — Task 6 — and the Claude-side `claude_config_dir` threading fix mirroring Task 8 — Task 9.
