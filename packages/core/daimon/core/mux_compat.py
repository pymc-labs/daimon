"""Temporary M0 host edge for legacy Anthropic error and model consumers.

No provider requests belong here. Remove SDK model decoding once every host
consumer uses the neutral resource records (tracked in sprint FOLLOWUPS.md).
"""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Mapping, Sequence
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from anthropic import AnthropicError, AsyncAnthropic
from anthropic.types.beta import (
    BetaEnvironment,
    BetaManagedAgentsAgent,
    FileMetadata,
    SkillCreateResponse,
    SkillListResponse,
)
from anthropic.types.beta.skills import VersionCreateResponse, VersionListResponse
from daimon.core.mux_backend import managed_agents, resource_ref
from mux.contracts.ids import Revision, Scope, SkillRef
from mux.contracts.ports import SkillVersions
from mux.contracts.resources import (
    Agent,
    AgentFilter,
    AgentPatch,
    AgentSpec,
    Environment,
    EnvironmentFilter,
    EnvironmentPatch,
    EnvironmentSpec,
    SkillUpload,
    SkillUploadFile,
)
from mux.drivers.anthropic.resources._secrets import CredentialFileUpload
from mux.drivers.anthropic.resources.artifacts import Artifacts as NativeArtifacts
from mux.drivers.anthropic.resources.walk import ResourceWalk
from mux.errors import ProviderError

if TYPE_CHECKING:
    from anthropic.types.beta import BetaManagedAgentsVault
    from anthropic.types.beta.vaults.beta_managed_agents_credential import (
        BetaManagedAgentsCredential,
    )
    from mux.contracts.resources import Skill, SkillVersion
    from mux.drivers.anthropic import AnthropicManagedAgents
    from mux.drivers.anthropic.resources.vaults import Vaults as NativeVaults


async def legacy_call[T](call: Awaitable[T]) -> T:
    """Preserve today's exception type, message and response at the host edge."""
    try:
        return await call
    except ProviderError as error:
        if isinstance(error.__cause__, AnthropicError):
            raise error.__cause__ from None
        raise


async def legacy_iter[T](items: AsyncIterable[T]) -> AsyncIterator[T]:
    try:
        async for item in items:
            yield item
    except ProviderError as error:
        if isinstance(error.__cause__, AnthropicError):
            raise error.__cause__ from None
        raise


# Payload codecs are deliberately at the host edge: existing authoring models
# mirror SDK kwargs, while all calls themselves consume neutral specs/patches.


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("expected resource object")
    return cast(Mapping[str, object], value)


def _list(value: object) -> list[object]:
    if not isinstance(value, (list, tuple)):
        raise TypeError("expected resource list")
    return list(cast(Sequence[object], value))


def _agent_values(payload: Mapping[str, object]) -> dict[str, object]:
    result = dict(payload)
    extensions: dict[str, object] = {}
    if "model" in result and result["model"] is not None:
        model = result["model"]
        result["model"] = {
            "provider": "anthropic",
            "id": model if isinstance(model, str) else _mapping(model)["id"],
        }
        if not isinstance(model, str):
            extensions["anthropic.model_config"] = {
                "namespace": "anthropic.model_config",
                "version": 1,
                "value": {"model": model},
            }
    if "tools" in result and result["tools"] is not None:
        extensions["anthropic.agent_tools"] = {
            "namespace": "anthropic.agent_tools",
            "version": 1,
            "value": {"tools": result.pop("tools")},
        }
    if "multiagent" in result:
        value = result.pop("multiagent")
        extensions["anthropic.multiagent"] = {
            "namespace": "anthropic.multiagent",
            "version": 1,
            "value": value if value is not None else {"clear": True},
        }
    if "mcp_servers" in result and result["mcp_servers"] is not None:
        result["mcp_servers"] = [
            {"name": _mapping(s)["name"], "url": _mapping(s)["url"]}
            for s in _list(result["mcp_servers"])
        ]
    if "skills" in result and result["skills"] is not None:
        result["skills"] = [
            {
                "id": _mapping(s)["skill_id"],
                "source": _mapping(s)["type"],
                **({"version": _mapping(s)["version"]} if "version" in _mapping(s) else {}),
            }
            for s in _list(result["skills"])
        ]
    if extensions:
        result["extensions"] = extensions
    return result


def agent_spec(payload: Mapping[str, object]) -> AgentSpec:
    values = _agent_values(payload)
    extensions = list(_mapping(values.pop("extensions", {})).values())
    nulls = [
        name
        for name in ("description", "system", "tools", "mcp_servers", "skills", "metadata")
        if name in payload and payload[name] is None
    ]
    if nulls:
        extensions.append(
            {"namespace": "anthropic.agent_create_nulls", "version": 1, "value": {"fields": nulls}}
        )
    if extensions:
        values["extensions"] = tuple(extensions)
    return AgentSpec.model_validate(values)


def agent_patch(payload: Mapping[str, object]) -> AgentPatch:
    return AgentPatch.model_validate(_agent_values(payload))


def _environment_values(payload: Mapping[str, object]) -> dict[str, object]:
    result = dict(payload)
    if "config" in result:
        config = result.pop("config")
        result["native_config"] = {
            "namespace": "anthropic.environment_config",
            "version": 1,
            "value": {"config": config},
        }
    return result


def environment_spec(payload: Mapping[str, object]) -> EnvironmentSpec:
    values = _environment_values(payload)
    if "description" in payload and payload["description"] is None:
        native = _mapping(values.get("native_config", {}))
        native_values = dict(_mapping(native.get("value", {})))
        native_values["create_nulls"] = ["description"]
        values["native_config"] = {
            "namespace": "anthropic.environment_config",
            "version": 1,
            "value": native_values,
        }
    return EnvironmentSpec.model_validate(values)


def environment_patch(payload: Mapping[str, object]) -> EnvironmentPatch:
    return EnvironmentPatch.model_validate(_environment_values(payload))


def sdk_agent(record: Agent) -> BetaManagedAgentsAgent:
    if record.native is not None:
        return BetaManagedAgentsAgent.model_construct(_fields_set=None, **_mapping(record.native))
    spec = record.spec
    from mux.drivers.anthropic.resources.agents import agent_payload

    payload = agent_payload(spec)
    payload["model"] = {"id": spec.model.id, **dict(spec.model.options)}
    payload.update(
        {
            "id": record.ref.id,
            "version": record.revision.local,
            "type": "agent",
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "archived_at": record.archived_at,
        }
    )
    return BetaManagedAgentsAgent.model_validate(payload)


def sdk_environment(record: Environment) -> BetaEnvironment:
    if record.native is not None:
        return BetaEnvironment.model_construct(_fields_set=None, **_mapping(record.native))
    from mux.drivers.anthropic.resources.environments import environment_payload

    payload = environment_payload(record.spec)
    payload.update(
        {
            "id": record.ref.id,
            "type": "environment",
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "archived_at": record.archived_at,
        }
    )
    return BetaEnvironment.model_validate(payload)


async def list_agents(
    client: AsyncAnthropic, *, include_archived: bool = False, scope: Scope
) -> AsyncIterator[BetaManagedAgentsAgent]:
    backend = managed_agents(client, scope=scope)
    port = backend.extension(ResourceWalk, namespace="anthropic.resource_walk", version=1)
    async for record in legacy_iter(
        port.agents(scope, filters=AgentFilter(include_archived=include_archived))
    ):
        yield sdk_agent(record)


async def list_environments(
    client: AsyncAnthropic, *, include_archived: bool = False, scope: Scope
) -> AsyncIterator[BetaEnvironment]:
    backend = managed_agents(client, scope=scope)
    port = backend.extension(ResourceWalk, namespace="anthropic.resource_walk", version=1)
    async for record in legacy_iter(
        port.environments(scope, filters=EnvironmentFilter(include_archived=include_archived))
    ):
        yield sdk_environment(record)


async def retrieve_agent(
    client: AsyncAnthropic, agent_id: str, *, scope: Scope
) -> BetaManagedAgentsAgent:
    backend = managed_agents(client, scope=scope, resources=frozenset({("agent", agent_id)}))
    return sdk_agent(
        await legacy_call(
            backend.agents.retrieve(scope, resource_ref(backend, "agent", agent_id, scope=scope))
        )
    )


async def create_agent(
    client: AsyncAnthropic, payload: Mapping[str, object], *, scope: Scope
) -> BetaManagedAgentsAgent:
    backend = managed_agents(client, scope=scope)
    return sdk_agent(
        await legacy_call(backend.agents.create(scope, agent_spec(payload), key=str(uuid4())))
    )


async def update_agent(
    client: AsyncAnthropic,
    agent_id: str,
    *,
    version: int,
    payload: Mapping[str, object],
    scope: Scope,
) -> BetaManagedAgentsAgent:
    backend = managed_agents(client, scope=scope, resources=frozenset({("agent", agent_id)}))
    return sdk_agent(
        await legacy_call(
            backend.agents.update(
                scope,
                resource_ref(backend, "agent", agent_id, scope=scope),
                agent_patch(payload),
                expected=Revision(local=version, native=str(version)),
                key=str(uuid4()),
            )
        )
    )


async def archive_agent(client: AsyncAnthropic, agent_id: str, *, scope: Scope) -> None:
    backend = managed_agents(client, scope=scope, resources=frozenset({("agent", agent_id)}))
    await legacy_call(
        backend.agents.archive(
            scope, resource_ref(backend, "agent", agent_id, scope=scope), key=str(uuid4())
        )
    )


async def archive_environment(client: AsyncAnthropic, environment_id: str, *, scope: Scope) -> None:
    backend = managed_agents(
        client, scope=scope, resources=frozenset({("environment", environment_id)})
    )
    await legacy_call(
        backend.environments.archive(
            scope,
            resource_ref(backend, "environment", environment_id, scope=scope),
            key=str(uuid4()),
        )
    )


async def create_environment(
    client: AsyncAnthropic, payload: Mapping[str, object], *, scope: Scope
) -> BetaEnvironment:
    backend = managed_agents(client, scope=scope)
    return sdk_environment(
        await legacy_call(
            backend.environments.create(scope, environment_spec(payload), key=str(uuid4()))
        )
    )


async def update_environment(
    client: AsyncAnthropic, environment_id: str, payload: Mapping[str, object], *, scope: Scope
) -> BetaEnvironment:
    backend = managed_agents(
        client, scope=scope, resources=frozenset({("environment", environment_id)})
    )
    return sdk_environment(
        await legacy_call(
            backend.environments.update(
                scope,
                resource_ref(backend, "environment", environment_id, scope=scope),
                environment_patch(payload),
                expected=Revision(local=0),
                key=str(uuid4()),
            )
        )
    )


def sdk_skill(record: Skill) -> SkillListResponse:
    if record.native is not None:
        return SkillListResponse.model_construct(_fields_set=None, **_mapping(record.native))
    return SkillListResponse.model_validate(
        {
            "id": record.id,
            "display_title": record.display_title,
            "source": record.source,
            "latest_version": record.latest_version.version if record.latest_version else None,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "type": "skill",
        }
    )


def sdk_skill_version(record: SkillVersion) -> VersionListResponse:
    if record.native is not None:
        return VersionListResponse.model_construct(_fields_set=None, **_mapping(record.native))
    return VersionListResponse.model_validate(
        {
            "id": record.ref.id,
            "skill_id": record.ref.id,
            "version": record.version,
            "name": record.name,
            "description": record.description,
            "directory": record.name,
            "created_at": record.created_at,
            "type": "skill_version",
        }
    )


async def collect_skills(
    client: AsyncAnthropic, *, limit: int, scope: Scope
) -> tuple[list[SkillListResponse], bool]:
    backend = managed_agents(client, scope=scope)
    port = backend.extension(ResourceWalk, namespace="anthropic.resource_walk", version=1)
    rows: list[SkillListResponse] = []
    truncated = False
    async for page in legacy_iter(port.skill_pages(scope, limit=limit)):
        rows.extend(sdk_skill(record) for record in page.data)
        truncated = truncated or (len(page.data) >= limit and not page.has_more)
    return rows, truncated


async def create_skill(
    client: AsyncAnthropic,
    *,
    display_title: str,
    data: bytes,
    filename: str = "SKILL.zip",
    media_type: str = "application/zip",
    scope: Scope,
) -> SkillCreateResponse:
    backend = managed_agents(client, scope=scope)
    record = await legacy_call(
        backend.skills.create(
            scope,
            SkillUpload(
                display_title=display_title,
                files=(SkillUploadFile(path=filename, content=data, media_type=media_type),),
            ),
            key=str(uuid4()),
        )
    )
    return SkillCreateResponse.model_construct(
        **sdk_skill(record).model_dump(mode="json", exclude_unset=True)
    )


async def publish_skill_version(
    client: AsyncAnthropic,
    skill_id: str,
    *,
    data: bytes,
    filename: str = "SKILL.zip",
    media_type: str = "application/zip",
    scope: Scope,
) -> VersionCreateResponse:
    backend = managed_agents(client, scope=scope, resources=frozenset({("skill", skill_id)}))
    record = await legacy_call(
        backend.skills.publish_version(
            scope,
            skill_id,
            SkillUpload(
                files=(SkillUploadFile(path=filename, content=data, media_type=media_type),)
            ),
            key=str(uuid4()),
        )
    )
    return VersionCreateResponse.model_construct(
        **sdk_skill_version(record).model_dump(mode="json", exclude_unset=True)
    )


async def list_skill_versions(
    client: AsyncAnthropic, skill_id: str, *, limit: int | None = None, scope: Scope
) -> AsyncIterator[VersionListResponse]:
    backend = managed_agents(client, scope=scope, resources=frozenset({("skill", skill_id)}))
    port = backend.extension(ResourceWalk, namespace="anthropic.resource_walk", version=1)
    async for record in legacy_iter(port.skill_versions(scope, skill_id, limit=limit)):
        yield sdk_skill_version(record)


async def download_skill_version(
    client: AsyncAnthropic, skill_id: str, version: str, *, scope: Scope
) -> bytes:
    backend = managed_agents(client, scope=scope, resources=frozenset({("skill", skill_id)}))
    port = backend.extension(SkillVersions, namespace="anthropic.skills_versions", version=1)
    return b"".join(
        [
            chunk
            async for chunk in legacy_iter(
                port.download(scope, SkillRef(id=skill_id, source="custom", version=version))
            )
        ]
    )


async def delete_skill_version(
    client: AsyncAnthropic, skill_id: str, version: str, *, scope: Scope
) -> None:
    backend = managed_agents(client, scope=scope, resources=frozenset({("skill", skill_id)}))
    port = backend.extension(SkillVersions, namespace="anthropic.skills_versions", version=1)
    await legacy_call(
        port.delete_version(
            scope,
            SkillRef(id=skill_id, source="custom", version=version),
            key=str(uuid4()),
        )
    )


async def delete_skill(client: AsyncAnthropic, skill_id: str, *, scope: Scope) -> None:
    backend = managed_agents(client, scope=scope, resources=frozenset({("skill", skill_id)}))
    await legacy_call(backend.skills.delete(scope, skill_id, key=str(uuid4())))


# PR3 host edge; append after existing skill helpers.


def _credential_refs(payload: Mapping[str, object]) -> tuple[dict[str, object], dict[str, str]]:
    """The host retains material; only opaque references enter driver DTOs."""
    secrets: dict[str, str] = {}
    secret_names = {"token", "access_token", "refresh_token", "client_secret", "secret_value"}

    def replace(node: object) -> object:
        if not isinstance(node, Mapping):
            return node
        result: dict[str, object] = {}
        for name, value in _mapping(cast(Mapping[str, object], node)).items():
            if name in secret_names:
                if value is None:
                    result[name + "_ref"] = None
                else:
                    if not isinstance(value, str):
                        raise TypeError("credential material must be text")
                    reference = str(uuid4())
                    secrets[reference] = value
                    result[name + "_ref"] = reference
            else:
                result[name] = replace(value)
        return result

    result = dict(payload)
    if "auth" in result:
        result["auth"] = replace(result["auth"])
    return result, secrets


def _vault_port(
    client: AsyncAnthropic,
    *,
    scope: Scope,
    vault_id: str | None = None,
    secrets: Mapping[str, str] | None = None,
) -> tuple[AnthropicManagedAgents, NativeVaults]:
    from mux.drivers.anthropic.resources.vaults import Vaults

    backend = managed_agents(
        client,
        scope=scope,
        resources=frozenset({("vault", vault_id)}) if vault_id is not None else frozenset(),
        secrets=(lambda _scope, reference: secrets[reference]) if secrets is not None else None,
    )
    return backend, backend.extension(Vaults, namespace="anthropic.vaults", version=1)


async def list_vaults(
    client: AsyncAnthropic, *, scope: Scope
) -> AsyncIterator[BetaManagedAgentsVault]:
    from anthropic.types.beta import BetaManagedAgentsVault

    _backend, port = _vault_port(client, scope=scope)
    async for record in legacy_iter(port.walk(scope)):
        yield BetaManagedAgentsVault.model_construct(_fields_set=None, **_mapping(record.native))


async def create_vault(
    client: AsyncAnthropic, display_name: str, *, scope: Scope
) -> BetaManagedAgentsVault:
    from anthropic.types.beta import BetaManagedAgentsVault

    _backend, port = _vault_port(client, scope=scope)
    record = await legacy_call(port.create(scope, display_name, key=str(uuid4())))
    return BetaManagedAgentsVault.model_construct(_fields_set=None, **_mapping(record.native))


async def archive_vault(client: AsyncAnthropic, vault_id: str, *, scope: Scope) -> None:
    backend, port = _vault_port(client, scope=scope, vault_id=vault_id)
    await legacy_call(
        port.archive(scope, resource_ref(backend, "vault", vault_id, scope=scope), key=str(uuid4()))
    )


async def list_credentials(
    client: AsyncAnthropic, vault_id: str, *, scope: Scope
) -> AsyncIterator[BetaManagedAgentsCredential]:
    from anthropic.types.beta.vaults.beta_managed_agents_credential import (
        BetaManagedAgentsCredential,
    )

    backend, port = _vault_port(client, scope=scope, vault_id=vault_id)
    async for record in legacy_iter(
        port.credential_walk(scope, resource_ref(backend, "vault", vault_id, scope=scope))
    ):
        yield BetaManagedAgentsCredential.model_construct(
            _fields_set=None, **_mapping(record.native)
        )


async def create_credential(
    client: AsyncAnthropic, vault_id: str, payload: Mapping[str, object], *, scope: Scope
) -> BetaManagedAgentsCredential:
    from anthropic.types.beta.vaults.beta_managed_agents_credential import (
        BetaManagedAgentsCredential,
    )
    from mux.drivers.anthropic.credential_schemas import CredentialCreate

    refs, secrets = _credential_refs(payload)
    try:
        backend, port = _vault_port(client, scope=scope, vault_id=vault_id, secrets=secrets)
        record = await legacy_call(
            port.create_credential(
                scope,
                resource_ref(backend, "vault", vault_id, scope=scope),
                CredentialCreate.model_validate(refs),
                key=str(uuid4()),
            )
        )
        return BetaManagedAgentsCredential.model_construct(
            _fields_set=None, **_mapping(record.native)
        )
    finally:
        secrets.clear()


async def store_credential(
    client: AsyncAnthropic, vault_id: str, payload: Mapping[str, object], *, scope: Scope
) -> None:
    """Keep the original write paths that discarded the SDK create response."""
    from mux.drivers.anthropic.credential_schemas import CredentialCreate

    refs, secrets = _credential_refs(payload)
    try:
        backend, port = _vault_port(client, scope=scope, vault_id=vault_id, secrets=secrets)
        await legacy_call(
            port.store_credential(
                scope,
                resource_ref(backend, "vault", vault_id, scope=scope),
                CredentialCreate.model_validate(refs),
                key=str(uuid4()),
            )
        )
    finally:
        secrets.clear()


async def update_credential(
    client: AsyncAnthropic,
    vault_id: str,
    credential_id: str,
    payload: Mapping[str, object],
    *,
    scope: Scope,
) -> BetaManagedAgentsCredential:
    from anthropic.types.beta.vaults.beta_managed_agents_credential import (
        BetaManagedAgentsCredential,
    )
    from mux.drivers.anthropic.credential_schemas import CredentialUpdate

    refs, secrets = _credential_refs(payload)
    try:
        backend, port = _vault_port(client, scope=scope, vault_id=vault_id, secrets=secrets)
        record = await legacy_call(
            port.update_credential(
                scope,
                resource_ref(backend, "vault", vault_id, scope=scope),
                credential_id,
                CredentialUpdate.model_validate(refs),
                key=str(uuid4()),
            )
        )
        return BetaManagedAgentsCredential.model_construct(
            _fields_set=None, **_mapping(record.native)
        )
    finally:
        secrets.clear()


async def delete_credential(
    client: AsyncAnthropic, vault_id: str, credential_id: str, *, scope: Scope
) -> None:
    backend, port = _vault_port(client, scope=scope, vault_id=vault_id)
    await legacy_call(
        port.remove(
            scope,
            resource_ref(backend, "vault", vault_id, scope=scope),
            credential_id,
            key=str(uuid4()),
        )
    )


async def upload_credential_file(
    client: AsyncAnthropic,
    content: bytes,
    *,
    filename: str,
    media_type: str,
    secret_values: list[str],
    scope: Scope,
) -> FileMetadata:
    # The host owns material; the closed driver spec holds only opaque refs.
    content_ref = str(uuid4())
    secrets = {content_ref: content.decode("utf-8")}
    value_refs: list[str] = []
    for value in secret_values:
        reference = str(uuid4())
        value_refs.append(reference)
        secrets[reference] = value
    try:
        backend = managed_agents(
            client, scope=scope, secrets=lambda _scope, reference: secrets[reference]
        )
        port = backend.extension(NativeArtifacts, namespace="anthropic.artifacts", version=1)
        config = CredentialFileUpload(
            filename=filename,
            media_type=media_type,
            content_ref=content_ref,
            secret_refs=tuple(value_refs),
        )
        record = await legacy_call(port.upload_credential_file(scope, config, key=str(uuid4())))
        return FileMetadata.model_construct(_fields_set=None, **_mapping(record.native))
    finally:
        secrets.clear()


async def rotate_session_repo_token(
    client: AsyncAnthropic, session_id: str, resource_id: str, token: str, *, scope: Scope
) -> None:
    reference = str(uuid4())
    secrets = {reference: token}
    try:
        backend = managed_agents(
            client,
            scope=scope,
            resources=frozenset({("session", session_id)}),
            secrets=lambda _scope, ref: secrets[ref],
        )
        await legacy_call(
            backend.session_admin.rotate_repo_token(
                scope,
                resource_ref(backend, "session", session_id, scope=scope),
                resource_id,
                reference,
                key=str(uuid4()),
            )
        )
    finally:
        secrets.clear()


async def archive_session(client: AsyncAnthropic, session_id: str, *, scope: Scope) -> None:
    backend = managed_agents(client, scope=scope, resources=frozenset({("session", session_id)}))
    await legacy_call(
        backend.session_admin.archive(
            scope, resource_ref(backend, "session", session_id, scope=scope), key=str(uuid4())
        )
    )
