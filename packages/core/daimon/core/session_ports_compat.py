"""Temporary M0 session codecs; lifecycle I/O goes through neutral ports.

The host owns authorization and credential resolution. The codec retains the
existing SDK return/exception types for unchanged downstream consumers.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from secrets import token_hex
from typing import cast

from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsSession
from anthropic.types.beta.session_create_params import Agent, Resource
from daimon.core.mux_backend import managed_agents, resource_ref, resource_scope
from daimon.core.mux_compat import legacy_call
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.ids import Revision, Scope
from mux.contracts.resources import ResourceBinding, Session, SessionSpec
from pydantic import JsonValue


def session_scope(
    *, tenant_id: uuid.UUID | None, account_id: uuid.UUID | None, call_site: str
) -> Scope:
    if tenant_id is not None:
        return resource_scope(
            tenant_id=str(tenant_id),
            account_id=str(account_id) if account_id is not None else "service",
            authorization_id=call_site,
        )
    return Scope.legacy_host_authorized(call_site=call_site)


def sdk_session(record: Session) -> BetaManagedAgentsSession:
    if record.native is None:
        raise ValueError("the M0 session codec requires the provider snapshot")
    return BetaManagedAgentsSession.model_validate(record.native)


async def create_session_record(
    client: AsyncAnthropic,
    *,
    agent: Agent,
    environment_id: str,
    scope: Scope,
    metadata: Mapping[str, str] | None,
    resources: Sequence[Resource] | None,
    vault_ids: Sequence[str] | None = None,
) -> BetaManagedAgentsSession:
    """None omits a field; explicit empty resources stay explicit for isolation."""
    agent_id = agent if isinstance(agent, str) else agent["id"]
    authorized = {("agent", agent_id), ("environment", environment_id)}
    for vault_id in vault_ids or ():
        authorized.add(("vault", vault_id))
    for resource in resources or ():
        if resource["type"] == "file":
            authorized.add(("file", resource["file_id"]))
        elif resource["type"] == "memory_store":
            authorized.add(("memory_store", resource["memory_store_id"]))

    secrets: dict[str, str] = {}

    def resolve(request_scope: Scope, reference: str) -> str:
        if request_scope != scope:
            raise ValueError("session credential scope differs from host authorization")
        return secrets[reference]

    try:
        backend = (
            managed_agents(client, scope=scope, resources=frozenset(authorized), secrets=resolve)
            if any(resource["type"] == "github_repository" for resource in resources or ())
            else managed_agents(client, scope=scope, resources=frozenset(authorized))
        )
        bindings: list[ResourceBinding] = []
        for index, resource in enumerate(resources or ()):
            payload = dict(resource)
            ref = None
            if resource["type"] == "github_repository":
                reference = f"session-repo:{token_hex(16)}"
                secrets[reference] = cast(str, payload.pop("authorization_token"))
                # Preserve the original request's token position when replacing
                # credential material with its host-only reference.
                payload = {
                    "authorization_token_ref" if name == "authorization_token" else name: reference
                    if name == "authorization_token"
                    else value
                    for name, value in resource.items()
                }
                kind = "repository"
            else:
                native_id = (
                    resource["file_id"]
                    if resource["type"] == "file"
                    else resource["memory_store_id"]
                )
                ref = resource_ref(
                    backend,
                    "file" if resource["type"] == "file" else "memory_store",
                    native_id,
                    scope=scope,
                )
                kind = "artifact" if resource["type"] == "file" else "native"
            bindings.append(
                ResourceBinding(
                    id=f"mount:{index}",
                    kind=kind,
                    resource=ref,
                    native=ExtensionConfig(
                        namespace="anthropic.session_resource_create",
                        version=1,
                        value={"resource": cast(JsonValue, payload)},
                    ),
                )
            )
        config: dict[str, JsonValue] = {}
        if not isinstance(agent, str):
            config["agent"] = cast(JsonValue, dict(agent))
        if vault_ids is not None:
            config["vault_ids"] = list(vault_ids)
        values: dict[str, object] = {
            "agent": resource_ref(backend, "agent", agent_id, scope=scope),
            "agent_revision": Revision(local=0),
            "environment": resource_ref(backend, "environment", environment_id, scope=scope),
            "config_revision": 0,
            "extensions": {
                "anthropic.session_create": ExtensionConfig(
                    namespace="anthropic.session_create", version=1, value=config
                )
            },
        }
        if metadata is not None:
            values["metadata"] = dict(metadata)
        if resources is not None:
            values["resources"] = tuple(bindings)
        record = await legacy_call(
            backend.sessions.create(scope, SessionSpec.model_validate(values), key=token_hex(16))
        )
        return sdk_session(record)
    finally:
        secrets.clear()


async def retrieve_session_record(
    client: AsyncAnthropic, session_id: str, *, scope: Scope
) -> BetaManagedAgentsSession:
    backend = managed_agents(client, scope=scope, resources=frozenset({("session", session_id)}))
    return sdk_session(
        await legacy_call(
            backend.sessions.retrieve(
                scope, resource_ref(backend, "session", session_id, scope=scope)
            )
        )
    )


async def archive_session_record(client: AsyncAnthropic, session_id: str, *, scope: Scope) -> None:
    backend = managed_agents(client, scope=scope, resources=frozenset({("session", session_id)}))
    await legacy_call(
        backend.sessions.archive(
            scope, resource_ref(backend, "session", session_id, scope=scope), key=token_hex(16)
        )
    )
