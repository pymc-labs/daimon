"""Vault tool: list_credentials — safe projection of caller's MCP vault credentials."""

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
    display_name = f"daimon-mcp:{auth.account_id}"
    matching = [v async for v in client.beta.vaults.list() if v.display_name == display_name]
    if not matching:
        raise ToolError(
            "no MCP vault found for this account — run a session first to bootstrap the vault"
        )
    vault_id = min(matching, key=lambda v: v.created_at).id
    creds = [c async for c in client.beta.vaults.credentials.list(vault_id=vault_id)]
    return [VaultCredentialSummary.model_validate(c.model_dump(mode="json")) for c in creds]


def register_vault_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def list_credentials(ctx: Context) -> list[VaultCredentialSummary]:  # pyright: ignore[reportUnusedFunction]
        """List credentials in the caller's MCP vault (safe projection — no secrets)."""
        return await _list_credentials_impl(runtime.client, await _auth(ctx))
