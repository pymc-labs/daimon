"""The prompts that connect a coding tool: one per tool, a code in them and never a key."""

from __future__ import annotations

import re

import pytest
from daimon.core.coding_key_prompts import (
    CODING_CLIENTS,
    client_name,
    connect_prompt,
    server_slug,
)

CLAIM = "https://daimon.example.com/coding-keys/claim"


def _prompt(client: str, *, slug: str = "helper", code: str = "CODE123") -> str:
    return connect_prompt(
        client,
        slug=slug,
        public_url="https://daimon.example.com/mcp",
        claim_url=CLAIM,
        code=code,
    )


@pytest.mark.parametrize("client", CODING_CLIENTS)
def test_a_prompt_names_the_server_the_code_the_claim_and_the_rules(client: str) -> None:
    prompt = _prompt(client)

    assert "daimon-helper" in prompt, "the entry is named for the agent's slug"
    assert "Bearer <key>" in prompt, "the header carries a placeholder, never a key"
    assert f"curl -fsS -X POST {CLAIM} -d code=CODE123" in prompt, "one command trades the code"
    assert "Never print, read back or echo it" in prompt, "the key is never shown"
    assert "works once" in prompt and "10 minutes" in prompt, "the code's life is stated"
    assert "a Daimon agent" in prompt, "the prose names no agent"


@pytest.mark.parametrize("client", CODING_CLIENTS)
def test_a_prompt_stops_and_reports_when_the_command_fails(client: str) -> None:
    prompt = _prompt(client)

    assert "exits non-zero or prints nothing" in prompt, "both failures are named"
    assert "stop and tell me what it said" in prompt, "the agent stops and reports"
    assert "make up a key" in prompt, "the agent does not invent a key"


def test_each_tool_gets_its_own_file_and_restart() -> None:
    claude = _prompt("claude-code")
    assert "~/.claude.json" in claude, "Claude Code's file"
    assert "restart Claude Code" in claude, "Claude Code's restart"
    codex = _prompt("codex")
    assert "~/.codex/config.toml" in codex, "Codex's file"
    assert "[mcp_servers.daimon-helper]" in codex, "Codex's table"
    assert "~/.cursor/mcp.json" in _prompt("cursor"), "Cursor's file"
    pi = _prompt("pi")
    assert "~/.pi/agent/mcp.json" in pi, "Pi's file"
    assert "/reload" in pi, "Pi's reload"
    assert client_name("mcp") == "Other MCP", "the generic tool's name"


@pytest.mark.parametrize(
    ("name", "ma_agent_id", "expected"),
    [
        ("Helper", "agent_01ABC", "helper"),
        ('my "agent"', "agent_01ABC", "my-agent"),
        ("  Q3 -- Report Bot!  ", "agent_01ABC", "q3-report-bot"),
        ("x" * 80, "agent_01ABC", "x" * 32),
        ("a" * 31 + " b", "agent_01ABC", "a" * 31),
        ("日本語", "agent_01ABCdef", "01abcdef"),
        ("!!!", "agent_01XYZ789", "01xyz789"),
        ("", "ag", "ag"),
        ("", "__", "agent"),
    ],
)
def test_the_server_slug_is_short_lowercase_and_plain(
    name: str, ma_agent_id: str, expected: str
) -> None:
    slug = server_slug(name, ma_agent_id)

    assert slug == expected, "the name cleaned and cut, else the id's tail, else a fixed word"
    assert re.fullmatch(r"[a-z0-9-]{1,32}", slug), "only a-z, 0-9 and dashes, 32 at most"


@pytest.mark.parametrize("client", CODING_CLIENTS)
def test_a_slug_is_valid_in_every_format(client: str) -> None:
    prompt = _prompt(client, slug=server_slug('my "agent"', "agent_01"))

    assert "daimon-my-agent" in prompt, "the slug names the server"
    assert '\\"' not in prompt, "nothing in the entry needs escaping"
