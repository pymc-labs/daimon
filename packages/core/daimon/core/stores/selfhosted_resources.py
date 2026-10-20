"""Postgres resource ports for the self-hosted driver, injected by the host.

Each mutation commits its resource and receipt together. A tenant-level
transaction lock serializes keys and names across processes, including init.
No provider client or credential value is needed to manage these records.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from daimon.core._models import (
    SelfHostedAgentRecord,
    SelfHostedEnvironmentRecord,
    SelfHostedResourceOperation,
)
from mux.contracts.ids import Page, PageRequest, ResourceRef, Revision, Scope
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
)
from mux.errors import OperationConflict, ProviderError, ScopeViolation
from mux.profiles.selfhosted import LOOP
from mux.state.operations import request_digest
from pydantic import BaseModel, ConfigDict, JsonValue, TypeAdapter, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from ulid import ULID

_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class HarnessConfig(BaseModel):
    """Environment native_config.value; auth_ref always names a credential source."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    harness_url: str
    auth_mode: Literal["bearer_env", "entra"] = "bearer_env"
    auth_ref: str
    packages_note: str | None = None
    transport: Literal["direct", "foundry_invocations"] = "direct"
    agent_name: str | None = None
    project_endpoint: str | None = None

    @model_validator(mode="after")
    def validate_routing(self) -> HarnessConfig:
        url = urlsplit(self.harness_url)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
            raise ValueError("harness_url must be an HTTP URL without credentials")
        if url.query or url.fragment:
            raise ValueError("harness_url cannot contain a query or fragment")
        if not self.auth_ref.strip():
            raise ValueError("auth_ref must name an environment variable or Entra audience")
        if self.auth_mode == "bearer_env" and not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*", self.auth_ref
        ):
            raise ValueError("bearer_env auth_ref must be an environment variable name")
        if self.transport == "foundry_invocations":
            if not self.agent_name or not re.fullmatch(r"[A-Za-z0-9_.-]+", self.agent_name):
                raise ValueError("Foundry transport requires an agent_name without path separators")
            if not self.project_endpoint:
                raise ValueError("Foundry transport requires project_endpoint")
        if self.project_endpoint is not None:
            project = urlsplit(self.project_endpoint)
            if (
                project.scheme not in ("http", "https")
                or not project.hostname
                or project.username
                or project.password
                or project.query
                or project.fragment
            ):
                raise ValueError(
                    "project_endpoint must be HTTP without credentials, query or fragment"
                )
        return self


Row = SelfHostedAgentRecord | SelfHostedEnvironmentRecord


class _Resources[
    Spec: (AgentSpec, EnvironmentSpec),
    Patch: (AgentPatch, EnvironmentPatch),
    Result: (Agent, Environment),
]:
    table: type[SelfHostedAgentRecord] | type[SelfHostedEnvironmentRecord]
    spec_type: type[Spec]
    result_type: type[Result]
    kind: str

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self.sessionmaker = sessionmaker

    def _tenant(self, scope: Scope) -> UUID:
        if scope.is_platform or scope.is_legacy_host_authorized:
            raise ScopeViolation("selfhosted", "an explicit tenant scope is required")
        return UUID(scope.tenant_id)

    def _check(self, scope: Scope, ref: ResourceRef) -> None:
        self._tenant(scope)
        if (
            ref.provider != LOOP.provider
            or ref.kind != self.kind
            or ref.tenant_id != scope.tenant_id
            or ref.account_scope_id != scope.tenant_id
        ):
            raise ScopeViolation(ref.id, "resource differs from authorized tenant or provider")

    def _ref(self, scope: Scope, row: Row) -> ResourceRef:
        return ResourceRef(
            id=row.id,
            kind=self.kind,
            provider=LOOP.provider,
            account_scope_id=scope.tenant_id,
            tenant_id=scope.tenant_id,
        )

    def _result(self, scope: Scope, row: Row) -> Result:
        return self.result_type.model_validate(
            {
                "ref": self._ref(scope, row),
                "revision": Revision(local=row.revision),
                "spec": row.spec,
                "lifecycle": "archived" if row.archived_at else "active",
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "archived_at": row.archived_at,
            }
        )

    async def _lock(self, session: AsyncSession, scope: Scope) -> UUID:
        tenant = self._tenant(scope)
        # Stable hash inside Postgres; collisions only serialize unrelated tenants.
        await session.execute(
            select(
                func.pg_advisory_xact_lock(
                    func.hashtextextended("selfhosted-resources:" + str(tenant), 0)
                )
            )
        )
        return tenant

    async def _row(self, session: AsyncSession, scope: Scope, ref: ResourceRef) -> Row:
        self._check(scope, ref)
        row = await session.scalar(
            select(self.table).where(
                self.table.tenant_id == self._tenant(scope), self.table.id == ref.id
            )
        )
        if row is None:
            raise ProviderError("not_found", retryable=False)
        return row

    def _validate(self, spec: Spec) -> None:
        if not spec.name.strip():
            raise ValueError("resource name cannot be blank")
        if isinstance(spec, AgentSpec):
            if spec.model.provider != LOOP.provider:
                raise ValueError("agent model must use provider selfhosted")
            for server in spec.mcp_servers or ():
                if server.credential_ref and not re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_]*", server.credential_ref
                ):
                    raise ValueError("MCP credential_ref must be an environment variable name")
                url = urlsplit(server.url)
                if (
                    url.scheme not in ("http", "https")
                    or not url.hostname
                    or url.username
                    or url.password
                    or url.query
                    or url.fragment
                ):
                    raise ValueError("MCP URL must be HTTP without credentials, query or fragment")
        else:
            if spec.execution != "self_hosted" or spec.native_config is None:
                raise ValueError("self-hosted environment requires its harness configuration")
            if (
                spec.native_config.namespace != "selfhosted.environment"
                or spec.native_config.version != 1
            ):
                raise ValueError("unsupported self-hosted environment configuration")
            HarnessConfig.model_validate(dict(spec.native_config.value))

    async def _previous(
        self, session: AsyncSession, tenant: UUID, key: str, digest: str
    ) -> dict[str, JsonValue] | None:
        row = await session.get(SelfHostedResourceOperation, (tenant, key))
        if row is None:
            return None
        if row.digest != digest:
            raise OperationConflict(key)
        return row.result

    async def create(self, scope: Scope, spec: Spec, *, key: str) -> Result:
        self._validate(spec)
        digest = request_digest(
            _JSON.validate_python([self.kind, "create", spec.model_dump(mode="json")])
        )
        async with self.sessionmaker() as session, session.begin():
            tenant = await self._lock(session, scope)
            previous = await self._previous(session, tenant, key, digest)
            if previous is not None:
                return self.result_type.model_validate(previous)
            existing = await session.scalar(
                select(self.table.id).where(
                    self.table.tenant_id == tenant, self.table.name == spec.name
                )
            )
            if existing is not None:
                raise ProviderError("conflict", retryable=False)
            row = self.table(
                id=f"sh_{self.kind[:3] if self.kind == 'environment' else self.kind}_{ULID()}",
                tenant_id=tenant,
                name=spec.name,
                spec=spec.model_dump(mode="json"),
                revision=1,
            )
            session.add(row)
            await session.flush()
            result = self._result(scope, row)
            session.add(
                SelfHostedResourceOperation(
                    tenant_id=tenant, key=key, digest=digest, result=result.model_dump(mode="json")
                )
            )
            return result

    async def retrieve(self, scope: Scope, ref: ResourceRef) -> Result:
        async with self.sessionmaker() as session:
            return self._result(scope, await self._row(session, scope, ref))

    async def resolve(self, scope: Scope, name_or_id: str) -> Result:
        """Resolve an exact ID or tenant-local name; IDs never fall back to names."""
        async with self.sessionmaker() as session:
            field = self.table.id if name_or_id.startswith("sh_") else self.table.name
            row = await session.scalar(
                select(self.table).where(
                    self.table.tenant_id == self._tenant(scope), field == name_or_id
                )
            )
            if row is None:
                raise ProviderError("not_found", retryable=False)
            return self._result(scope, row)

    async def _list(
        self, scope: Scope, *, filters: AgentFilter | EnvironmentFilter, page: PageRequest
    ) -> Page[Result]:
        tenant = self._tenant(scope)
        query = select(self.table).where(self.table.tenant_id == tenant)
        if filters.name is not None:
            query = query.where(self.table.name == filters.name)
        if not filters.include_archived:
            query = query.where(self.table.archived_at.is_(None))
        if isinstance(filters, AgentFilter) and filters.created_after is not None:
            query = query.where(self.table.created_at > filters.created_after)
        descending = page.order == "desc"
        if page.cursor:
            query = query.where(
                self.table.id < page.cursor if descending else self.table.id > page.cursor
            )
        query = query.order_by(self.table.id.desc() if descending else self.table.id.asc())
        limit = page.limit or 100
        async with self.sessionmaker() as session:
            rows = list(await session.scalars(query.limit(limit + 1)))
            more = len(rows) > limit
            data = tuple(self._result(scope, row) for row in rows[:limit])
            return Page(data=data, has_more=more, next_cursor=rows[limit - 1].id if more else None)

    async def update(
        self, scope: Scope, ref: ResourceRef, patch: Patch, *, expected: Revision, key: str
    ) -> Result:
        self._check(scope, ref)
        digest = request_digest(
            _JSON.validate_python(
                [
                    self.kind,
                    "update",
                    ref.model_dump(mode="json"),
                    patch.model_dump(mode="json", exclude_unset=True),
                    expected.model_dump(mode="json"),
                ]
            )
        )
        async with self.sessionmaker() as session, session.begin():
            tenant = await self._lock(session, scope)
            previous = await self._previous(session, tenant, key, digest)
            if previous is not None:
                return self.result_type.model_validate(previous)
            row = await self._row(session, scope, ref)
            if expected != Revision(local=row.revision) or row.archived_at is not None:
                raise ProviderError("conflict", retryable=False)
            changes = patch.model_dump(mode="json", exclude_unset=True)
            spec_data = dict(row.spec)
            if "metadata" in changes:
                changes.pop("metadata")
                metadata = dict(self.spec_type.model_validate(row.spec).metadata or {})
                for name, value in (patch.metadata or {}).items():
                    if value is None:
                        metadata.pop(name, None)
                    else:
                        metadata[name] = value
                spec_data["metadata"] = metadata
            if "extensions" in changes:
                changes.pop("extensions")
                current = self.spec_type.model_validate(row.spec)
                if isinstance(current, AgentSpec):
                    extensions = {item.namespace: item for item in current.extensions or ()}
                    extensions.update(patch.extensions or {})
                    spec_data["extensions"] = [
                        item.model_dump(mode="json") for item in extensions.values()
                    ]
                elif patch.extensions:
                    raise ValueError("environment extensions must use native_config")
            spec_data.update(changes)
            spec = self.spec_type.model_validate(spec_data)
            self._validate(spec)
            if spec.name != row.name:
                exists = await session.scalar(
                    select(self.table.id).where(
                        self.table.tenant_id == tenant, self.table.name == spec.name
                    )
                )
                if exists is not None:
                    raise ProviderError("conflict", retryable=False)
            row.spec, row.name = spec.model_dump(mode="json"), spec.name
            row.revision += 1
            row.updated_at = datetime.now(UTC)
            result = self._result(scope, row)
            session.add(
                SelfHostedResourceOperation(
                    tenant_id=tenant, key=key, digest=digest, result=result.model_dump(mode="json")
                )
            )
            return result

    async def archive(self, scope: Scope, ref: ResourceRef, *, key: str) -> Operation:
        result = await self._remove(scope, ref, key=key, delete=False)
        return Operation.model_validate(result)

    async def delete(self, scope: Scope, ref: ResourceRef, *, key: str) -> DeletionReceipt:
        return DeletionReceipt.model_validate(await self._remove(scope, ref, key=key, delete=True))

    async def _remove(
        self, scope: Scope, ref: ResourceRef, *, key: str, delete: bool
    ) -> dict[str, JsonValue]:
        self._check(scope, ref)
        digest = request_digest(
            _JSON.validate_python(
                [self.kind, "delete" if delete else "archive", ref.model_dump(mode="json")]
            )
        )
        async with self.sessionmaker() as session, session.begin():
            tenant = await self._lock(session, scope)
            previous = await self._previous(session, tenant, key, digest)
            if previous is not None:
                return previous
            row = await self._row(session, scope, ref)
            now = datetime.now(UTC)
            if delete:
                await session.delete(row)
                result = DeletionReceipt(operation_id=str(ULID()), deleted=(ref,))
            else:
                if row.archived_at is None:
                    row.archived_at = row.updated_at = now
                    row.revision += 1
                result = Operation(
                    id=str(ULID()),
                    key=key,
                    request_digest=digest,
                    status="processed",
                    resource=ref,
                    created_at=now,
                    updated_at=now,
                )
            data = result.model_dump(mode="json")
            session.add(
                SelfHostedResourceOperation(tenant_id=tenant, key=key, digest=digest, result=data)
            )
            return data


class SelfHostedAgents(_Resources[AgentSpec, AgentPatch, Agent]):
    table = SelfHostedAgentRecord
    spec_type = AgentSpec
    result_type = Agent
    kind = "agent"

    async def list(self, scope: Scope, *, filters: AgentFilter, page: PageRequest) -> Page[Agent]:
        return await self._list(scope, filters=filters, page=page)


class SelfHostedEnvironments(_Resources[EnvironmentSpec, EnvironmentPatch, Environment]):
    table = SelfHostedEnvironmentRecord
    spec_type = EnvironmentSpec
    result_type = Environment
    kind = "environment"

    async def list(
        self, scope: Scope, *, filters: EnvironmentFilter, page: PageRequest
    ) -> Page[Environment]:
        return await self._list(scope, filters=filters, page=page)
