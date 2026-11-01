"""A person's access token at a node's server, by token exchange (RFC 8693).

The web app signs who the person's chat account is and trades that at the server for
a token, kept encrypted per person and server and exchanged again when nearly spent.
Only chat accounts of a workspace the server serves get one. A refusal is remembered
for a while, so the server is not asked on every call.
"""

from __future__ import annotations

import ssl
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Literal

import httpx
import structlog
from cryptography.fernet import InvalidToken
from daimon.adapters.web.caller import WebCaller
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.config import SignedIdentityServer
from daimon.core.errors import DaimonError
from daimon.core.github_credentials import decrypt_token, encrypt_token
from daimon.core.stores import tenants as tenants_store
from daimon.core.stores import web_node_tokens as tokens_store
from daimon.core.stores.domain import CHAT_PLATFORMS, TenantRow
from daimon.core.token_exchange import (
    ExchangeClient,
    TokenExchangeError,
    exchange_subject_token,
    sign_subject_token,
)
from daimon.core.web_identity import linked_identity

log = structlog.get_logger(__name__)

#: A token with less left than this is exchanged again before use.
MIN_TOKEN_LEFT = timedelta(seconds=60)
#: A refused exchange is not tried again for this long.
EXCHANGE_BACKOFF = timedelta(minutes=5)
#: How long a token is taken to last when the server does not say.
DEFAULT_TOKEN_LIFE = timedelta(minutes=5)
#: Every request to a node's server gives up after this long.
NODE_TIMEOUT_S = 10.0
_MAX_REFUSALS = 1024

NodeAccessProblem = Literal["no_account", "not_served", "refused", "unconfigured", "unreachable"]


class NodeAccessError(DaimonError):
    """No token for this person at this server."""

    def __init__(self, problem: NodeAccessProblem) -> None:
        super().__init__(problem)
        self.problem: NodeAccessProblem = problem


def tenant_workspace(tenant: TenantRow | None) -> str | None:
    """`<platform>:<workspace id>` of a chat workspace; None for any other."""
    if tenant is None or tenant.platform not in CHAT_PLATFORMS:
        return None
    return f"{tenant.platform}:{tenant.external_id}"


def chat_workspace(platform: str, tenant: TenantRow | None) -> str | None:
    """The workspace of a chat account in its own workspace; None for anyone else."""
    if tenant is None or tenant.platform != platform:
        return None
    return tenant_workspace(tenant)


def serves(server: SignedIdentityServer, workspace: str | None) -> bool:
    """Whether a server takes people of `workspace`; the MCP relay's limit too."""
    return workspace is not None and (server.workspaces is None or workspace in server.workspaces)


def node_token_key(server: SignedIdentityServer) -> str:
    """Where a token is stored: per issuer and server, so two servers never share one."""
    return f"{server.issuer}#{server.name}"


def server_base(server: SignedIdentityServer) -> str:
    """The server's base URL, which serves its API and token endpoint."""
    return server.audience.rstrip("/")


@asynccontextmanager
async def node_http(
    runtime: WebRuntime, server: SignedIdentityServer
) -> AsyncIterator[httpx.AsyncClient]:
    """The shared client, or one trusting the server's private CA when it has one."""
    if server.ca_certificate is None:
        yield runtime.http_client
        return
    context = ssl.create_default_context()
    context.load_verify_locations(cadata=server.ca_certificate)
    async with httpx.AsyncClient(verify=context, timeout=NODE_TIMEOUT_S) as http:
        yield http


def _refused(runtime: WebRuntime, key: tuple[uuid.UUID, str], now: datetime) -> bool:
    until = runtime.exchange_refused.get(key)
    return until is not None and until > now


def _remember_refusal(runtime: WebRuntime, key: tuple[uuid.UUID, str], until: datetime) -> None:
    refused = runtime.exchange_refused
    if len(refused) >= _MAX_REFUSALS:
        del refused[next(iter(refused))]  # oldest first: insertion order
    refused[key] = until


async def node_token(
    runtime: WebRuntime,
    caller: WebCaller,
    http: httpx.AsyncClient,
    *,
    kind: str,
    server: SignedIdentityServer,
    client: ExchangeClient,
) -> str:
    """The caller's access token at `server`, exchanged when missing or nearly spent."""
    person, now, key = caller.person, runtime.now(), runtime.identity_key
    if key is None:
        raise NodeAccessError("unconfigured")
    refused_key = (person.id, kind)
    if _refused(runtime, refused_key, now):
        raise NodeAccessError("refused")
    token_key = node_token_key(server)
    async with runtime.sessionmaker() as db:
        stored = await tokens_store.get_node_token(db, person_id=person.id, token_key=token_key)
        platform, user_id = await linked_identity(db, person)
        tenant = await tenants_store.get_tenant(db, person.tenant_id)
    workspace = chat_workspace(platform, tenant)
    if workspace is None:
        raise NodeAccessError("no_account")
    # Checked before the stored token too, so narrowing the list takes effect at once.
    if not serves(server, workspace):
        raise NodeAccessError("not_served")
    if stored is not None and stored.expires_at - now > MIN_TOKEN_LEFT:
        try:
            return decrypt_token(runtime.fernet, stored.access_token_enc)
        except InvalidToken:
            log.warning("web.node_token.unreadable", kind=kind)
    subject = sign_subject_token(
        key=key,
        issuer=server.issuer,
        audience=server.audience,
        subject=f"{platform}:{user_id}",
        workspace=workspace,
        now=now,
        jti=uuid.uuid4().hex,
    )
    try:
        got = await exchange_subject_token(
            http, client, subject_token=subject, timeout=NODE_TIMEOUT_S
        )
    except httpx.HTTPError as err:
        log.warning("web.node_token.unreachable", kind=kind, err_type=type(err).__name__)
        raise NodeAccessError("unreachable") from err
    except TokenExchangeError as err:
        # Its text is only the OAuth error code (`invalid_grant`), never the description.
        log.warning("web.node_token.refused", kind=kind, error=str(err))
        _remember_refusal(runtime, refused_key, now + EXCHANGE_BACKOFF)
        raise NodeAccessError("refused") from err
    life = timedelta(seconds=got.expires_in) if got.expires_in > 0 else DEFAULT_TOKEN_LIFE
    async with runtime.sessionmaker.begin() as db:
        await tokens_store.upsert_node_token(
            db,
            tenant_id=person.tenant_id,
            person_id=person.id,
            token_key=token_key,
            access_token_enc=encrypt_token(runtime.fernet, got.access_token),
            expires_at=now + life,
        )
    runtime.exchange_refused.pop(refused_key, None)
    log.info("web.node_token.exchanged", kind=kind, platform=platform)
    return got.access_token


async def forget_node_token(
    runtime: WebRuntime, caller: WebCaller, server: SignedIdentityServer
) -> None:
    """Drop a token the server stopped taking, so the next call exchanges again."""
    async with runtime.sessionmaker.begin() as db:
        await tokens_store.delete_node_token(
            db, person_id=caller.person.id, token_key=node_token_key(server)
        )
