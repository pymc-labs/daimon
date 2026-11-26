"""Finish an MCP OAuth handshake: exchange the code, store the grant, attach.

Runs on the mcp process when the authorization server redirects back. The
flow row is already spent by the caller (the atomic consume is the replay
gate), so everything here is the write side: tokens from the code, the
grant Daimon holds and refreshes (`connector_tokens`), the completion
stamp that marks this person — and only this person — as connected to the
server, and the server on the agent so the toolset exists. Failures raise; the route decides what
the browser sees.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from anthropic import AsyncAnthropic
from cryptography.fernet import MultiFernet
from daimon.core.agent_mcp_credentials import agent_mcp_write_lock
from daimon.core.agent_ownership import owns_agent
from daimon.core.defaults.ma_index import find_agent_by_derived_uuid
from daimon.core.errors import DaimonError
from daimon.core.github_credentials import decrypt_token
from daimon.core.mcp_attach import (
    McpServerReplaceRefusedError,
    attach_mcp_server_to_agent,
    decide_mcp_connect,
)
from daimon.core.mcp_oauth.connector_tokens import store_connector_grant
from daimon.core.mcp_oauth.flow import exchange_authorization_code, refresh_scope
from daimon.core.mcp_oauth.models import ClientRegistration, TokenEndpointAuthMethod
from daimon.core.mux_backend import resource_scope
from daimon.core.native_admins import AdminCaller
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import connector_tokens as connector_tokens_store
from daimon.core.stores import mcp_oauth_flows as flows_store
from daimon.core.stores.accounts import get_account_with_tenant
from daimon.core.stores.domain import McpOAuthFlowRow, Role
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class McpOAuthWriteRefusedError(DaimonError):
    """The access decision refused the sign-in after the code exchange; nothing is left."""


class McpOAuthIncompleteFlowError(DaimonError):
    """The callback arrived for a flow that never registered a client."""


@dataclass(frozen=True, slots=True)
class McpOAuthCompletion:
    ma_agent_id: str | None
    """None when the agent no longer exists; the grant is stored regardless."""
    connector_token_id: uuid.UUID


def registered_client(flow: McpOAuthFlowRow, *, fernet: MultiFernet) -> ClientRegistration:
    """The client `/oauth/mcp/start` registered, secret decrypted."""
    if flow.client_id is None or flow.token_endpoint is None:
        raise McpOAuthIncompleteFlowError(
            f"flow {flow.state[:8]}… reached the callback without a registered client"
        )
    method: TokenEndpointAuthMethod = "none"
    if flow.token_endpoint_auth_method in ("client_secret_basic", "client_secret_post"):
        method = flow.token_endpoint_auth_method
    return ClientRegistration(
        client_id=flow.client_id,
        client_secret=(
            decrypt_token(fernet, flow.client_secret_encrypted.encode())
            if flow.client_secret_encrypted is not None
            else None
        ),
        token_endpoint_auth_method=method,
    )


async def complete_mcp_oauth_flow(
    http: httpx.AsyncClient,
    anthropic: AsyncAnthropic,
    *,
    flow: McpOAuthFlowRow,
    code: str,
    fernet: MultiFernet,
    jwt_secret: bytes,
    public_url: str,
    now: dt.datetime,
    session_factory: async_sessionmaker[AsyncSession],
    default: DeploymentDefault,
    may_write: Callable[[], Awaitable[bool]] | None = None,
    configured_client: bool = False,
) -> McpOAuthCompletion:
    """Exchange, store the requester's grant, attach the server to the agent.

    The grant goes to `connector_tokens` on every backend: Daimon is its one
    refresh owner, and each Managed Agents turn mirrors the fresh access token
    into the caller's vault (`agent_mcp_credentials.mirror_stored_credentials`),
    so a backend switch keeps it working.

    ``may_write`` is the caller's access decision. It is asked after the code
    exchange, again inside the grant's transaction, and again inside the
    agent's MCP lock immediately before the attach: each step before it awaits
    the network or a lock, and a pin landing in any of those waits must stop
    the grant and the attach. A refusal raises `McpOAuthWriteRefusedError`; no
    grant is left and nothing is attached.

    The grant is personal, but the attach is not: repointing a server name the
    agent already declares at another URL redirects every caller. That is
    re-decided here as `mcp_replace` against the requester as stored (the
    browser callback carries no live platform identity): stored admin role. A
    refused replacement raises `McpServerReplaceRefusedError` with the agent
    unchanged.

    ``configured_client`` marks a flow signed in with a client from
    `DAIMON_MCP_OAUTH__CLIENTS`; only then is the `offline_access` its sign-in
    asked for kept in the refresh scope, so a dynamically registered client's
    stored grant is exactly what the server returned.
    """
    client = registered_client(flow, fernet=fernet)
    assert flow.token_endpoint is not None  # narrowed by registered_client
    tokens = await exchange_authorization_code(
        http,
        token_endpoint=flow.token_endpoint,
        code=code,
        code_verifier=flow.code_verifier,
        client=client,
        redirect_uri=flow.redirect_uri,
        resource=flow.resource,
    )
    if configured_client:
        tokens = tokens.model_copy(update={"scope": refresh_scope(tokens.scope, flow.scope)})

    async def still_allowed() -> None:
        if may_write is not None and not await may_write():
            raise McpOAuthWriteRefusedError

    grant_scope = resource_scope(tenant_id=str(flow.tenant_id), account_id=str(flow.account_id))
    await still_allowed()
    async with session_factory() as session, session.begin():
        await still_allowed()
        row = await store_connector_grant(
            session, flow=flow, tokens=tokens, client=client, fernet=fernet, now=now
        )

    async def withdraw() -> None:
        async with session_factory() as session, session.begin():
            await connector_tokens_store.delete_connector_token(
                session, tenant_id=row.tenant_id, token_id=row.id, read_at=row.updated_at
            )

    # Do not advertise a completed grant until the spec mutation is authorized:
    # grant records also determine which servers other people's sessions hide.
    agent = await find_agent_by_derived_uuid(
        anthropic, tenant_id=flow.tenant_id, agent_id=flow.agent_id, records=session_factory
    )
    if agent is None:
        async with session_factory() as session, session.begin():
            await flows_store.mark_flow_completed(session, state=flow.state, now=now)
        return McpOAuthCompletion(ma_agent_id=None, connector_token_id=row.id)
    async with session_factory() as session:
        requester = await get_account_with_tenant(session, account_id=flow.account_id)
        # The person the grant belongs to; their ownership is read, never claimed.
        owned = requester is not None and await owns_agent(
            session,
            tenant_id=flow.tenant_id,
            ma_agent_id=str(agent.id),
            account_id=flow.account_id,
        )
    caller = AdminCaller(
        platform_user_id=requester.platform_user_id if requester is not None else None,
        is_server_admin=requester is not None and requester.role is Role.ADMIN,
    )
    # Decide and attach under the per-agent MCP lock, serialized with the
    # token forms' attach-then-publish and the direct tools.
    async with agent_mcp_write_lock(
        session_factory, tenant_id=flow.tenant_id, agent_id=flow.agent_id
    ):
        if may_write is not None and not await may_write():
            await withdraw()
            raise McpOAuthWriteRefusedError
        decision = await decide_mcp_connect(
            session_factory,
            tenant_id=flow.tenant_id,
            agent=agent,
            agent_id=flow.agent_id,
            server_name=flow.server_name,
            url=flow.mcp_server_url,
            platform=requester.platform if requester is not None else "",
            caller=caller,
            default=default,
            shares_token=False,
            owned=owned,
        )
        if decision.refused:
            await withdraw()
            if decision.replaces:
                raise McpServerReplaceRefusedError(server_name=flow.server_name)
            raise McpOAuthWriteRefusedError
        attached = agent
        if decision.attaches:
            try:
                attached = await attach_mcp_server_to_agent(
                    anthropic,
                    agent.id,
                    server_name=flow.server_name,
                    url=flow.mcp_server_url,
                    replace_allowed=decision.replace_allowed,
                    before_update=still_allowed,
                    scope=grant_scope,
                    records=session_factory,
                )
            except (McpOAuthWriteRefusedError, McpServerReplaceRefusedError):
                await withdraw()
                raise
    async with session_factory() as session, session.begin():
        await flows_store.mark_flow_completed(session, state=flow.state, now=now)
    return McpOAuthCompletion(ma_agent_id=attached.id, connector_token_id=row.id)


__all__ = [
    "McpOAuthCompletion",
    "McpOAuthIncompleteFlowError",
    "McpOAuthWriteRefusedError",
    "complete_mcp_oauth_flow",
    "registered_client",
]
