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
