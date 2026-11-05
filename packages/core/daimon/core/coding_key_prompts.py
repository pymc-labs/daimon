"""The prompt a person pastes into a coding agent to connect it to one Daimon agent.

One template per coding tool, each naming where that tool keeps its MCP servers
and the shape of an entry there. The prompt never holds the key: it holds a
single-use code, and tells the agent to trade it for the key with one shell
command that writes the key into the file, so the key never enters the chat.
"""

from __future__ import annotations

import dataclasses
import json
import re
from typing import Final

CODING_CLIENTS: Final = ("claude-code", "codex", "cursor", "pi", "mcp")
"""The tools a prompt exists for, in the order the panel shows them."""

#: How long a code works, for the prompt's own wording; the claim enforces it.
CODE_TTL_MINUTES: Final = 10

#: Longest the agent's part of a server key gets.
_SLUG_MAX = 32


@dataclasses.dataclass(frozen=True)
class _Client:
    name: str
    where: str
    after: str


_CLIENTS: Final[dict[str, _Client]] = {
    "claude-code": _Client(
        "Claude Code", 'the top-level "mcpServers" object in ~/.claude.json', "restart Claude Code"
    ),
    "codex": _Client(
        "Codex",
        "~/.codex/config.toml, replacing any existing table for this server",
        "restart Codex",
    ),
    "cursor": _Client("Cursor", 'the "mcpServers" object in ~/.cursor/mcp.json', "reload Cursor"),
    "pi": _Client("Pi", 'the "mcpServers" object in ~/.pi/agent/mcp.json', "run /reload"),
    "mcp": _Client(
        "Other MCP", "your MCP config, as a streamable HTTP server", "reload your MCP servers"
    ),
}

#: What the entry holds where the key goes; the agent swaps it for the real key.
KEY_PLACEHOLDER: Final = "<key>"


def client_name(client: str) -> str:
    """The tool's name as a person reads it."""
    return _CLIENTS[client].name


def _entry(client: str, *, server_name: str, url: str) -> str:
    auth = f"Bearer {KEY_PLACEHOLDER}"
    name = json.dumps(server_name)
    headers = f'"headers": {{"Authorization": "{auth}"}}'
    if client == "claude-code":
        return f'{name}: {{"type": "http", "url": {json.dumps(url)}, {headers}}}'
    if client == "cursor":
        return f'{name}: {{"url": {json.dumps(url)}, {headers}}}'
    if client == "pi":
        return f'{name}: {{"url": {json.dumps(url)}, {headers}, "exposure": "direct"}}'
    if client == "codex":
        return (
            f"[mcp_servers.{server_name}]\nurl = {json.dumps(url)}\n"
            f'http_headers = {{ Authorization = "{auth}" }}'
        )
    return f"name: {server_name}\nurl: {url}\nheader: Authorization: {auth}"


def server_slug(agent_name: str, ma_agent_id: str) -> str:
    """The agent's part of its server key: lowercase letters, digits and dashes, at most 32.

    An agent's name is free text, so it never reaches a prompt as written: only
    this slug does. A name with nothing usable in it falls back to the tail of
    the agent's id.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", agent_name.lower())[:_SLUG_MAX].strip("-")
    return slug or re.sub(r"[^a-z0-9]", "", ma_agent_id.lower())[-8:] or "agent"


def connect_prompt(client: str, *, slug: str, public_url: str, claim_url: str, code: str) -> str:
    """The prompt for one tool: add this entry, get the key with one command, never show it.

    `slug` is `server_slug`'s, the only thing of the agent the prompt carries.
    """
    spec = _CLIENTS[client]
    entry = _entry(client, server_name=f"daimon-{slug}", url=public_url)
    return (
        "Connect yourself to a Daimon agent as a remote MCP server. "
        f"Add this to {spec.where}, keeping everything else:\n\n{entry}\n\n"
        f"Get {KEY_PLACEHOLDER} with ONE shell command that runs "
        f"`curl -fsS -X POST {claim_url} -d code={code}` (the response body is the bare key) "
        "and writes it straight into the file, so it never appears in this chat. "
        "Never print, read back or echo it. If the command exits non-zero or prints nothing, "
        "stop and tell me what it said; do not retry or make up a key.\n\n"
        f"Then tell me to {spec.after}. The code works once, "
        f"within {CODE_TTL_MINUTES} minutes."
    )
