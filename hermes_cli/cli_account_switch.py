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
