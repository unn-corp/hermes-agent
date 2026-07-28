"""Each Claude Code subscription gets its own section in the model picker.

A synthetic provider slug (claude-code:<name>) carries the choice from the
picker to the write path, where it is translated back to the real `anthropic`
provider plus that account's CLAUDE_CONFIG_DIR. The slug must never reach
config.yaml — a bogus model.provider would break model resolution entirely.
"""
from __future__ import annotations

import pytest

from hermes_cli.inventory import (
    CLAUDE_CODE_ROW_PREFIX,
    _append_claude_code_account_rows,
    claude_code_account_from_slug,
)

ACCOUNTS = {
    "personal": {"provider": "claude_code_sdk", "config_dir": "/home/u/.claude"},
    "work": {"provider": "claude_code_sdk", "config_dir": "/home/u/.claude-work"},
    "boxed": {"provider": "codex", "config_dir": "/home/u/.codex"},
}


def _cfg(runtime="claude_code_sdk", config_dir="/home/u/.claude"):
    return {
        "model": {"default": "claude-fable-5", "provider": "anthropic", "anthropic_runtime": runtime},
        "claude_code": {"config_dir": config_dir},
        "cli_accounts": ACCOUNTS,
    }


def _anthropic_row():
    return {
        "slug": "anthropic", "name": "Anthropic", "models": ["claude-fable-5", "claude-sonnet-5"],
        "total_models": 2, "authenticated": True, "is_current": True, "is_user_defined": False,
        "source": "hermes",
    }


class TestSlugRoundTrip:
    def test_parses_a_synthetic_slug(self):
        assert claude_code_account_from_slug(f"{CLAUDE_CODE_ROW_PREFIX}work") == "work"

    def test_ignores_a_real_provider_slug(self):
        for slug in ("anthropic", "openrouter", "", None):
            assert claude_code_account_from_slug(slug) is None

    def test_tolerates_a_name_containing_a_colon(self):
        assert claude_code_account_from_slug(f"{CLAUDE_CODE_ROW_PREFIX}odd:name") == "odd:name"


class TestRowEmission:
    def test_one_row_per_claude_account_replacing_the_generic_one(self):
        rows = [_anthropic_row()]
        _append_claude_code_account_rows(rows, _cfg(), current_provider="anthropic")
        slugs = [r["slug"] for r in rows]

        assert f"{CLAUDE_CODE_ROW_PREFIX}personal" in slugs
        assert f"{CLAUDE_CODE_ROW_PREFIX}work" in slugs
        # The generic Anthropic row is replaced — keeping it would mean four
        # sections, one of which silently uses whichever account is default.
        assert "anthropic" not in slugs
        # Codex accounts are not Anthropic model sources.
        assert f"{CLAUDE_CODE_ROW_PREFIX}boxed" not in slugs

    def test_rows_inherit_the_model_list(self):
        rows = [_anthropic_row()]
        _append_claude_code_account_rows(rows, _cfg(), current_provider="anthropic")
        for row in rows:
            assert row["models"] == ["claude-fable-5", "claude-sonnet-5"]

    def test_only_the_active_account_is_current(self):
        rows = [_anthropic_row()]
        _append_claude_code_account_rows(rows, _cfg(config_dir="/home/u/.claude-work"), current_provider="anthropic")
        current = [r["slug"] for r in rows if r.get("is_current")]
        assert current == [f"{CLAUDE_CODE_ROW_PREFIX}work"]

    def test_no_rows_when_runtime_is_off(self):
        """Runtime off means Anthropic is the plain HTTP API — the generic row
        must survive untouched."""
        rows = [_anthropic_row()]
        _append_claude_code_account_rows(rows, _cfg(runtime="auto"), current_provider="anthropic")
        assert [r["slug"] for r in rows] == ["anthropic"]

    def test_no_rows_without_an_anthropic_row(self):
        """Anthropic gated out (not explicitly configured) means no models to
        clone — emitting bare sections would be worse than none."""
        rows = [{"slug": "openrouter", "name": "OpenRouter", "models": ["x"]}]
        _append_claude_code_account_rows(rows, _cfg(), current_provider="openrouter")
        assert [r["slug"] for r in rows] == ["openrouter"]

    def test_no_accounts_registered_leaves_the_generic_row(self):
        cfg = _cfg()
        cfg["cli_accounts"] = {}
        rows = [_anthropic_row()]
        _append_claude_code_account_rows(rows, cfg, current_provider="anthropic")
        assert [r["slug"] for r in rows] == ["anthropic"]

    def test_never_raises_on_junk(self):
        for junk in ({}, {"model": "bare"}, {"model": {"anthropic_runtime": "claude_code_sdk"}, "cli_accounts": "no"}):
            rows = [_anthropic_row()]
            _append_claude_code_account_rows(rows, junk, current_provider="anthropic")


class TestRowsAreOptIn:
    """Per-subscription rows carry a synthetic slug that only the web write
    path translates. Any consumer that persists `model.provider` straight from
    the picker (the CLI/TUI `/model` command, MoA config, dashboard plugins)
    must NOT see them, or it would write a provider that does not exist.
    """

    def test_default_off(self, monkeypatch):
        import hermes_cli.config as cfgmod
        from hermes_cli.inventory import build_models_payload, load_picker_context

        monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: {
            "model": {"provider": "anthropic", "default": "claude-fable-5",
                      "anthropic_runtime": "claude_code_sdk"},
            "claude_code": {"config_dir": "/home/u/.claude"},
            "cli_accounts": ACCOUNTS,
        })
        payload = build_models_payload(load_picker_context())
        slugs = [r["slug"] for r in payload["providers"]]
        assert not [s for s in slugs if s.startswith(CLAUDE_CODE_ROW_PREFIX)]

    def test_opt_in_emits_them(self, monkeypatch):
        import hermes_cli.config as cfgmod
        from hermes_cli.inventory import build_models_payload, load_picker_context

        monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: {
            "model": {"provider": "anthropic", "default": "claude-fable-5",
                      "anthropic_runtime": "claude_code_sdk"},
            "claude_code": {"config_dir": "/home/u/.claude"},
            "cli_accounts": ACCOUNTS,
        })
        payload = build_models_payload(load_picker_context(), claude_code_sections=True)
        slugs = [r["slug"] for r in payload["providers"]]
        assert f"{CLAUDE_CODE_ROW_PREFIX}personal" in slugs
