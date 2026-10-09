"""Native recovery dump extension; no provider types leave these methods."""

from collections.abc import AsyncIterator, Mapping
from typing import Protocol, cast

from anthropic import AsyncAnthropic
from pydantic import JsonValue

from mux.contracts.ids import Page, PageRequest, Scope
from mux.contracts.ports import PlatformExport as CorePlatformExport
from mux.drivers.anthropic.resources._errors import provider_call, provider_iter
from mux.errors import ScopeViolation, UnsupportedCapability


class PlatformExport(CorePlatformExport, Protocol):
    """anthropic.platform_export@1, for authorized operator recovery exports."""

    def agents(self, scope: Scope) -> AsyncIterator[dict[str, JsonValue]]: ...
    async def agent(self, scope: Scope, agent_id: str) -> dict[str, JsonValue]: ...
    def environments(self, scope: Scope) -> AsyncIterator[dict[str, JsonValue]]: ...
    async def environment(self, scope: Scope, environment_id: str) -> dict[str, JsonValue]: ...
    async def skills_page(
        self, scope: Scope, *, page: PageRequest
    ) -> Page[dict[str, JsonValue]]: ...
    def skill_pages(
        self, scope: Scope, *, limit: int
    ) -> AsyncIterator[Page[dict[str, JsonValue]]]: ...
    def skill_versions(
        self, scope: Scope, skill_id: str
    ) -> AsyncIterator[dict[str, JsonValue]]: ...
    async def download_skill_version(self, scope: Scope, skill_id: str, version: str) -> bytes: ...
    def memory_stores(self, scope: Scope) -> AsyncIterator[dict[str, JsonValue]]: ...
    def memories(self, scope: Scope, store_id: str) -> AsyncIterator[dict[str, JsonValue]]: ...
    async def memory(self, scope: Scope, store_id: str, memory_id: str) -> dict[str, JsonValue]: ...


class AnthropicPlatformExport:
    """Read-only SDK walk preserving the old export's omitted query arguments.

    Scope is supplied by the host's operator authorization. This extension
    intentionally reads the provider workspace across tenants for recovery.
    """

    def __init__(self, client: AsyncAnthropic) -> None:
        self._client = client

    def _authorize(self, scope: Scope) -> None:
        if not scope.is_platform or scope.authorization_id != "platform-export":
            raise ScopeViolation("platform", "native export requires host operator authorization")

    async def agents(self, scope: Scope) -> AsyncIterator[dict[str, JsonValue]]:
        self._authorize(scope)
        async for item in provider_iter(self._client.beta.agents.list()):
            yield cast(dict[str, JsonValue], item.model_dump(mode="json"))

    async def agent(self, scope: Scope, agent_id: str) -> dict[str, JsonValue]:
        self._authorize(scope)
        item = await provider_call(self._client.beta.agents.retrieve(agent_id))
        return cast(dict[str, JsonValue], item.model_dump(mode="json"))

    async def environments(self, scope: Scope) -> AsyncIterator[dict[str, JsonValue]]:
        self._authorize(scope)
        async for item in provider_iter(self._client.beta.environments.list()):
            yield cast(dict[str, JsonValue], item.model_dump(mode="json"))

    async def environment(self, scope: Scope, environment_id: str) -> dict[str, JsonValue]:
        self._authorize(scope)
        item = await provider_call(self._client.beta.environments.retrieve(environment_id))
        return cast(dict[str, JsonValue], item.model_dump(mode="json"))

    async def skills_page(self, scope: Scope, *, page: PageRequest) -> Page[dict[str, JsonValue]]:
        self._authorize(scope)
        from anthropic.types.beta.skill_list_params import SkillListParams

        kwargs: dict[str, object] = {}
        if page.limit is not None:
            kwargs["limit"] = page.limit
        if page.cursor is not None:
            kwargs["page"] = page.cursor
        if page.order is not None:
            raise UnsupportedCapability(("skill_list_order",), "anthropic.managed_agents")
        result = await provider_call(self._client.beta.skills.list(**cast(SkillListParams, kwargs)))
        return Page(
            data=tuple(
                cast(dict[str, JsonValue], item.model_dump(mode="json"))
                for item in (result.data or ())
            ),
            next_cursor=result.next_page or None,
            has_more=bool(result.next_page),
        )

    async def skill_pages(
        self, scope: Scope, *, limit: int
    ) -> AsyncIterator[Page[dict[str, JsonValue]]]:
        self._authorize(scope)
        page = await provider_call(self._client.beta.skills.list(limit=limit))
        async for current in provider_iter(page.iter_pages()):
            yield Page(
                data=tuple(
                    cast(dict[str, JsonValue], item.model_dump(mode="json"))
                    for item in (current.data or ())
                ),
                next_cursor=current.next_page or None,
                has_more=bool(current.next_page),
            )

    async def skill_versions(
        self, scope: Scope, skill_id: str
    ) -> AsyncIterator[dict[str, JsonValue]]:
        self._authorize(scope)
        async for item in provider_iter(self._client.beta.skills.versions.list(skill_id)):
            yield cast(dict[str, JsonValue], item.model_dump(mode="json"))

    async def download_skill_version(self, scope: Scope, skill_id: str, version: str) -> bytes:
        self._authorize(scope)
        content = await provider_call(
            self._client.beta.skills.versions.download(version, skill_id=skill_id)
        )
        try:
            return await provider_call(content.read())
        finally:
            await provider_call(content.close())

    async def memory_stores(self, scope: Scope) -> AsyncIterator[dict[str, JsonValue]]:
        self._authorize(scope)
        async for item in provider_iter(self._client.beta.memory_stores.list()):
            yield cast(dict[str, JsonValue], item.model_dump(mode="json"))

    async def memories(self, scope: Scope, store_id: str) -> AsyncIterator[dict[str, JsonValue]]:
        self._authorize(scope)
        async for item in provider_iter(
            self._client.beta.memory_stores.memories.list(store_id, path_prefix="/")
        ):
            yield cast(dict[str, JsonValue], item.model_dump(mode="json"))

    async def memory(self, scope: Scope, store_id: str, memory_id: str) -> dict[str, JsonValue]:
        self._authorize(scope)
        item = await provider_call(
            self._client.beta.memory_stores.memories.retrieve(
                memory_id, memory_store_id=store_id, view="full"
            )
        )
        return cast(dict[str, JsonValue], item.model_dump(mode="json"))

    async def export(
        self, scope: Scope, *, resource_kinds: frozenset[str]
    ) -> Mapping[str, JsonValue]:
        self._authorize(scope)
        if resource_kinds - {"agents", "environments", "memory_stores"}:
            raise UnsupportedCapability(
                tuple(sorted(resource_kinds - {"agents", "environments", "memory_stores"})),
                "anthropic.managed_agents",
            )
        result: dict[str, JsonValue] = {}
        if "agents" in resource_kinds:
            result["agents"] = [
                await self.agent(scope, str(item["id"])) async for item in self.agents(scope)
            ]
        if "environments" in resource_kinds:
            result["environments"] = [
                await self.environment(scope, str(item["id"]))
                async for item in self.environments(scope)
            ]
        if "memory_stores" in resource_kinds:
            result["memory_stores"] = [item async for item in self.memory_stores(scope)]
        return result
