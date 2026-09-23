"""Vault tool: list_credentials — safe projection of caller's MCP vault credentials.

Tagged ``agent-chat``, so it is visible only to a session whose token
carries an agent identity — the same identity its ``agent_id`` guard needs
to resolve the caller's per-agent vault. An ordinary chat session never
discovers this tool.
"""

from __future__ import annotations

from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict


class VaultCredentialAuthSummary(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    type: str
    mcp_server_url: str | None = None


class VaultCredentialSummary(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str
    vault_id: str
    type: str
    mcp_server_url: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    archived_at: str | None = None
    metadata: dict[str, str] | None = None
    auth: VaultCredentialAuthSummary | None = None


async def _list_credentials_impl(
    client: AsyncAnthropic,
    auth: AuthIdentity,
) -> list[VaultCredentialSummary]:
    if auth.agent_id is None:
        raise ToolError("agent_id missing — token was not minted for an agent session")
    display_name = f"daimon-mcp:{auth.account_id}:{auth.agent_id}"
    matching = [v async for v in client.beta.vaults.list() if v.display_name == display_name]
    if not matching:
        raise ToolError(
            "no MCP vault found for this agent — run a session first to bootstrap the vault"
        )
    vault_id = min(matching, key=lambda v: v.created_at).id
    creds = [c async for c in client.beta.vaults.credentials.list(vault_id=vault_id)]
    return [VaultCredentialSummary.model_validate(c.model_dump(mode="json")) for c in creds]


def register_vault_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool(tags={"agent-chat"})  # pyright: ignore[reportArgumentType]
    async def list_credentials(ctx: Context) -> list[VaultCredentialSummary]:  # pyright: ignore[reportUnusedFunction]
        """List MCP connection credential metadata for the calling agent and account.

        Returns no secret values. This only inspects the caller's MCP vault, not
        another named agent or stored environment/API keys. For "what keys does
        <agent> have?", use ``list_agent_keys(agent_name=...)``. An empty MCP vault
        does not mean the agent has no API keys.
        """
        return await _list_credentials_impl(runtime.client, await _auth(ctx))
