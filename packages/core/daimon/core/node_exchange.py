"""A chat account's access token at a node's server, by token exchange (RFC 8693).

Daimon signs who the chat account is and trades that at the server for a token, kept
encrypted per web person and server and exchanged again when nearly spent; an account
with no web person exchanges on every call. Only chat accounts of a workspace the server
serves get one. A refusal, or a server that cannot be reached, is remembered for a while,
so it is not asked on every call.
"""

from __future__ import annotations

import ssl
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import httpx
import structlog
from cryptography.fernet import InvalidToken, MultiFernet
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from daimon.core.config import Settings, SignedIdentityServer
from daimon.core.errors import DaimonError
from daimon.core.github_credentials import decrypt_token, encrypt_token
from daimon.core.mcp_identity import load_signing_key
from daimon.core.stores import tenants as tenants_store
from daimon.core.stores import web_node_tokens as tokens_store
from daimon.core.stores import web_people as people_store
from daimon.core.stores.domain import CHAT_PLATFORMS, TenantRow, WebPersonRow
from daimon.core.token_exchange import (
    ExchangeClient,
    TokenExchangeError,
    exchange_subject_token,
    sign_subject_token,
)
from daimon.core.web_identity import chat_identity, linked_identity
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

#: A token with less left than this is exchanged again before use.
MIN_TOKEN_LEFT = timedelta(seconds=60)
#: A refused exchange is not tried again for this long.
EXCHANGE_BACKOFF = timedelta(minutes=5)
#: Nor is a server that could not be reached, for this long.
UNREACHABLE_BACKOFF = timedelta(seconds=60)
#: Token endpoint answers that mean "not now" (timeout, rate limit), held like a 5xx.
_BUSY = frozenset({408, 429})
#: A logged error code is cut to this many characters.
_ERROR_CHARS = 64
#: How long a token is taken to last when the server does not say.
DEFAULT_TOKEN_LIFE = timedelta(minutes=5)
#: Every request to a node's server gives up after this long.
NODE_TIMEOUT_S = 10.0
_MAX_REFUSALS = 1024

NodeAccessProblem = Literal["no_account", "not_served", "refused", "unconfigured", "unreachable"]
type NodeHolds = dict[tuple[uuid.UUID, str], tuple[datetime, NodeAccessProblem]]


class NodeAccessError(DaimonError):
    """No token for this person at this server."""

    def __init__(self, problem: NodeAccessProblem) -> None:
        super().__init__(problem)
        self.problem: NodeAccessProblem = problem


@dataclass(frozen=True)
class NodeAsker:
    """Whom a node's server is asked as: a chat account, by its platform user id."""

    tenant_id: uuid.UUID
    account_id: uuid.UUID
    platform: str
    user_id: str
    person_id: uuid.UUID | None = None
    """Their web person, under whom tokens are kept; without one, none is kept."""

    @property
    def key(self) -> uuid.UUID:
        """Whose failures are held: the web person's, else the account's."""
        return self.person_id or self.account_id


@dataclass(frozen=True)
class NodeExchange:
    """What an exchange needs; `held` is the process's remembered failures."""

    sessionmaker: async_sessionmaker[AsyncSession]
    fernet: MultiFernet | None
    identity_key: Ed25519PrivateKey | None
    held: NodeHolds
    now: Callable[[], datetime]


def node_identity_key(settings: Settings) -> Ed25519PrivateKey | None:
    """The caller-identity key, when a connection node exchanges tokens with it."""
    key = settings.mcp_identity.signing_key
    web = settings.web
    if key is None or web is None or not web.nodes.configured():
        return None
    return load_signing_key(key.get_secret_value())


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


async def serves_tenant(
    sessionmaker: async_sessionmaker[AsyncSession],
    server: SignedIdentityServer,
    tenant_id: uuid.UUID,
) -> bool:
    """Whether a server takes the tenant's workspace.

    Every path that shows or reaches the node asks this first: a workspace the server
    does not serve sees no node, as if none were configured.
    """
    async with sessionmaker() as db:
        tenant = await tenants_store.get_tenant(db, tenant_id)
    return serves(server, tenant_workspace(tenant))


def node_token_key(server: SignedIdentityServer) -> str:
    """Where a token is stored: per issuer and server, so two servers never share one."""
    return f"{server.issuer}#{server.name}"


def server_base(server: SignedIdentityServer) -> str:
    """The server's base URL, which serves its API and token endpoint."""
    return server.audience.rstrip("/")


def exchange_client(
    server: SignedIdentityServer, *, client_id: str, client_secret: str, scope: str
) -> ExchangeClient:
    """The server's token endpoint, as the node's OAuth client."""
    return ExchangeClient(
        token_url=f"{server_base(server)}/oauth/token",
        client_id=client_id,
        client_secret=client_secret,
        scope=scope,
    )


@asynccontextmanager
async def node_http(
    shared: httpx.AsyncClient | None, server: SignedIdentityServer
) -> AsyncIterator[httpx.AsyncClient]:
    """`shared`, or an own client: always when the server has a private CA to trust."""
    if server.ca_certificate is None and shared is not None:
        yield shared
        return
    if server.ca_certificate is None:
        async with httpx.AsyncClient(timeout=NODE_TIMEOUT_S) as http:
            yield http
        return
    context = ssl.create_default_context()
    context.load_verify_locations(cadata=server.ca_certificate)
    async with httpx.AsyncClient(verify=context, timeout=NODE_TIMEOUT_S) as http:
        yield http


async def asker_of_person(session: AsyncSession, person: WebPersonRow) -> NodeAsker:
    """A web person, as the chat account they run as (a visitor never has one)."""
    platform, user_id = await linked_identity(session, person)
    return NodeAsker(
        tenant_id=person.tenant_id,
        account_id=person.account_id,
        platform=platform,
        user_id=user_id,
        person_id=person.id,
    )


async def asker_of_account(
    session: AsyncSession, *, tenant_id: uuid.UUID, account_id: uuid.UUID
) -> NodeAsker | None:
    """An account's chat principal in the tenant, with its web person if any; None without."""
    found = await chat_identity(session, tenant_id=tenant_id, account_id=account_id)
    if found is None:
        return None
    person = await people_store.get_person_for_account(
        session, tenant_id=tenant_id, account_id=account_id
    )
    return NodeAsker(
        tenant_id=tenant_id,
        account_id=account_id,
        platform=found[0],
        user_id=found[1],
        person_id=None if person is None else person.id,
    )


def _held(holds: NodeHolds, key: tuple[uuid.UUID, str], now: datetime) -> None:
    """Raise the remembered failure while it holds."""
    held = holds.get(key)
    if held is not None and held[0] > now:
        raise NodeAccessError(held[1])


def _hold(
    holds: NodeHolds, key: tuple[uuid.UUID, str], problem: NodeAccessProblem, until: datetime
) -> NodeAccessError:
    if len(holds) >= _MAX_REFUSALS:
        del holds[next(iter(holds))]  # oldest first: insertion order
    holds[key] = (until, problem)
    return NodeAccessError(problem)


async def node_token(
    exchange: NodeExchange,
    asker: NodeAsker,
    http: httpx.AsyncClient,
    *,
    kind: str,
    server: SignedIdentityServer,
    client: ExchangeClient,
) -> str:
    """The asker's access token at `server`, exchanged when missing or nearly spent."""
    now, key = exchange.now(), exchange.identity_key
    if key is None:
        raise NodeAccessError("unconfigured")
    held_key = (asker.key, kind)
    _held(exchange.held, held_key, now)
    token_key = node_token_key(server)
    person_id, fernet = asker.person_id, exchange.fernet
    kept = person_id is not None and fernet is not None
    async with exchange.sessionmaker() as db:
        stored = None
        if kept and person_id is not None:
            stored = await tokens_store.get_node_token(db, person_id=person_id, token_key=token_key)
        tenant = await tenants_store.get_tenant(db, asker.tenant_id)
    workspace = chat_workspace(asker.platform, tenant)
    if workspace is None:
        raise NodeAccessError("no_account")
    # Checked before the stored token too, so narrowing the list takes effect at once.
    if not serves(server, workspace):
        raise NodeAccessError("not_served")
    if stored is not None and fernet is not None and stored.expires_at - now > MIN_TOKEN_LEFT:
        try:
            return decrypt_token(fernet, stored.access_token_enc)
        except InvalidToken:
            log.warning("web.node_token.unreadable", kind=kind)
    subject = sign_subject_token(
        key=key,
        issuer=server.issuer,
        audience=server.audience,
        subject=f"{asker.platform}:{asker.user_id}",
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
        raise _hold(exchange.held, held_key, "unreachable", now + UNREACHABLE_BACKOFF) from err
    except TokenExchangeError as err:
        # Its text is only the OAuth error code (`invalid_grant`), never the description.
        error = str(err)[:_ERROR_CHARS]
        if err.status is not None and (err.status >= 500 or err.status in _BUSY):
            log.warning("web.node_token.unreachable", kind=kind, error=error)
            raise _hold(exchange.held, held_key, "unreachable", now + UNREACHABLE_BACKOFF) from err
        log.warning("web.node_token.refused", kind=kind, error=error)
        raise _hold(exchange.held, held_key, "refused", now + EXCHANGE_BACKOFF) from err
    if person_id is not None and fernet is not None:
        life = timedelta(seconds=got.expires_in) if got.expires_in > 0 else DEFAULT_TOKEN_LIFE
        async with exchange.sessionmaker.begin() as db:
            await tokens_store.upsert_node_token(
                db,
                tenant_id=asker.tenant_id,
                person_id=person_id,
                token_key=token_key,
                access_token_enc=encrypt_token(fernet, got.access_token),
                expires_at=now + life,
            )
    exchange.held.pop(held_key, None)
    log.info("web.node_token.exchanged", kind=kind, platform=asker.platform)
    return got.access_token


async def forget_node_token(
    sessionmaker: async_sessionmaker[AsyncSession],
    person_id: uuid.UUID | None,
    server: SignedIdentityServer,
) -> None:
    """Drop a token the server stopped taking, so the next call exchanges again."""
    if person_id is None:
        return
    async with sessionmaker.begin() as db:
        await tokens_store.delete_node_token(
            db, person_id=person_id, token_key=node_token_key(server)
        )
