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
