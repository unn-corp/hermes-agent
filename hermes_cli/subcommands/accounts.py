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
