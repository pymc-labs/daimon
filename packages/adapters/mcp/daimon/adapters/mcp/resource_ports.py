"""Temporary SDK codecs for MCP resource consumers; provider I/O stays in mux."""

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from secrets import token_hex
from typing import IO, cast

from anthropic import AsyncAnthropic
from anthropic._models import construct_type, construct_type_unchecked
from anthropic.types.beta import BetaManagedAgentsSession, BetaManagedAgentsVault, FileMetadata
from anthropic.types.beta.sessions import (
    BetaManagedAgentsEventParams,
    BetaManagedAgentsSendSessionEvents,
    BetaManagedAgentsSessionEvent,
)
from anthropic.types.beta.vaults import BetaManagedAgentsCredential
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.core.mux_backend import managed_agents, resource_ref, resource_scope
from daimon.core.mux_compat import legacy_call, legacy_iter
from mux.contracts.ids import Scope
from mux.drivers.anthropic.credential_schemas import CredentialCreate
from mux.drivers.anthropic.resources.artifacts import Artifacts
from mux.drivers.anthropic.resources.session_tools import EventQuery, SessionSend, SessionTools
from mux.drivers.anthropic.resources.skills import NativeSkillVersions
from mux.drivers.anthropic.resources.vaults import Vaults


def mcp_scope(auth: AuthIdentity) -> Scope:
    return resource_scope(
        tenant_id=str(auth.tenant_id), account_id=str(auth.account_id), authorization_id="mcp"
    )


async def retrieve_session(
    client: AsyncAnthropic, session_id: str, *, scope: Scope
) -> BetaManagedAgentsSession:
    backend = managed_agents(client, scope=scope, resources=frozenset({("session", session_id)}))
    port = backend.extension(SessionTools, namespace="anthropic.session_tools", version=1)
    result = await legacy_call(
        port.retrieve(scope, resource_ref(backend, "session", session_id, scope=scope))
    )
    return construct_type_unchecked(value=result.native, type_=BetaManagedAgentsSession)


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


async def walk_named_vaults(
    client: AsyncAnthropic, display_name: str, *, scope: Scope
) -> AsyncIterator[BetaManagedAgentsVault]:
    backend = managed_agents(
        client, scope=scope, resources=frozenset({("vault_name", display_name)})
    )
    port = backend.extension(Vaults, namespace="anthropic.vaults", version=1)
    async for item in legacy_iter(port.walk_named(scope, display_name)):
        yield construct_type_unchecked(value=item.native, type_=BetaManagedAgentsVault)


async def walk_vault_credentials(
    client: AsyncAnthropic, vault_id: str, *, scope: Scope
) -> AsyncIterator[BetaManagedAgentsCredential]:
    backend = managed_agents(client, scope=scope, resources=frozenset({("vault", vault_id)}))
    port = backend.extension(Vaults, namespace="anthropic.vaults", version=1)
    async for item in legacy_iter(
        port.credential_walk_native(scope, resource_ref(backend, "vault", vault_id, scope=scope))
    ):
        yield construct_type_unchecked(value=item.native, type_=BetaManagedAgentsCredential)


async def create_repo_credential(
    client: AsyncAnthropic, vault_id: str, *, token: str, metadata: Mapping[str, str], scope: Scope
) -> BetaManagedAgentsCredential:
    reference = token_hex(16)
    materials = {reference: token}
    try:
        backend = managed_agents(
            client,
            scope=scope,
            resources=frozenset({("vault", vault_id)}),
            secrets=lambda _scope, ref: materials[ref],
        )
        port = backend.extension(Vaults, namespace="anthropic.vaults", version=1)
        identity = await legacy_call(
            port.create_credential_id(
                scope,
                resource_ref(backend, "vault", vault_id, scope=scope),
                CredentialCreate.model_validate(
                    {
                        "auth": {
                            "type": "static_bearer",
                            "mcp_server_url": "https://github.com",
                            "token_ref": reference,
                        },
                        "metadata": dict(metadata),
                    }
                ),
                key=token_hex(16),
            )
        )
        return construct_type_unchecked(value={"id": identity}, type_=BetaManagedAgentsCredential)
    finally:
        materials.clear()


async def walk_skill_versions(
    client: AsyncAnthropic, skill_id: str, *, scope: Scope
) -> AsyncIterator[Mapping[str, object]]:
    backend = managed_agents(client, scope=scope, resources=frozenset({("skill", skill_id)}))
    port = backend.extension(NativeSkillVersions, namespace="anthropic.skills_versions", version=1)
    async for item in legacy_iter(port.walk_native(scope, skill_id)):
        yield item


async def upload_bundle_file(
    client: AsyncAnthropic, body: IO[bytes], *, scope: Scope
) -> FileMetadata:
    backend = managed_agents(client, scope=scope)
    port = backend.extension(Artifacts, namespace="anthropic.artifacts", version=1)
    item = await legacy_call(
        port.upload_native(
            scope, body, filename="bundle.tar.gz", media_type="application/gzip", key=token_hex(16)
        )
    )
    return construct_type_unchecked(value=item.native, type_=FileMetadata)


async def walk_chart_files(
    client: AsyncAnthropic, session_id: str, *, limit: int, scope: Scope
) -> AsyncIterator[FileMetadata]:
    backend = managed_agents(client, scope=scope, resources=frozenset({("session", session_id)}))
    port = backend.extension(Artifacts, namespace="anthropic.artifacts", version=1)
    async for item in legacy_iter(
        port.walk_session_native(
            scope, resource_ref(backend, "session", session_id, scope=scope), limit=limit
        )
    ):
        yield construct_type_unchecked(value=item.native, type_=FileMetadata)
