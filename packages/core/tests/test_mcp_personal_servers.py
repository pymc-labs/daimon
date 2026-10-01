"""Unit tests for the personal-MCP-server classification and list surgery.

Pure module, so no DB and no fake MA: grants in, server names out, and the
two agent arrays cut in step. The DB read that feeds it lives in
`agent_mcp_credentials` and is covered by `test_agent_mcp_credentials.py`.
"""

from __future__ import annotations

import uuid

from daimon.core.mcp_personal_servers import (
    hidden_mcp_server_names,
    visible_mcp_servers,
    visible_tools,
)
from daimon.core.stores.domain import McpOAuthGrantRow
from daimon.testing.ma_models import ma_agent

_CONNECTED = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
_OTHER = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
_AGENT = uuid.UUID("00000000-0000-0000-0000-000000000a01")
_FORK = uuid.UUID("00000000-0000-0000-0000-000000000a02")
_SERVER_URL = "https://mcp.example.com/docs"
_SERVERS = {"docs": _SERVER_URL}


def _toolset(server_name: str) -> dict[str, object]:
    """One `mcp_toolset` entry in the shape MA returns it on an agent."""
    return {
        "type": "mcp_toolset",
        "mcp_server_name": server_name,
        "configs": [],
        "default_config": {
            "enabled": True,
            "permission_policy": {"type": "always_allow"},
        },
    }


def test_a_server_only_someone_else_connected_is_hidden() -> None:
    grants = (McpOAuthGrantRow(agent_id=_AGENT, account_id=_CONNECTED, mcp_server_url=_SERVER_URL),)
    assert hidden_mcp_server_names(
        grants, agent_id=_AGENT, account_id=_OTHER, shared_server_urls=(), server_urls=_SERVERS
    ) == frozenset({"docs"}), "a caller with no grant of their own cannot authenticate the server"
    assert (
        hidden_mcp_server_names(
            grants,
            agent_id=_AGENT,
            account_id=_CONNECTED,
            shared_server_urls=(),
            server_urls=_SERVERS,
        )
        == frozenset()
    ), "the person who signed in keeps the server they connected"


def test_nothing_is_hidden_when_nobody_has_signed_in() -> None:
    assert (
        hidden_mcp_server_names(
            (), agent_id=_AGENT, account_id=_OTHER, shared_server_urls=(), server_urls=_SERVERS
        )
        == frozenset()
    ), "an agent with no OAuth grants hides nothing"


def test_a_grant_for_a_url_the_server_no_longer_uses_is_not_a_connection() -> None:
    """The name is re-pointed at another server: the stale grant unlocks
    nothing, so its holder is a bystander like everyone else."""
    grants = (
        McpOAuthGrantRow(
            agent_id=_AGENT,
            account_id=_CONNECTED,
            mcp_server_url="https://mcp.example.com/old-docs",
        ),
        McpOAuthGrantRow(agent_id=_AGENT, account_id=_OTHER, mcp_server_url=_SERVER_URL),
    )
    assert hidden_mcp_server_names(
        grants, agent_id=_AGENT, account_id=_CONNECTED, shared_server_urls=(), server_urls=_SERVERS
    ) == frozenset({"docs"}), "a grant for the old URL is not a connection to the new server"


def test_a_fork_hides_a_copied_sign_in_server_from_everyone_until_someone_signs_in_there() -> None:
    """A fork copies the source's servers and none of its grants: MA would
    authenticate from the (person, fork) vault, which nobody has filled, so
    even the person who signed in on the source is a bystander on the fork."""
    grants = (McpOAuthGrantRow(agent_id=_AGENT, account_id=_CONNECTED, mcp_server_url=_SERVER_URL),)
    for account in (_CONNECTED, _OTHER):
        assert hidden_mcp_server_names(
            grants, agent_id=_FORK, account_id=account, shared_server_urls=(), server_urls=_SERVERS
        ) == frozenset({"docs"}), "a grant on the source agent unlocks nothing on the fork"
    fork_grants = (
        *grants,
        McpOAuthGrantRow(agent_id=_FORK, account_id=_CONNECTED, mcp_server_url=_SERVER_URL),
    )
    assert (
        hidden_mcp_server_names(
            fork_grants,
            agent_id=_FORK,
            account_id=_CONNECTED,
            shared_server_urls=(),
            server_urls=_SERVERS,
        )
        == frozenset()
    ), "signing in on the fork itself is what unlocks it there"


def test_a_server_with_an_agent_wide_credential_is_never_hidden() -> None:
    grants = (
        McpOAuthGrantRow(
            agent_id=_AGENT,
            account_id=_CONNECTED,
            mcp_server_url=_SERVER_URL + "/",
        ),
    )
    assert (
        hidden_mcp_server_names(
            grants,
            agent_id=_AGENT,
            account_id=_OTHER,
            shared_server_urls=(_SERVER_URL,),
            server_urls={"docs": _SERVER_URL + "/"},
        )
        == frozenset()
    ), "a token stored on the agent is mirrored into every caller's vault, trailing slash or not"


def test_visible_lists_drop_a_hidden_server_and_its_toolset_together() -> None:
    agent = ma_agent(
        mcp_servers=[
            {"name": "docs", "type": "url", "url": "https://mcp.example.com/docs"},
            {"name": "daimon-mcp", "type": "url", "url": "https://daimon/mcp"},
        ],
        tools=[
            _toolset("docs"),
            _toolset("daimon-mcp"),
        ],
    )

    servers = visible_mcp_servers(agent, frozenset({"docs"}))
    tools = visible_tools(agent, frozenset({"docs"}))

    assert [server.name for server in servers] == ["daimon-mcp"], "the hidden server is gone"
    assert len(tools) == 1, "its toolset goes with it — MA rejects one without the other"
    assert not any(getattr(tool, "mcp_server_name", None) == "docs" for tool in tools), (
        "no toolset may reference a server this session does not mount"
    )


def test_visible_lists_are_the_agent_lists_when_nothing_is_hidden() -> None:
    agent = ma_agent(
        mcp_servers=[{"name": "daimon-mcp", "type": "url", "url": "https://daimon/mcp"}],
        tools=[_toolset("daimon-mcp")],
    )

    assert list(visible_mcp_servers(agent, frozenset())) == list(agent.mcp_servers), (
        "an empty hidden set must not reshape the agent's servers"
    )
    assert list(visible_tools(agent, frozenset())) == list(agent.tools), (
        "an empty hidden set must not reshape the agent's tools"
    )
