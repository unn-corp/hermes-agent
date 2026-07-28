"""resolve_provider must absorb the synthetic picker slug.

The per-subscription picker rows use `claude-code:<name>` slugs. Translating
only at the config-write path proved insufficient: the Desktop composer pins
the selected provider in ITS OWN localStorage and sends it as a per-session
override, which reached agent init and failed with

    Unknown provider 'claude-code:work'

resolve_provider is the one function every provider string funnels through, so
absorbing it here covers config, session overrides, and any future caller.
"""
from __future__ import annotations

import pytest

from hermes_cli.auth import resolve_provider


def test_synthetic_slug_resolves_to_anthropic():
    assert resolve_provider("claude-code:work") == "anthropic"


def test_every_account_name_resolves():
    for name in ("personal", "work", "alt", "anything-at-all"):
        assert resolve_provider(f"claude-code:{name}") == "anthropic"


def test_bare_prefix_is_not_swallowed():
    """`claude-code:` with no account is malformed — it must not silently
    resolve, or a typo would look like it worked."""
    with pytest.raises(Exception):
        resolve_provider("claude-code:")


def test_real_providers_are_unaffected():
    assert resolve_provider("anthropic") == "anthropic"
    assert resolve_provider("openrouter") == "openrouter"


def test_unknown_provider_still_raises():
    with pytest.raises(Exception):
        resolve_provider("definitely-not-a-provider")
