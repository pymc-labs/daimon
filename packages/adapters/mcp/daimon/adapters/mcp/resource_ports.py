"""Temporary SDK codecs for MCP resource consumers; provider I/O stays in mux."""

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from secrets import token_hex
from typing import cast

from anthropic import AsyncAnthropic
from anthropic._models import construct_type, construct_type_unchecked
from anthropic.types.beta import BetaManagedAgentsSession, FileMetadata
from anthropic.types.beta.sessions import (
    BetaManagedAgentsEventParams,
    BetaManagedAgentsSendSessionEvents,
    BetaManagedAgentsSessionEvent,
)
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.core.mux_backend import managed_agents, resource_ref, resource_scope
from daimon.core.mux_compat import legacy_call, legacy_iter
from mux.contracts.ids import Scope
from mux.drivers.anthropic.resources.artifacts import Artifacts
from mux.drivers.anthropic.resources.session_tools import EventQuery, SessionSend, SessionTools


def mcp_scope(auth: AuthIdentity) -> Scope:
    return resource_scope(
        tenant_id=str(auth.tenant_id), account_id=str(auth.account_id), authorization_id="mcp"
    )


async def walk_sessions(
    client: AsyncAnthropic, *, agent_id: str, scope: Scope, page: str | None = None
) -> AsyncIterator[BetaManagedAgentsSession]:
    backend = managed_agents(client, scope=scope, resources=frozenset({("agent", agent_id)}))
    port = backend.extension(SessionTools, namespace="anthropic.session_tools", version=1)
    async for item in legacy_iter(
        port.walk(scope, resource_ref(backend, "agent", agent_id, scope=scope), page=page)
    ):
        yield construct_type_unchecked(value=item.native, type_=BetaManagedAgentsSession)


async def send_session_events(
    client: AsyncAnthropic,
    session_id: str,
    *,
    events: Sequence[BetaManagedAgentsEventParams],
    scope: Scope,
) -> BetaManagedAgentsSendSessionEvents:
    backend = managed_agents(client, scope=scope, resources=frozenset({("session", session_id)}))
    port = backend.extension(SessionTools, namespace="anthropic.session_tools", version=1)
    result = await legacy_call(
        port.send(
            scope,
            resource_ref(backend, "session", session_id, scope=scope),
            SessionSend.model_validate({"events": events}),
            key=token_hex(16),
        )
    )
    return construct_type_unchecked(value=result.native, type_=BetaManagedAgentsSendSessionEvents)


@dataclass(frozen=True)
class SessionEventPage:
    data: tuple[BetaManagedAgentsSessionEvent, ...]
    next_page: str | None


async def list_session_events(
    client: AsyncAnthropic, session_id: str, *, query: Mapping[str, object], scope: Scope
) -> SessionEventPage:
    backend = managed_agents(client, scope=scope, resources=frozenset({("session", session_id)}))
    port = backend.extension(SessionTools, namespace="anthropic.session_tools", version=1)
    result = await legacy_call(
        port.list_events(
            scope,
            resource_ref(backend, "session", session_id, scope=scope),
            EventQuery.model_validate(query),
        )
    )
    return SessionEventPage(
        data=tuple(
            cast(
                BetaManagedAgentsSessionEvent,
                construct_type(value=event.native, type_=BetaManagedAgentsSessionEvent),
            )
            for event in result.data
        ),
        next_page=result.next_page,
    )


async def retrieve_file_metadata(
    client: AsyncAnthropic, file_id: str, *, scope: Scope
) -> FileMetadata:
    backend = managed_agents(client, scope=scope, resources=frozenset({("file", file_id)}))
    port = backend.extension(Artifacts, namespace="anthropic.artifacts", version=1)
    result = await legacy_call(
        port.retrieve_native(scope, resource_ref(backend, "file", file_id, scope=scope))
    )
    return construct_type_unchecked(value=result.native, type_=FileMetadata)
