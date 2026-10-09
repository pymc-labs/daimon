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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import anthropic as anthropic_pkg
import httpx
import structlog
from anthropic import AsyncAnthropic
from cryptography.fernet import MultiFernet
from daimon.core.agent_mcp_credentials import agent_mcp_write_lock
from daimon.core.channel_admins import (
    CHANNEL_ADMIN_PLATFORMS,
    ChannelAdminCaller,
    GroupMembersFor,
    confirm_stored_group_ids,
    grant_group_ids,
)
from daimon.core.defaults.ma_index import find_agent_by_derived_uuid
from daimon.core.errors import DaimonError
from daimon.core.github_credentials import decrypt_token
from daimon.core.mcp_attach import attach_mcp_server_to_agent, decide_mcp_connect
from daimon.core.mcp_oauth.flow import exchange_authorization_code
from daimon.core.mcp_oauth.models import ClientRegistration, TokenEndpointAuthMethod
from daimon.core.mcp_oauth.vault import put_mcp_oauth_credential
from daimon.core.mcp_vault import ensure_agent_mcp_vault, hold_agent_vault_lock
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import delete_credential
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import mcp_oauth_flows as flows_store
from daimon.core.stores.accounts import get_account_with_tenant
from daimon.core.stores.channel_admins import list_channel_admins
from daimon.core.stores.domain import McpOAuthFlowRow, Role
from mux.contracts.ids import Scope
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_log = structlog.get_logger(__name__)


class McpOAuthWriteRefusedError(DaimonError):
    """The access decision refused the sign-in after the code exchange; nothing is left."""


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
    default: DeploymentDefault,
    may_write: Callable[[], Awaitable[bool]] | None = None,
    group_members: GroupMembersFor | None = None,
) -> McpOAuthCompletion:
    """Exchange, store in the requester's vault, attach the server to the agent.

    ``may_write`` is the caller's access decision. It is asked before the
    first vault call (after the code exchange), again inside the vault lock
    immediately before the grant is written, and again inside the agent's MCP
    lock immediately before the attach: each step before it awaits the
    network or a lock, and a pin landing in any of those waits must stop the
    grant and the attach. A refusal raises `McpOAuthWriteRefusedError`; no
    grant is written and nothing is attached (at most the person's own empty
    vault exists, as it would after a declined sign-in).

    The grant is personal, but the attach is not: repointing a server name the
    agent already declares at another URL redirects every caller. That is
    re-decided here as `mcp_replace` against the requester as stored (the
    browser callback carries no live platform identity): role, platform id and
    role ids, so a channel admin passes or fails here as at the request (a
    stored Slack group or Teams team only as `group_members` confirms it). A
    refused replacement raises `McpServerReplaceRefusedError` with the agent
    unchanged.
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

    async def still_allowed() -> None:
        if may_write is not None and not await may_write():
            raise McpOAuthWriteRefusedError

    await still_allowed()
    vault_id = await ensure_agent_mcp_vault(
        anthropic,
        account_id=flow.account_id,
        agent_id=flow.agent_id,
        jwt_secret=jwt_secret,
        public_url=public_url,
        now=now,
        session_factory=session_factory,
        scope=resource_scope(tenant_id=str(flow.tenant_id), account_id=str(flow.account_id)),
    )
    # Locked like the mirror, the Copilot PAT and the pasted-token writers
    # (see hold_agent_vault_lock for the two that are not), so a turn
    # mirroring the agent's shared token cannot recreate it between the
    # delete and the create.
    async with hold_agent_vault_lock(
        session_factory, account_id=flow.account_id, agent_id=flow.agent_id
    ):
        await still_allowed()
        credential_id = await put_mcp_oauth_credential(
            anthropic,
            vault_id=vault_id,
            mcp_server_url=flow.mcp_server_url,
            tokens=tokens,
            client=client,
            token_endpoint=flow.token_endpoint,
            resource=flow.resource,
            now=now,
            before_write=still_allowed,
            scope=resource_scope(tenant_id=str(flow.tenant_id), account_id=str(flow.account_id)),
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
    async with session_factory() as session:
        requester = await get_account_with_tenant(session, account_id=flow.account_id)
        grants = (
            await list_channel_admins(
                session, tenant_id=requester.tenant_id, platform=requester.platform
            )
            if requester is not None and requester.platform in CHANNEL_ADMIN_PLATFORMS
            else []
        )
    caller = (
        ChannelAdminCaller(platform_user_id=None)
        if requester is None
        else ChannelAdminCaller(
            platform_user_id=requester.platform_user_id,
            role_ids=await confirm_stored_group_ids(
                requester.platform,
                requester.platform_user_id,
                requester.platform_role_ids,
                group_members(requester.platform, requester.external_id) if group_members else None,
                named=grant_group_ids(grants),
            ),
            is_server_admin=requester.role is Role.ADMIN,
        )
    )
    # Decide and attach under the per-agent MCP lock, serialized with the
    # token forms' attach-then-publish and the direct tools.
    async with agent_mcp_write_lock(
        session_factory, tenant_id=flow.tenant_id, agent_id=flow.agent_id
    ):
        if may_write is not None and not await may_write():
            await _withdraw_grant(
                anthropic,
                credential_id=credential_id,
                vault_id=vault_id,
                scope=resource_scope(
                    tenant_id=str(flow.tenant_id), account_id=str(flow.account_id)
                ),
            )
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
        )
        try:
            attached = await attach_mcp_server_to_agent(
                anthropic,
                agent.id,
                server_name=flow.server_name,
                url=flow.mcp_server_url,
                replace_allowed=decision.replace_allowed,
                before_update=still_allowed,
            )
        except McpOAuthWriteRefusedError:
            await _withdraw_grant(
                anthropic,
                credential_id=credential_id,
                vault_id=vault_id,
                scope=resource_scope(
                    tenant_id=str(flow.tenant_id), account_id=str(flow.account_id)
                ),
            )
            raise
    return McpOAuthCompletion(
        vault_id=vault_id, credential_id=credential_id, ma_agent_id=attached.id
    )


async def _withdraw_grant(
    anthropic: AsyncAnthropic,
    *,
    credential_id: str,
    vault_id: str,
    scope: Scope | None = None,
) -> None:
    """Remove a grant written before the sign-in was refused; retry once, log a failure.

    A pin landed after the grant was written, so a refused sign-in must leave
    no grant behind as well as no attach. A failed delete is logged by id
    (never the credential) so an operator can remove it.
    """
    scope = scope or Scope.legacy_host_authorized(
        call_site="daimon.core.mcp_oauth.complete:_withdraw_grant"
    )
    for attempt in (1, 2):
        try:
            await delete_credential(anthropic, vault_id, credential_id, scope=scope)
            return
        except anthropic_pkg.NotFoundError:
            return
        except anthropic_pkg.APIError as exc:
            if attempt == 2:
                _log.error(
                    "mcp_oauth.refused_grant_withdraw_failed",
                    credential_id=credential_id,
                    vault_id=vault_id,
                    error_type=type(exc).__name__,
                )


__all__ = [
    "McpOAuthCompletion",
    "McpOAuthIncompleteFlowError",
    "McpOAuthWriteRefusedError",
    "complete_mcp_oauth_flow",
    "registered_client",
]
