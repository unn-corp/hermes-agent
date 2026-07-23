"""Claude Code CLI transport via claude-agent-sdk-python.

Wraps claude_agent_sdk.ClaudeSDKClient — a real asyncio-native PyPI package
(async def query(), async for message in client.receive_messages()) — inside
a dedicated background thread running its own asyncio event loop, exposing a
synchronous, blocking-queue facade.

This is a different shape than its sibling agent/transports/codex_app_server.py,
which hand-rolls a JSON-RPC-over-stdio wire client with zero package
dependency. That module's own comment explains Hermes deliberately avoids
asyncio in the main call path because AIAgent.run_conversation() is
synchronous. This module resolves the same tension by isolating all asyncio
usage inside its own thread — it never leaks upward.

Status: optional opt-in runtime gated behind `model.anthropic_runtime ==
"claude_code_sdk"`. Hermes' default tool dispatch is unchanged when this
runtime is not selected.
"""

from __future__ import annotations

import subprocess
from typing import Optional

# Floor chosen for stream-json + permission-hook + Task-tracking support in
# the CLI's own protocol surface (verified locally against claude 2.1.217).
# Bump this the same way MIN_CODEX_VERSION gets bumped: a one-line change.
MIN_CLAUDE_VERSION = (2, 0, 0)


def parse_claude_version(output: str) -> Optional[tuple[int, int, int]]:
    """Parse `claude --version` output. Returns (major, minor, patch) or None."""
    import re

    match = re.search(r"(\d+)\.(\d+)\.(\d+)", output or "")
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def check_claude_binary(
    claude_bin: str = "claude", min_version: tuple[int, int, int] = MIN_CLAUDE_VERSION
) -> tuple[bool, str]:
    """Verify the claude CLI is installed and meets the minimum version.

    Returns (ok, message). The claude-agent-sdk package version pin alone
    doesn't guarantee the `claude` CLI binary itself is present or
    compatible — the SDK shells out to whatever binary is on PATH (or
    cli_path) at runtime.
    """
    try:
        proc = subprocess.run(
            [claude_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return False, (
            f"claude CLI not found at {claude_bin!r}. Install with: "
            f"npm i -g @anthropic-ai/claude-code"
        )
    except subprocess.TimeoutExpired:
        return False, "claude --version timed out"
    if proc.returncode != 0:
        return False, f"claude --version exited {proc.returncode}: {proc.stderr.strip()}"
    version = parse_claude_version(proc.stdout)
    if version is None:
        return False, f"could not parse claude version from: {proc.stdout!r}"
    if version < min_version:
        return False, (
            f"claude {'.'.join(map(str, version))} is older than required "
            f"{'.'.join(map(str, min_version))}. Run: "
            f"npm i -g @anthropic-ai/claude-code"
        )
    return True, ".".join(map(str, version))
