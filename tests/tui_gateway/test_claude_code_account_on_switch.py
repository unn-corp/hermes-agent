"""Selecting a model from a Claude Code section must select that ACCOUNT too.

The picker's per-subscription sections are only honest if picking
"Claude Code (personal) → Sonnet 5" actually runs on the personal
subscription. With a live session the switch goes through the gateway
(config.set -> _apply_model_switch), which translated the provider but left
claude_code.config_dir pointing at whatever account was previously active —
so the section label lied.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import tui_gateway.server as srv


def test_helper_switches_the_agent_account():
    agent = MagicMock()
    srv._apply_claude_code_account_selection(agent, "claude-code:personal")
    agent.switch_cli_account.assert_called_once_with("claude_code_sdk", "personal")


def test_real_provider_is_a_noop():
    agent = MagicMock()
    srv._apply_claude_code_account_selection(agent, "anthropic")
    agent.switch_cli_account.assert_not_called()


def test_no_agent_is_a_noop():
    srv._apply_claude_code_account_selection(None, "claude-code:work")  # must not raise


def test_unregistered_account_never_propagates():
    """switch_cli_account raises ValueError for an unknown account. The model
    switch itself already succeeded, so that must not turn into a failed
    switch the user sees as a rollback."""
    agent = MagicMock()
    agent.switch_cli_account.side_effect = ValueError("No cli_accounts entry named 'ghost'")
    srv._apply_claude_code_account_selection(agent, "claude-code:ghost")  # must not raise
