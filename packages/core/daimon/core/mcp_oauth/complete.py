"""Finish an MCP OAuth handshake: exchange the code, store the grant, attach.

Runs on the mcp process when the authorization server redirects back. The
flow row is already spent by the caller (the atomic consume is the replay
gate), so everything here is the write side: tokens from the code, the
`mcp_oauth` credential into the requester's per-agent vault, the completion
stamp that marks this person — and only this person — as connected to the
server, and the server on the agent so the toolset exists. Failures raise; the route decides what
the browser sees.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import httpx
from anthropic import AsyncAnthropic
from cryptography.fernet import MultiFernet
from daimon.core.defaults.ma_index import find_agent_by_derived_uuid
from daimon.core.errors import DaimonError
from daimon.core.github_credentials import decrypt_token
from daimon.core.mcp_attach import attach_mcp_server_to_agent
from daimon.core.mcp_oauth.flow import exchange_authorization_code
from daimon.core.mcp_oauth.models import ClientRegistration, TokenEndpointAuthMethod
from daimon.core.mcp_oauth.vault import put_mcp_oauth_credential
from daimon.core.mcp_vault import ensure_agent_mcp_vault, hold_agent_vault_lock
from daimon.core.stores import mcp_oauth_flows as flows_store
from daimon.core.stores.domain import McpOAuthFlowRow
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class McpOAuthIncompleteFlowError(DaimonError):
    """The callback arrived for a flow that never registered a client."""


@dataclass(frozen=True, slots=True)
class McpOAuthCompletion:
    vault_id: str
    credential_id: str
    ma_agent_id: str | None
    """None when the agent no longer exists; the grant is stored regardless."""


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
) -> McpOAuthCompletion:
    """Exchange, store in the requester's vault, attach the server to the agent."""
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
    vault_id = await ensure_agent_mcp_vault(
        anthropic,
        account_id=flow.account_id,
        agent_id=flow.agent_id,
        jwt_secret=jwt_secret,
        public_url=public_url,
        now=now,
        session_factory=session_factory,
    )
    # Locked like the mirror, the Copilot PAT and the pasted-token writers
    # (see hold_agent_vault_lock for the two that are not), so a turn
    # mirroring the agent's shared token cannot recreate it between the
    # delete and the create.
    async with hold_agent_vault_lock(
        session_factory, account_id=flow.account_id, agent_id=flow.agent_id
    ):
        credential_id = await put_mcp_oauth_credential(
            anthropic,
            vault_id=vault_id,
            mcp_server_url=flow.mcp_server_url,
            tokens=tokens,
            client=client,
            token_endpoint=flow.token_endpoint,
            resource=flow.resource,
            now=now,
        )
    # The grant is in this person's vault now, which is what makes them
    # connected: their sessions mount the server, nobody else's do. Stamped
    # before the attach, since a grant outlives an agent that has gone away.
    async with session_factory() as session, session.begin():
        await flows_store.mark_flow_completed(session, state=flow.state, now=now)
    agent = await find_agent_by_derived_uuid(
        anthropic, tenant_id=flow.tenant_id, agent_id=flow.agent_id
    )
    if agent is None:
        return McpOAuthCompletion(vault_id=vault_id, credential_id=credential_id, ma_agent_id=None)
    attached = await attach_mcp_server_to_agent(
        anthropic, agent.id, server_name=flow.server_name, url=flow.mcp_server_url
    )
    return McpOAuthCompletion(
        vault_id=vault_id, credential_id=credential_id, ma_agent_id=attached.id
    )


__all__ = [
    "McpOAuthCompletion",
    "McpOAuthIncompleteFlowError",
    "complete_mcp_oauth_flow",
    "registered_client",
]
