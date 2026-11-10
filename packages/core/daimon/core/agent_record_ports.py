"""Route agent and environment ports between Daimon-owned records and Managed Agents.

A record is Daimon-owned when the tenant's row exists in the Daimon tables; its
calls never reach Anthropic. Every other id keeps today's Managed Agents request.
Only Daimon ids (`sh_`, `dm_` prefixes) are looked up, so MA calls add no reads.
Lists and walks return both sources; with Anthropic disabled they return Daimon only.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import cast
from uuid import UUID

from anthropic import AsyncAnthropic
from daimon.core.agent_record_facts import agent_facts, environment_facts
from daimon.core.anthropic_guard import anthropic_disabled, require_anthropic_allowed
from daimon.core.stores.selfhosted_resources import SelfHostedAgents, SelfHostedEnvironments
from mux.contracts.ids import Page, PageRequest, ResourceRef, Revision, Scope
from mux.contracts.ports import Artifacts, Events, Models, Sessions, Skills, Usage
from mux.contracts.receipts import DeletionReceipt, Operation
from mux.contracts.resources import (
    Agent,
    AgentFilter,
    AgentPatch,
    AgentSpec,
    Environment,
    EnvironmentFilter,
    EnvironmentPatch,
    EnvironmentSpec,
    Skill,
    SkillVersion,
)
from mux.drivers.anthropic import (
    AGENT_VERSIONS_NAMESPACE,
    AgentVersions,
    AnthropicAgents,
    AnthropicEnvironments,
    AnthropicManagedAgents,
    EnvironmentReads,
    NativeSnapshot,
    ResourceAuthorization,
    ResourceWalk,
    SecretResolver,
)
from mux.errors import ProviderError, ScopeViolation, UnsupportedCapability
from mux.profiles.selfhosted import LOOP
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

DAIMON_ID_PREFIXES = ("sh_agent_", "sh_env_", "dm_agent_", "dm_env_")
_DAIMON_CURSOR = "dm:"
_WALK = (ResourceWalk, "anthropic.resource_walk", 1)
_RECORD_PORTS: frozenset[type[object]] = frozenset({AgentVersions, EnvironmentReads, ResourceWalk})


def _tenant_scoped(scope: Scope) -> bool:
    if scope.is_platform or scope.is_legacy_host_authorized:
        return False
    try:
        UUID(scope.tenant_id)
    except ValueError:
        return False
    return True


def _authorize(
    authorization: ResourceAuthorization | None, scope: Scope, kind: str, resource_id: str | None
) -> None:
    """The same host decision the MA ports enforce, for tenant scopes."""
    if authorization is None:
        raise ScopeViolation(resource_id or kind, "resource access requires host authorization")
    authorization.check(scope, kind, resource_id)


async def _local_ref(
    store: SelfHostedAgents | SelfHostedEnvironments,
    authorization: ResourceAuthorization | None,
    scope: Scope,
    ref: ResourceRef,
) -> ResourceRef | None:
    """The store's ref for a Daimon-owned id, or None when Managed Agents serves it."""
    if ref.provider != LOOP.provider and not ref.id.startswith(DAIMON_ID_PREFIXES):
        return None
    if not _tenant_scoped(scope):
        if ref.provider == LOOP.provider:
            raise ScopeViolation(ref.id, "Daimon records need a tenant scope")
        return None
    _authorize(authorization, scope, store.kind, ref.id)
    if ref.kind != store.kind or not await store.owns(scope, ref.id):
        raise ProviderError("not_found", retryable=False)
    return store.record_ref(scope, ref.id)


def _agent(scope: Scope, agent: Agent) -> Agent:
    native = agent_facts(agent, tenant_id=scope.tenant_id).model_dump(mode="json")
    return agent.model_copy(update={"native": native})


def _environment(scope: Scope, environment: Environment) -> Environment:
    native = environment_facts(environment, tenant_id=scope.tenant_id).model_dump(mode="json")
    return environment.model_copy(update={"native": native})


async def _merged_page[R: (Agent, Environment)](
    scope: Scope,
    page: PageRequest,
    *,
    remote: Callable[[], Awaitable[Page[R]]],
    owned: Callable[[PageRequest], Awaitable[Page[R]]],
    wrap: Callable[[Scope, R], R],
    authorize: Callable[[], None],
) -> Page[R]:
    """MA pages first with their own cursors, then Daimon pages behind `dm:` cursors."""
    cursor = page.cursor
    resumed = cursor is not None and cursor.startswith(_DAIMON_CURSOR)
    data: tuple[R, ...] = ()
    if not _tenant_scoped(scope) or (not resumed and (cursor or not anthropic_disabled())):
        require_anthropic_allowed()
        listed = await remote()
        if not _tenant_scoped(scope) or listed.has_more:
            return listed
        data, cursor = listed.data, None
    else:
        authorize()
        cursor = cursor[len(_DAIMON_CURSOR) :] if cursor else None
    remaining = None if page.limit is None else page.limit - len(data)
    if remaining == 0:
        return Page(data=data, has_more=True, next_cursor=_DAIMON_CURSOR)
    local = await owned(PageRequest(cursor=cursor or None, limit=remaining))
    return Page(
        data=data + tuple(wrap(scope, item) for item in local.data),
        has_more=local.has_more,
        next_cursor=_DAIMON_CURSOR + local.next_cursor if local.next_cursor else None,
    )


async def _merged_walk[R: (Agent, Environment)](
    scope: Scope,
    *,
    remote: Callable[[], AsyncIterator[R]],
    owned: Callable[[PageRequest], Awaitable[Page[R]]],
    wrap: Callable[[Scope, R], R],
    authorize: Callable[[], None],
) -> AsyncIterator[R]:
    if not _tenant_scoped(scope) or not anthropic_disabled():
        require_anthropic_allowed()
        async for item in remote():
            yield item
    if not _tenant_scoped(scope):
        return
    authorize()
    cursor: str | None = None
    while True:
        local = await owned(PageRequest(cursor=cursor))
        for item in local.data:
            yield wrap(scope, item)
        if not local.next_cursor:
            return
        cursor = local.next_cursor


class RoutedAgents(AnthropicAgents):
    """`Agents` and `AgentVersions`; MA calls are the parent's, unchanged."""

    def __init__(
        self,
        client: AsyncAnthropic,
        account_scope_id: str,
        authorization: ResourceAuthorization | None,
        store: SelfHostedAgents,
    ) -> None:
        super().__init__(client, account_scope_id, authorization)
        self._store = store
        self._grant = authorization

    async def create_owned(self, scope: Scope, spec: AgentSpec, *, key: str) -> Agent:
        """Create a Daimon-owned agent; `create` stays Managed Agents."""
        _authorize(self._grant, scope, "agent", None)
        return _agent(scope, await self._store.create(scope, spec, key=key))

    async def create(self, scope: Scope, spec: AgentSpec, *, key: str) -> Agent:
        require_anthropic_allowed()
        return await super().create(scope, spec, key=key)

    async def retrieve(self, scope: Scope, ref: ResourceRef) -> Agent:
        local = await _local_ref(self._store, self._grant, scope, ref)
        if local is not None:
            return _agent(scope, await self._store.retrieve(scope, local))
        require_anthropic_allowed()
        return await super().retrieve(scope, ref)

    async def retrieve_version(
        self, scope: Scope, ref: ResourceRef, version: int
    ) -> NativeSnapshot:
        local = await _local_ref(self._store, self._grant, scope, ref)
        if local is not None:
            stored = await self._store.retrieve_version(scope, local, version)
            facts = agent_facts(stored, tenant_id=scope.tenant_id)
            return NativeSnapshot(native=facts.model_dump(mode="json"))
        require_anthropic_allowed()
        return await super().retrieve_version(scope, ref, version)

    async def list_versions(
        self, scope: Scope, ref: ResourceRef, *, page: PageRequest
    ) -> Page[Agent]:
        """Daimon-owned agents only; Managed Agents has no version list here."""
        local = await _local_ref(self._store, self._grant, scope, ref)
        if local is None:
            raise UnsupportedCapability(("agent_version_list",), "anthropic.managed_agents")
        listed = await self._store.list_versions(scope, local, page=page)
        return listed.model_copy(update={"data": tuple(_agent(scope, a) for a in listed.data)})

    async def list(self, scope: Scope, *, filters: AgentFilter, page: PageRequest) -> Page[Agent]:
        return await _merged_page(
            scope,
            page,
            remote=lambda: super(RoutedAgents, self).list(scope, filters=filters, page=page),
            owned=lambda local: self._store.list(scope, filters=filters, page=local),
            wrap=_agent,
            authorize=lambda: _authorize(self._grant, scope, "agent", None),
        )

    async def update(
        self, scope: Scope, ref: ResourceRef, patch: AgentPatch, *, expected: Revision, key: str
    ) -> Agent:
        local = await _local_ref(self._store, self._grant, scope, ref)
        if local is not None:
            updated = await self._store.update(
                scope, local, patch, expected=Revision(local=expected.local), key=key
            )
            return _agent(scope, updated)
        require_anthropic_allowed()
        return await super().update(scope, ref, patch, expected=expected, key=key)

    async def archive(self, scope: Scope, ref: ResourceRef, *, key: str) -> Operation:
        local = await _local_ref(self._store, self._grant, scope, ref)
        if local is not None:
            return await self._store.archive(scope, local, key=key)
        require_anthropic_allowed()
        return await super().archive(scope, ref, key=key)

    async def delete(self, scope: Scope, ref: ResourceRef, *, key: str) -> DeletionReceipt:
        local = await _local_ref(self._store, self._grant, scope, ref)
        if local is not None:
            return await self._store.delete(scope, local, key=key)
        require_anthropic_allowed()
        return await super().delete(scope, ref, key=key)

    def walk(self, scope: Scope, *, filters: AgentFilter) -> AsyncIterator[Agent]:
        return _merged_walk(
            scope,
            remote=lambda: super(RoutedAgents, self).walk(scope, filters=filters),
            owned=lambda local: self._store.list(scope, filters=filters, page=local),
            wrap=_agent,
            authorize=lambda: _authorize(self._grant, scope, "agent", None),
        )


class RoutedEnvironments(AnthropicEnvironments):
    """`Environments` and `EnvironmentReads`; MA calls are the parent's, unchanged."""

    def __init__(
        self,
        client: AsyncAnthropic,
        account_scope_id: str,
        authorization: ResourceAuthorization | None,
        store: SelfHostedEnvironments,
    ) -> None:
        super().__init__(client, account_scope_id, authorization)
        self._store = store
        self._grant = authorization

    async def create_owned(self, scope: Scope, spec: EnvironmentSpec, *, key: str) -> Environment:
        """Create a Daimon-owned environment; `create` stays Managed Agents."""
        _authorize(self._grant, scope, "environment", None)
        return _environment(scope, await self._store.create(scope, spec, key=key))

    async def create(self, scope: Scope, spec: EnvironmentSpec, *, key: str) -> Environment:
        require_anthropic_allowed()
        return await super().create(scope, spec, key=key)

    async def retrieve(self, scope: Scope, ref: ResourceRef) -> Environment:
        local = await _local_ref(self._store, self._grant, scope, ref)
        if local is not None:
            return _environment(scope, await self._store.retrieve(scope, local))
        require_anthropic_allowed()
        return await super().retrieve(scope, ref)

    async def retrieve_native(self, scope: Scope, ref: ResourceRef) -> NativeSnapshot:
        local = await _local_ref(self._store, self._grant, scope, ref)
        if local is not None:
            stored = await self._store.retrieve(scope, local)
            facts = environment_facts(stored, tenant_id=scope.tenant_id)
            return NativeSnapshot(native=facts.model_dump(mode="json"))
        require_anthropic_allowed()
        return await super().retrieve_native(scope, ref)

    async def list(
        self, scope: Scope, *, filters: EnvironmentFilter, page: PageRequest
    ) -> Page[Environment]:
        return await _merged_page(
            scope,
            page,
            remote=lambda: super(RoutedEnvironments, self).list(scope, filters=filters, page=page),
            owned=lambda local: self._store.list(scope, filters=filters, page=local),
            wrap=_environment,
            authorize=lambda: _authorize(self._grant, scope, "environment", None),
        )

    async def update(
        self,
        scope: Scope,
        ref: ResourceRef,
        patch: EnvironmentPatch,
        *,
        expected: Revision,
        key: str,
    ) -> Environment:
        local = await _local_ref(self._store, self._grant, scope, ref)
        if local is not None:
            # MA environments are unversioned and callers send revision 0 for "current".
            current = (
                Revision(local=expected.local)
                if expected.local
                else (await self._store.retrieve(scope, local)).revision
            )
            updated = await self._store.update(scope, local, patch, expected=current, key=key)
            return _environment(scope, updated)
        require_anthropic_allowed()
        return await super().update(scope, ref, patch, expected=expected, key=key)

    async def archive(self, scope: Scope, ref: ResourceRef, *, key: str) -> Operation:
        local = await _local_ref(self._store, self._grant, scope, ref)
        if local is not None:
            return await self._store.archive(scope, local, key=key)
        require_anthropic_allowed()
        return await super().archive(scope, ref, key=key)

    async def delete(self, scope: Scope, ref: ResourceRef, *, key: str) -> DeletionReceipt:
        local = await _local_ref(self._store, self._grant, scope, ref)
        if local is not None:
            return await self._store.delete(scope, local, key=key)
        require_anthropic_allowed()
        return await super().delete(scope, ref, key=key)

    def walk(self, scope: Scope, *, filters: EnvironmentFilter) -> AsyncIterator[Environment]:
        return _merged_walk(
            scope,
            remote=lambda: super(RoutedEnvironments, self).walk(scope, filters=filters),
            owned=lambda local: self._store.list(scope, filters=filters, page=local),
            wrap=_environment,
            authorize=lambda: _authorize(self._grant, scope, "environment", None),
        )


class RoutedResourceWalk:
    """Agent and environment walks merge both sources; skill walks stay MA."""

    def __init__(
        self, agents: RoutedAgents, environments: RoutedEnvironments, ma: ResourceWalk
    ) -> None:
        self._agents = agents
        self._environments = environments
        self._ma = ma

    def agents(self, scope: Scope, *, filters: AgentFilter) -> AsyncIterator[Agent]:
        return self._agents.walk(scope, filters=filters)

    def environments(
        self, scope: Scope, *, filters: EnvironmentFilter
    ) -> AsyncIterator[Environment]:
        return self._environments.walk(scope, filters=filters)

    def skill_pages(self, scope: Scope, *, limit: int) -> AsyncIterator[Page[Skill]]:
        require_anthropic_allowed()
        return self._ma.skill_pages(scope, limit=limit)

    def skill_versions(
        self, scope: Scope, skill_id: str, *, limit: int | None = None
    ) -> AsyncIterator[SkillVersion]:
        require_anthropic_allowed()
        return self._ma.skill_versions(scope, skill_id, limit=limit)


class RecordRoutedManagedAgents(AnthropicManagedAgents):
    """The MA backend with agent records routed; other ports refuse when Anthropic is off.

    Create Daimon-owned records with `agents.create_owned` and
    `environments.create_owned`; `create` targets Managed Agents as before.
    """

    def __init__(
        self,
        client: AsyncAnthropic,
        *,
        records: async_sessionmaker[AsyncSession],
        authorization: ResourceAuthorization | None = None,
        secrets: SecretResolver | None = None,
    ) -> None:
        super().__init__(client, authorization=authorization, secrets=secrets)
        self.routed_agents = RoutedAgents(
            client, self.account_scope_id, authorization, SelfHostedAgents(records)
        )
        self.routed_environments = RoutedEnvironments(
            client, self.account_scope_id, authorization, SelfHostedEnvironments(records)
        )
        self.agents = self.routed_agents
        self.environments = self.routed_environments
        ma_walk = cast(ResourceWalk, self._extensions[_WALK])
        self._extensions[_WALK] = RoutedResourceWalk(
            self.routed_agents, self.routed_environments, ma_walk
        )
        self._extensions[(AgentVersions, AGENT_VERSIONS_NAMESPACE, 1)] = self.routed_agents
        self._extensions[(EnvironmentReads, "anthropic.environment_reads", 1)] = (
            self.routed_environments
        )

    def extension[T](self, port: type[T], *, namespace: str, version: int) -> T:
        implementation = super().extension(port, namespace=namespace, version=version)
        if port not in _RECORD_PORTS:
            require_anthropic_allowed()
        return implementation

    @property
    def sessions(self) -> Sessions:
        require_anthropic_allowed()
        return super().sessions

    @property
    def events(self) -> Events:
        require_anthropic_allowed()
        return super().events

    @property
    def artifacts(self) -> Artifacts:
        require_anthropic_allowed()
        return super().artifacts

    @property
    def skills(self) -> Skills:
        require_anthropic_allowed()
        return super().skills

    @property
    def models(self) -> Models:
        require_anthropic_allowed()
        return super().models

    @property
    def usage(self) -> Usage:
        require_anthropic_allowed()
        return super().usage
