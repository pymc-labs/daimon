"""Connect an MCP server with a pasted token, publishing the token last.

The pasted token becomes the agent-wide credential that every caller's
session mirrors into its own vault. Once another session has mirrored it, it
cannot be taken back, so it must never be visible before the connection is
authorized. All of it runs in one transaction holding the per-agent MCP lock
(`agent_mcp_write_lock`), which every writer of the agent's MCP servers and
every agent-wide token write also holds:

1. attach the server with a fresh-agent re-check (`attach_mcp_server_to_agent`
   refuses a repoint, or a server already at this URL, unless replacement was
   authorized);
2. re-read the agent: this server must be at the URL, and no other server may
   be, unless replacement was authorized;
3. publish the agent-wide token, refusing if one for the URL exists (unless
   replacement was authorized); it becomes visible only at commit;
4. after commit, write the submitter's own vault copy.

A refusal in 1-3 rolls back and publishes nothing. A brand-new server attached
in 1 stays declared without a token, which only makes its calls fail.
"""

from __future__ import annotations

import datetime as dt
import uuid

from anthropic import AsyncAnthropic
from cryptography.fernet import MultiFernet
from daimon.core.agent_mcp_credentials import agent_mcp_write_lock, store_agent_mcp_token
from daimon.core.defaults.ma_index import find_agent_by_derived_uuid
from daimon.core.errors import DaimonError
from daimon.core.mcp_attach import McpServerReplaceRefusedError, attach_mcp_server_to_agent
from daimon.core.mcp_server_url import same_mcp_url
from daimon.core.mcp_vault import add_external_mcp_credential
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import retrieve_agent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class McpAgentGoneError(DaimonError):
    """The agent the request targets no longer exists; nothing was stored."""


class McpAttachFailedError(DaimonError):
    """The attach failed for a reason other than a refusal; nothing was stored."""


class McpTokenWriteFailedError(DaimonError):
    """The server is attached, but storing the token did not finish."""


async def connect_mcp_server_with_token(
    client: AsyncAnthropic,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    fernet: MultiFernet | None,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    account_id: uuid.UUID,
    server_name: str,
    mcp_server_url: str,
    token: str,
    replace_allowed: bool,
    jwt_secret: bytes,
    public_url: str,
    now: dt.datetime,
) -> None:
    """Attach, then publish the agent-wide token, then the submitter's vault copy.

    Raises `McpServerReplaceRefusedError` (nothing published),
    `McpAgentGoneError` / `McpAttachFailedError` (nothing stored) or
    `McpTokenWriteFailedError` (attached; the token may be partly stored).
    """
    scope = resource_scope(
        tenant_id=str(tenant_id),
        account_id=str(account_id),
    )
    agent = await find_agent_by_derived_uuid(client, tenant_id=tenant_id, agent_id=agent_id)
    if agent is None:
        raise McpAgentGoneError("The agent this request was for no longer exists.")
    try:
        async with agent_mcp_write_lock(
            sessionmaker, tenant_id=tenant_id, agent_id=agent_id
        ) as session:
            try:
                await attach_mcp_server_to_agent(
                    client,
                    agent.id,
                    server_name=server_name,
                    url=mcp_server_url,
                    replace_allowed=replace_allowed,
                    shares_token=True,
                    scope=scope,
                )
            except McpServerReplaceRefusedError:
                raise
            except Exception as err:
                raise McpAttachFailedError(type(err).__name__) from err
            fresh = await retrieve_agent(client, agent.id, scope=scope)
            at_url = [s for s in fresh.mcp_servers or [] if same_mcp_url(s.url, mcp_server_url)]
            if not any(s.name == server_name for s in at_url) or (
                not replace_allowed and any(s.name != server_name for s in at_url)
            ):
                raise McpServerReplaceRefusedError(server_name=server_name)
            if fernet is not None:
                await store_agent_mcp_token(
                    session,
                    fernet=fernet,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    mcp_server_url=mcp_server_url,
                    plaintext_token=token,
                    replace_allowed=replace_allowed,
                )
    except (McpServerReplaceRefusedError, McpAttachFailedError):
        raise
    except Exception as err:
        raise McpTokenWriteFailedError(type(err).__name__) from err
    try:
        await add_external_mcp_credential(
            client,
            account_id=account_id,
            agent_id=agent_id,
            jwt_secret=jwt_secret,
            public_url=public_url,
            mcp_server_url=mcp_server_url,
            token=token,
            now=now,
            session_factory=sessionmaker,
            scope=resource_scope(tenant_id=str(tenant_id), account_id=str(account_id)),
        )
    except Exception as err:
        raise McpTokenWriteFailedError(type(err).__name__) from err
