"""A signed-in person's access token at a node's server: `daimon.core.node_exchange`,
asked as the chat account the person runs as."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import node_exchange
from daimon.core.config import SignedIdentityServer
from daimon.core.node_exchange import (
    NODE_TIMEOUT_S,
    NodeAccessError,
    NodeAccessProblem,
    NodeExchange,
    asker_of_person,
    chat_workspace,
    node_token_key,
    server_base,
    serves,
    serves_tenant,
    tenant_workspace,
)
from daimon.core.stores.domain import WebPersonRow
from daimon.core.token_exchange import ExchangeClient
from fastapi import HTTPException

__all__ = [
    "NODE_TIMEOUT_S",
    "NodeAccessError",
    "NodeAccessProblem",
    "chat_workspace",
    "forget_node_token",
    "node_exchange_of",
    "node_http",
    "node_token",
    "node_token_key",
    "server_base",
    "require_served",
    "serves",
    "serves_tenant",
    "tenant_workspace",
]


async def require_served(
    runtime: WebRuntime, server: SignedIdentityServer, tenant_id: uuid.UUID
) -> None:
    """404 for a workspace the server does not serve, before any work or any mention of it."""
    if not await serves_tenant(runtime.sessionmaker, server, tenant_id):
        raise HTTPException(status_code=404, detail="Not found.")


def node_exchange_of(runtime: WebRuntime) -> NodeExchange:
    return NodeExchange(
        sessionmaker=runtime.sessionmaker,
        fernet=runtime.fernet,
        identity_key=runtime.identity_key,
        held=runtime.exchange_held,
        now=runtime.now,
    )


@asynccontextmanager
async def node_http(
    runtime: WebRuntime, server: SignedIdentityServer
) -> AsyncIterator[httpx.AsyncClient]:
    """The shared client, or one trusting the server's private CA when it has one."""
    async with node_exchange.node_http(runtime.http_client, server) as http:
        yield http


async def node_token(
    runtime: WebRuntime,
    person: WebPersonRow,
    http: httpx.AsyncClient,
    *,
    kind: str,
    server: SignedIdentityServer,
    client: ExchangeClient,
) -> str:
    """The person's access token at `server`, exchanged when missing or nearly spent."""
    if runtime.identity_key is None:
        raise NodeAccessError("unconfigured")
    async with runtime.sessionmaker() as db:
        asker = await asker_of_person(db, person)
    return await node_exchange.node_token(
        node_exchange_of(runtime), asker, http, kind=kind, server=server, client=client
    )


async def forget_node_token(
    runtime: WebRuntime, person: WebPersonRow, server: SignedIdentityServer
) -> None:
    """Drop a token the server stopped taking, so the next call exchanges again."""
    await node_exchange.forget_node_token(runtime.sessionmaker, person.id, server)
