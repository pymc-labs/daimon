"""The prompts that connect a coding tool: one per tool, a code in them and never a key."""

from __future__ import annotations

import pytest
from daimon.core.coding_key_prompts import CODING_CLIENTS, client_name, connect_prompt

CLAIM = "https://daimon.example.com/coding-keys/claim"


def _prompt(client: str, *, name: str = "helper", code: str = "CODE123") -> str:
    return connect_prompt(
        client,
        agent_name=name,
        public_url="https://daimon.example.com/mcp",
        claim_url=CLAIM,
        code=code,
    )


@pytest.mark.parametrize("client", CODING_CLIENTS)
def test_a_prompt_names_the_server_the_code_the_claim_and_the_rules(client: str) -> None:
    prompt = _prompt(client)

    assert "daimon-helper" in prompt, "the entry is named for the agent"
    assert "Bearer <key>" in prompt, "the header carries a placeholder, never a key"
    assert f"curl -fsS -X POST {CLAIM} -d code=CODE123" in prompt, "one command trades the code"
    assert "Never print, read back or echo it" in prompt
    assert "works once" in prompt and "10 minutes" in prompt


def test_each_tool_gets_its_own_file_and_restart() -> None:
    assert "~/.claude.json" in _prompt("claude-code") and "restart Claude Code" in _prompt(
        "claude-code"
    )
    assert "~/.codex/config.toml" in _prompt("codex") and "[mcp_servers.daimon-helper]" in _prompt(
        "codex"
    )
    assert "~/.cursor/mcp.json" in _prompt("cursor")
    assert "~/.pi/agent/mcp.json" in _prompt("pi") and "/reload" in _prompt("pi")
    assert client_name("mcp") == "Other MCP"


def test_an_agent_name_with_odd_characters_stays_valid_in_each_format() -> None:
    codex = _prompt("codex", name='my "agent"')
    assert '[mcp_servers."daimon-my \\"agent\\""]' in codex, "TOML quotes the key"
    assert '"daimon-my \\"agent\\""' in _prompt("cursor", name='my "agent"')
