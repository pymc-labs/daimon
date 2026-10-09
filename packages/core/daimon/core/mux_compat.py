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
    SkillCreateResponse,
    SkillListResponse,
)
from anthropic.types.beta.skills import VersionCreateResponse, VersionListResponse
from daimon.core.mux_backend import managed_agents, resource_ref, resource_scope
from mux.contracts.ids import PageRequest, Revision, SkillRef
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
from mux.drivers.anthropic.resources.walk import ResourceWalk
from mux.errors import ProviderError

if TYPE_CHECKING:
    from mux.contracts.resources import Skill, SkillVersion


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
        if value is not None:
            extensions["anthropic.multiagent"] = {
                "namespace": "anthropic.multiagent",
                "version": 1,
                "value": value,
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
    if "extensions" in values:
        values["extensions"] = tuple(_mapping(values["extensions"]).values())
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
    return EnvironmentSpec.model_validate(_environment_values(payload))


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
    client: AsyncAnthropic, *, include_archived: bool = False
) -> AsyncIterator[BetaManagedAgentsAgent]:
    backend = managed_agents(client)
    port = backend.extension(ResourceWalk, namespace="anthropic.resource_walk", version=1)
    async for record in legacy_iter(
        port.agents(resource_scope(), filters=AgentFilter(include_archived=include_archived))
    ):
        yield sdk_agent(record)


async def list_environments(
    client: AsyncAnthropic, *, include_archived: bool = False
) -> AsyncIterator[BetaEnvironment]:
    backend = managed_agents(client)
    port = backend.extension(ResourceWalk, namespace="anthropic.resource_walk", version=1)
    async for record in legacy_iter(
        port.environments(
            resource_scope(), filters=EnvironmentFilter(include_archived=include_archived)
        )
    ):
        yield sdk_environment(record)


async def retrieve_agent(client: AsyncAnthropic, agent_id: str) -> BetaManagedAgentsAgent:
    backend = managed_agents(client)
    return sdk_agent(
        await legacy_call(
            backend.agents.retrieve(resource_scope(), resource_ref(backend, "agent", agent_id))
        )
    )


async def create_agent(
    client: AsyncAnthropic, payload: Mapping[str, object]
) -> BetaManagedAgentsAgent:
    backend = managed_agents(client)
    return sdk_agent(
        await legacy_call(
            backend.agents.create(resource_scope(), agent_spec(payload), key=str(uuid4()))
        )
    )


async def update_agent(
    client: AsyncAnthropic, agent_id: str, *, version: int, payload: Mapping[str, object]
) -> BetaManagedAgentsAgent:
    backend = managed_agents(client)
    return sdk_agent(
        await legacy_call(
            backend.agents.update(
                resource_scope(),
                resource_ref(backend, "agent", agent_id),
                agent_patch(payload),
                expected=Revision(local=version, native=str(version)),
                key=str(uuid4()),
            )
        )
    )


async def archive_agent(client: AsyncAnthropic, agent_id: str) -> None:
    backend = managed_agents(client)
    await legacy_call(
        backend.agents.archive(
            resource_scope(), resource_ref(backend, "agent", agent_id), key=str(uuid4())
        )
    )


async def archive_environment(client: AsyncAnthropic, environment_id: str) -> None:
    backend = managed_agents(client)
    await legacy_call(
        backend.environments.archive(
            resource_scope(), resource_ref(backend, "environment", environment_id), key=str(uuid4())
        )
    )


async def create_environment(
    client: AsyncAnthropic, payload: Mapping[str, object]
) -> BetaEnvironment:
    backend = managed_agents(client)
    return sdk_environment(
        await legacy_call(
            backend.environments.create(
                resource_scope(), environment_spec(payload), key=str(uuid4())
            )
        )
    )


async def update_environment(
    client: AsyncAnthropic, environment_id: str, payload: Mapping[str, object]
) -> BetaEnvironment:
    backend = managed_agents(client)
    return sdk_environment(
        await legacy_call(
            backend.environments.update(
                resource_scope(),
                resource_ref(backend, "environment", environment_id),
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
    client: AsyncAnthropic, *, limit: int
) -> tuple[list[SkillListResponse], bool]:
    backend = managed_agents(client)
    scope = resource_scope()
    cursor: str | None = None
    rows: list[SkillListResponse] = []
    truncated = False
    while True:
        page = await legacy_call(
            backend.skills.list(scope, page=PageRequest(cursor=cursor, limit=limit))
        )
        rows.extend(sdk_skill(record) for record in page.data)
        truncated = truncated or (len(page.data) >= limit and not page.has_more)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    return rows, truncated


async def create_skill(
    client: AsyncAnthropic,
    *,
    display_title: str,
    data: bytes,
    filename: str = "SKILL.zip",
    media_type: str = "application/zip",
) -> SkillCreateResponse:
    backend = managed_agents(client)
    record = await legacy_call(
        backend.skills.create(
            resource_scope(),
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
) -> VersionCreateResponse:
    backend = managed_agents(client)
    record = await legacy_call(
        backend.skills.publish_version(
            resource_scope(),
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
    client: AsyncAnthropic, skill_id: str, *, limit: int | None = None
) -> AsyncIterator[VersionListResponse]:
    backend = managed_agents(client)
    port = backend.extension(SkillVersions, namespace="anthropic.skills_versions", version=1)
    scope = resource_scope()
    cursor: str | None = None
    while True:
        page = await legacy_call(
            port.versions(scope, skill_id, page=PageRequest(cursor=cursor, limit=limit))
        )
        for record in page.data:
            yield sdk_skill_version(record)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor


async def download_skill_version(client: AsyncAnthropic, skill_id: str, version: str) -> bytes:
    backend = managed_agents(client)
    port = backend.extension(SkillVersions, namespace="anthropic.skills_versions", version=1)
    return b"".join(
        [
            chunk
            async for chunk in legacy_iter(
                port.download(
                    resource_scope(), SkillRef(id=skill_id, source="custom", version=version)
                )
            )
        ]
    )


async def delete_skill_version(client: AsyncAnthropic, skill_id: str, version: str) -> None:
    backend = managed_agents(client)
    port = backend.extension(SkillVersions, namespace="anthropic.skills_versions", version=1)
    await legacy_call(
        port.delete_version(
            resource_scope(),
            SkillRef(id=skill_id, source="custom", version=version),
            key=str(uuid4()),
        )
    )


async def delete_skill(client: AsyncAnthropic, skill_id: str) -> None:
    backend = managed_agents(client)
    await legacy_call(backend.skills.delete(resource_scope(), skill_id, key=str(uuid4())))
