"""Tenant-scoped Postgres resource ports for injection into the self-hosted driver.

Mutations and reference-only receipts commit together. Tenant transaction
locks serialize key/name races; receipt ownership matches mux operation scope.
Receipts expire after seven days and retries never return deleted resources.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core._models import (
    Account,
    NeutralSkillVersion,
    SelfHostedAgentRecord,
    SelfHostedEnvironmentRecord,
    SelfHostedResourceOperation,
    Tenant,
)
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.stores.privacy_session_deletes import lock_account
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
from mux.drivers.selfhosted import validate_resource_spec
from mux.errors import OperationConflict, ProviderError, ScopeViolation
from mux.profiles.selfhosted import LOOP
from mux.state.operations import request_digest
from pydantic import JsonValue, TypeAdapter, ValidationError
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from ulid import ULID

_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
Row = SelfHostedAgentRecord | SelfHostedEnvironmentRecord


async def list_live_agent_facts(
    session: AsyncSession, *, tenant_id: UUID
) -> list[BetaManagedAgentsAgent]:
    """Local policy facts in the shape used by routing and isolation readers.

    Include every live record, without a page limit. Reserved metadata comes
    from the row's tenant and configured name, never caller-supplied aliases.
    These facts contain no provider-managed tools or skills.
    """
    rows = await session.scalars(
        select(SelfHostedAgentRecord).where(
            SelfHostedAgentRecord.tenant_id == tenant_id,
            SelfHostedAgentRecord.archived_at.is_(None),
        )
    )
    facts: list[BetaManagedAgentsAgent] = []
    for row in rows:
        spec = AgentSpec.model_validate(row.spec)
        facts.append(
            BetaManagedAgentsAgent.model_validate(
                {
                    "id": row.id,
                    "type": "agent",
                    "name": spec.name,
                    "version": row.revision,
                    "model": {"id": spec.model.id},
                    "system": spec.system,
                    "description": spec.description,
                    "metadata": {
                        **dict(spec.metadata or {}),
                        MA_METADATA_KEY_NAME: spec.name,
                        MA_METADATA_KEY_TENANT: str(tenant_id),
                    },
                    "tools": [],
                    "mcp_servers": [],
                    "skills": [],
                    "created_at": row.created_at,
                    "updated_at": row.updated_at,
                    "archived_at": None,
                }
            )
        )
    return facts


async def _skill_attachments(
    session: AsyncSession,
    scope: Scope,
    spec: AgentSpec,
    previous: list[dict[str, str]],
    previous_spec: AgentSpec | None,
) -> list[dict[str, str]]:
    """Keep provenance; authorize only pins introduced by this write."""
    previous_pins: set[tuple[str, str | None]] = (
        {(ref.id, ref.version) for ref in (previous_spec.skills or ())}
        if previous_spec is not None
        else set()
    )
    attachments: list[dict[str, str]] = []
    for ref in spec.skills or ():
        if ref.version is None:
            continue
        old = next(
            (
                item
                for item in previous
                if (item["skill_id"], item["version"]) == (ref.id, ref.version)
            ),
            None,
        )
        if old is not None:
            attachments.append(old)
            continue
        # An unchanged but unattached pin must stay unattached. Another editor
        # may have added it without the current writer's authorization.
        if (ref.id, ref.version) in previous_pins:
            continue
        # Operator-created seeded refs have no personal catalog ownership.
        if scope.account_id == "operator":
            continue
        retained = await session.scalar(
            select(NeutralSkillVersion).where(
                NeutralSkillVersion.tenant_id == UUID(scope.tenant_id),
                NeutralSkillVersion.account_id == UUID(scope.account_id),
                NeutralSkillVersion.provider == "selfhosted",
                NeutralSkillVersion.account_scope_id == scope.tenant_id,
                NeutralSkillVersion.skill_id == ref.id,
                NeutralSkillVersion.version == ref.version,
                NeutralSkillVersion.principal_id == scope.principal_id,
            )
        )
        if retained is not None:
            attachments.append(
                {
                    "skill_id": ref.id,
                    "version": ref.version,
                    "account_id": str(retained.account_id),
                    "principal_id": retained.principal_id,
                }
            )
    return attachments


async def cleanup_expired_receipts(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    now: datetime | None = None,
) -> int:
    """Operator cleanup for idle tenants; writes also prune their tenant's expired receipts."""
    async with sessionmaker() as session, session.begin():
        removed = await session.scalars(
            delete(SelfHostedResourceOperation)
            .where(SelfHostedResourceOperation.expires_at <= (now or datetime.now(UTC)))
            .returning(SelfHostedResourceOperation.key)
        )
        return len(list(removed))


async def count_operations_for_account(session: AsyncSession, *, account_id: UUID) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(SelfHostedResourceOperation)
            .where(SelfHostedResourceOperation.account_id == str(account_id))
        )
        or 0
    )


async def delete_operations_for_account(session: AsyncSession, *, account_id: UUID) -> int:
    removed = await session.scalars(
        delete(SelfHostedResourceOperation)
        .where(SelfHostedResourceOperation.account_id == str(account_id))
        .returning(SelfHostedResourceOperation.key)
    )
    return len(list(removed))


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
        try:
            return UUID(scope.tenant_id)
        except ValueError:
            raise ScopeViolation("selfhosted", "tenant scope must contain a UUID") from None

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
        try:
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
        except ValidationError:
            raise ProviderError("invalid_request", retryable=False) from None

    @asynccontextmanager
    async def _write(self, scope: Scope) -> AsyncIterator[tuple[AsyncSession, UUID]]:
        try:
            async with self.sessionmaker() as session, session.begin():
                tenant = self._tenant(scope)
                # Bootstrap runs as the deployment operator, without a personal
                # account. Every personal writer coordinates with erasure before
                # taking tenant locks or creating any resource/receipt rows.
                operator = (
                    scope.account_id == "operator"
                    and scope.principal_id == "cli"
                    and scope.authorization_id == "cli-deployment-operator"
                )
                if not operator:
                    try:
                        account_id = UUID(scope.account_id)
                    except ValueError:
                        raise ScopeViolation("selfhosted", "an account UUID is required") from None
                    await lock_account(session, account_id=account_id)
                    account = await session.scalar(
                        select(Account.id)
                        .where(Account.id == account_id)
                        .with_for_update(read=True, key_share=True)
                    )
                    if account is None:
                        raise ScopeViolation("selfhosted", "account no longer exists")
                await session.execute(
                    select(
                        func.pg_advisory_xact_lock(
                            func.hashtextextended("selfhosted-resources:" + str(tenant), 0)
                        )
                    )
                )
                exists = await session.scalar(
                    select(Tenant.id)
                    .where(Tenant.id == tenant, Tenant.archived_at.is_(None))
                    .with_for_update(read=True, key_share=True)
                )
                if exists is None:
                    raise ProviderError("not_found", retryable=False)
                await session.execute(
                    delete(SelfHostedResourceOperation).where(
                        SelfHostedResourceOperation.tenant_id == tenant,
                        SelfHostedResourceOperation.expires_at <= func.now(),
                    )
                )
                yield session, tenant
        except IntegrityError:
            raise ProviderError("conflict", retryable=False) from None

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

    async def _previous(
        self, session: AsyncSession, scope: Scope, key: str, digest: str
    ) -> SelfHostedResourceOperation | None:
        row = await session.get(
            SelfHostedResourceOperation,
            (self._tenant(scope), self._account_key(scope), self.kind, key),
        )
        if row is None:
            return None
        if row.principal_id != scope.principal_id:
            raise ScopeViolation(row.operation_id, "operation belongs to another principal")
        if row.digest != digest:
            raise OperationConflict(key)
        return row

    async def _record(
        self,
        session: AsyncSession,
        scope: Scope,
        key: str,
        digest: str,
        action: str,
        ref: ResourceRef,
        revision: Revision,
    ) -> SelfHostedResourceOperation:
        receipt = SelfHostedResourceOperation(
            tenant_id=self._tenant(scope),
            account_id=self._account_key(scope),
            principal_id=scope.principal_id,
            kind=self.kind,
            key=key,
            digest=digest,
            operation_id=str(ULID()),
            action=action,
            result={
                "ref": ref.model_dump(mode="json"),
                "revision": revision.model_dump(mode="json"),
            },
        )
        session.add(receipt)
        await session.flush()
        return receipt

    def _account_key(self, scope: Scope) -> str:
        # UUID spellings share the same erasure lock and persisted receipt key.
        return "operator" if scope.account_id == "operator" else str(UUID(scope.account_id))

    async def _replay(
        self, session: AsyncSession, scope: Scope, receipt: SelfHostedResourceOperation
    ) -> Result:
        ref = ResourceRef.model_validate(receipt.result["ref"])
        row = await self._row(session, scope, ref)
        if Revision.model_validate(receipt.result["revision"]) != Revision(local=row.revision):
            raise ProviderError("conflict", retryable=False)
        return self._result(scope, row)

    async def create(self, scope: Scope, spec: Spec, *, key: str) -> Result:
        validate_resource_spec(spec)
        digest = request_digest(
            _JSON.validate_python([self.kind, "create", spec.model_dump(mode="json")])
        )
        async with self._write(scope) as (session, tenant):
            previous = await self._previous(session, scope, key, digest)
            if previous is not None:
                return await self._replay(session, scope, previous)
            exists = await session.scalar(
                select(self.table.id).where(
                    self.table.tenant_id == tenant,
                    self.table.name == spec.name,
                    self.table.archived_at.is_(None),
                )
            )
            if exists is not None:
                raise ProviderError("conflict", retryable=False)
            prefix = "sh_agent_" if self.kind == "agent" else "sh_env_"
            row = self.table(
                id=prefix + str(ULID()),
                tenant_id=tenant,
                name=spec.name,
                spec=spec.model_dump(mode="json"),
                revision=1,
            )
            if isinstance(row, SelfHostedAgentRecord) and isinstance(spec, AgentSpec):
                row.skill_attachments = await _skill_attachments(session, scope, spec, [], None)
            session.add(row)
            await session.flush()
            result = self._result(scope, row)
            await self._record(session, scope, key, digest, "create", result.ref, result.revision)
            return result

    async def retrieve(self, scope: Scope, ref: ResourceRef) -> Result:
        async with self.sessionmaker() as session:
            return self._result(scope, await self._row(session, scope, ref))

    async def resolve(self, scope: Scope, name_or_id: str) -> Result:
        """Resolve active resources only; reserved IDs never fall back to names."""
        async with self.sessionmaker() as session:
            field = self.table.id if name_or_id.startswith("sh_") else self.table.name
            row = await session.scalar(
                select(self.table).where(
                    self.table.tenant_id == self._tenant(scope),
                    field == name_or_id,
                    self.table.archived_at.is_(None),
                )
            )
            if row is None:
                raise ProviderError("not_found", retryable=False)
            return self._result(scope, row)

    async def _list(
        self, scope: Scope, *, filters: AgentFilter | EnvironmentFilter, page: PageRequest
    ) -> Page[Result]:
        query = select(self.table).where(self.table.tenant_id == self._tenant(scope))
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
            return Page(
                data=tuple(self._result(scope, row) for row in rows[:limit]),
                has_more=more,
                next_cursor=rows[limit - 1].id if more else None,
            )

    def _patched(self, row: Row, patch: Patch) -> Spec:
        changes = patch.model_dump(mode="json", exclude_unset=True)
        current = self.spec_type.model_validate(row.spec)
        data = dict(row.spec)
        if "metadata" in changes:
            changes.pop("metadata")
            if patch.metadata is None:
                data["metadata"] = None
            else:
                metadata = dict(current.metadata or {})
                for name, value in patch.metadata.items():
                    if value is None:
                        metadata.pop(name, None)
                    else:
                        metadata[name] = value
                data["metadata"] = metadata
        if "extensions" in changes:
            changes.pop("extensions")
            if isinstance(current, AgentSpec):
                if patch.extensions is None:
                    data["extensions"] = None
                else:
                    extensions = {item.namespace: item for item in current.extensions or ()}
                    extensions.update(patch.extensions)
                    data["extensions"] = [
                        item.model_dump(mode="json") for item in extensions.values()
                    ]
            elif patch.extensions:
                raise ProviderError("invalid_request", retryable=False)
        data.update(changes)
        try:
            spec = self.spec_type.model_validate(data)
        except ValidationError:
            raise ProviderError("invalid_request", retryable=False) from None
        validate_resource_spec(spec)
        return spec

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
        async with self._write(scope) as (session, tenant):
            previous = await self._previous(session, scope, key, digest)
            if previous is not None:
                return await self._replay(session, scope, previous)
            row = await self._row(session, scope, ref)
            if expected != Revision(local=row.revision) or row.archived_at is not None:
                raise ProviderError("conflict", retryable=False)
            spec = self._patched(row, patch)
            if isinstance(row, SelfHostedAgentRecord) and isinstance(spec, AgentSpec):
                row.skill_attachments = await _skill_attachments(
                    session, scope, spec, row.skill_attachments, AgentSpec.model_validate(row.spec)
                )
            if spec.name != row.name:
                exists = await session.scalar(
                    select(self.table.id).where(
                        self.table.tenant_id == tenant,
                        self.table.name == spec.name,
                        self.table.archived_at.is_(None),
                    )
                )
                if exists is not None:
                    raise ProviderError("conflict", retryable=False)
            row.spec, row.name = spec.model_dump(mode="json"), spec.name
            row.revision += 1
            row.updated_at = await self._now(session)
            result = self._result(scope, row)
            await self._record(session, scope, key, digest, "update", result.ref, result.revision)
            return result

    async def _now(self, session: AsyncSession) -> datetime:
        now = await session.scalar(select(func.now()))
        assert isinstance(now, datetime)
        return now

    def _archive_receipt(self, receipt: SelfHostedResourceOperation) -> Operation:
        return Operation(
            id=receipt.operation_id,
            key=receipt.key,
            request_digest=receipt.digest,
            status="processed",
            resource=ResourceRef.model_validate(receipt.result["ref"]),
            created_at=receipt.created_at,
            updated_at=receipt.created_at,
        )

    async def archive(self, scope: Scope, ref: ResourceRef, *, key: str) -> Operation:
        self._check(scope, ref)
        digest = request_digest(
            _JSON.validate_python([self.kind, "archive", ref.model_dump(mode="json")])
        )
        async with self._write(scope) as (session, _):
            previous = await self._previous(session, scope, key, digest)
            if previous is not None:
                return self._archive_receipt(previous)
            row = await self._row(session, scope, ref)
            if row.archived_at is None:
                row.archived_at = row.updated_at = await self._now(session)
                row.revision += 1
            receipt = await self._record(
                session, scope, key, digest, "archive", ref, Revision(local=row.revision)
            )
            return self._archive_receipt(receipt)

    async def delete(self, scope: Scope, ref: ResourceRef, *, key: str) -> DeletionReceipt:
        self._check(scope, ref)
        digest = request_digest(
            _JSON.validate_python([self.kind, "delete", ref.model_dump(mode="json")])
        )
        async with self._write(scope) as (session, _):
            previous = await self._previous(session, scope, key, digest)
            if previous is not None:
                return DeletionReceipt(operation_id=previous.operation_id, deleted=(ref,))
            row = await self._row(session, scope, ref)
            revision = Revision(local=row.revision)
            await session.delete(row)
            receipt = await self._record(session, scope, key, digest, "delete", ref, revision)
            return DeletionReceipt(operation_id=receipt.operation_id, deleted=(ref,))


class SelfHostedAgents(_Resources[AgentSpec, AgentPatch, Agent]):
    table = SelfHostedAgentRecord
    spec_type = AgentSpec
    result_type = Agent
    kind = "agent"

    async def find_by_derived_uuid(self, scope: Scope, agent_id: UUID) -> Agent | None:
        """Resolve a chat credential's execution identity among this tenant's live records."""
        async with self.sessionmaker() as session:
            rows = await session.scalars(
                select(SelfHostedAgentRecord).where(
                    SelfHostedAgentRecord.tenant_id == self._tenant(scope),
                    SelfHostedAgentRecord.archived_at.is_(None),
                )
            )
            for row in rows:
                if derive_agent_uuid(tenant_id=self._tenant(scope), ma_agent_id=row.id) == agent_id:
                    return self._result(scope, row)
        return None

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
