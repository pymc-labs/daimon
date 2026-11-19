"""Native cleanup edges with the existing SDK paginator and request arguments."""

from collections.abc import AsyncIterator
from typing import Protocol

from anthropic import AsyncAnthropic
from pydantic import JsonValue

from mux.contracts.ids import ResourceRef, Scope
from mux.drivers.anthropic.resources._authorization import (
    ResourceAuthorization,
    authorize,
    check_ref,
)
from mux.drivers.anthropic.resources._errors import provider_call, provider_iter
from mux.drivers.anthropic.resources._native import native_snapshot
from mux.drivers.anthropic.transport import mutation_client
from mux.errors import ScopeViolation


class CoreAdmin(Protocol):
    """anthropic.core_admin@1: scoped account purge and disposable-workspace walks."""

    def workspace_agents(
        self, scope: Scope, *, include_archived: bool = False
    ) -> AsyncIterator[dict[str, JsonValue]]: ...

    def workspace_environment_ids(self, scope: Scope) -> AsyncIterator[str]: ...

    async def workspace_has_resources(self, scope: Scope) -> bool: ...

    async def create_workspace_agent(
        self,
        scope: Scope,
        *,
        name: str,
        description: str,
        model: str,
        metadata: dict[str, str],
    ) -> None: ...

    def sessions_for_agent(
        self, scope: Scope, agent: ResourceRef
    ) -> AsyncIterator[dict[str, JsonValue]]: ...

    async def delete_session(self, scope: Scope, session: ResourceRef) -> None: ...

    async def interrupt_orphan(self, scope: Scope, session: ResourceRef) -> None: ...


class AnthropicCoreAdmin:
    def __init__(
        self,
        client: AsyncAnthropic,
        account_scope_id: str,
        authorization: ResourceAuthorization | None = None,
    ) -> None:
        self._client = client
        self._account_scope_id = account_scope_id
        self._authorization = authorization

    def _workspace(self, scope: Scope) -> None:
        if not scope.is_platform:
            raise ScopeViolation("workspace", "disposable workspace walks require platform scope")

    def _check(self, scope: Scope, ref: ResourceRef, kind: str) -> None:
        if scope.is_platform or scope.is_legacy_host_authorized:
            raise ScopeViolation(ref.id, "account cleanup requires tenant scope")
        authorize(self._authorization, scope, kind, ref.id)
        check_ref(scope, ref, self._account_scope_id, kind)

    async def workspace_agents(
        self, scope: Scope, *, include_archived: bool = False
    ) -> AsyncIterator[dict[str, JsonValue]]:
        self._workspace(scope)
        # Omit the archive filter unless asked, so existing cleanup requests keep their bytes.
        agents = (
            self._client.beta.agents.list(limit=100, include_archived=True)
            if include_archived
            else self._client.beta.agents.list(limit=100)
        )
        async for item in provider_iter(agents):
            yield native_snapshot(item).native

    async def workspace_environment_ids(self, scope: Scope) -> AsyncIterator[str]:
        self._workspace(scope)
        async for item in provider_iter(self._client.beta.environments.list(limit=100)):
            yield item.id

    async def workspace_has_resources(self, scope: Scope) -> bool:
        """True when any environment, vault or memory store exists, archived included."""
        self._workspace(scope)
        for pages in (
            self._client.beta.environments.list(limit=100, include_archived=True),
            self._client.beta.vaults.list(limit=100, include_archived=True),
            self._client.beta.memory_stores.list(limit=100, include_archived=True),
        ):
            async for _ in provider_iter(pages):
                return True
        return False

    async def create_workspace_agent(
        self,
        scope: Scope,
        *,
        name: str,
        description: str,
        model: str,
        metadata: dict[str, str],
    ) -> None:
        """Create a bare agent in the workspace (an idle ownership marker, not a host agent)."""
        self._workspace(scope)
        await provider_call(
            mutation_client(self._client).beta.agents.create(
                model=model, name=name, description=description, metadata=metadata
            )
        )

    async def sessions_for_agent(
        self, scope: Scope, agent: ResourceRef
    ) -> AsyncIterator[dict[str, JsonValue]]:
        self._check(scope, agent, "agent")
        async for item in provider_iter(self._client.beta.sessions.list(agent_id=agent.id)):
            yield native_snapshot(item).native

    async def delete_session(self, scope: Scope, session: ResourceRef) -> None:
        self._check(scope, session, "session")
        await provider_call(self._client.beta.sessions.delete(session.id))

    async def interrupt_orphan(self, scope: Scope, session: ResourceRef) -> None:
        self._check(scope, session, "session")
        await provider_call(
            mutation_client(self._client, single_attempt=True).beta.sessions.events.send(
                session.id, events=[{"type": "user.interrupt"}]
            )
        )
