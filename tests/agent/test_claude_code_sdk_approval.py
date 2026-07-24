import asyncio
from unittest.mock import MagicMock, patch

from agent.claude_code_runtime import _make_claude_code_approval_callback


def test_approval_callback_allows_on_once():
    agent = MagicMock()
    callback = _make_claude_code_approval_callback(agent)

    with patch("tools.approval.is_approval_bypass_active", return_value=False), \
         patch("tools.terminal_tool._get_approval_callback", return_value=None), \
         patch("tools.approval.prompt_dangerous_approval", return_value="once"):
        result = asyncio.run(callback("Bash", {"command": "ls"}, MagicMock()))

    assert type(result).__name__ == "PermissionResultAllow"


def test_approval_callback_denies_on_deny():
    agent = MagicMock()
    callback = _make_claude_code_approval_callback(agent)

    with patch("tools.approval.is_approval_bypass_active", return_value=False), \
         patch("tools.terminal_tool._get_approval_callback", return_value=None), \
         patch("tools.approval.prompt_dangerous_approval", return_value="deny"):
        result = asyncio.run(callback("Bash", {"command": "rm -rf /"}, MagicMock()))

    assert type(result).__name__ == "PermissionResultDeny"


def test_approval_callback_auto_allows_when_bypass_active():
    agent = MagicMock()
    callback = _make_claude_code_approval_callback(agent)

    with patch("tools.approval.is_approval_bypass_active", return_value=True):
        result = asyncio.run(callback("Bash", {"command": "ls"}, MagicMock()))

    assert type(result).__name__ == "PermissionResultAllow"
