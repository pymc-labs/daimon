"""Authorized alternate-backend catalog, without pretending native mutation exists.

The host creates an immutable native agent before publishing its next catalog
revision. Old revisions retain their native refs; a stale CAS cannot change the
catalog. The caller owns cleanup of an uncommitted native replacement. Checked
skill bundles retain upload ownership before attachment.

No default Anthropic path calls this store. Policy checks and native operations
remain the host edge's responsibility; Scope must come from that authorization.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from daimon.core._models import (
    Account,
    NeutralAgentRevision,
    NeutralSkillVersion,
)
from daimon.core.skills.ingest import SkillBundle, SkillPreview, bundle_from_upload
from mux.contracts.ids import Scope
from mux.contracts.resources import Agent
from mux.errors import ScopeViolation
from sqlalchemy import ColumnElement, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class CatalogConflict(ValueError):
    """A stale agent revision or a conflicting immutable skill/ownership write."""


@dataclass(frozen=True)
class AgentRevision:
    catalog_id: str
    local_revision: int
    agent: Agent
    principal_id: str


@dataclass(frozen=True)
class SkillVersion:
    skill_id: str
    version: str
    agent_name: str
    principal_id: str
    display_title: str
    bundle: SkillBundle


type CatalogTable = type[NeutralAgentRevision] | type[NeutralSkillVersion]


class PostgresNeutralCatalog:
    """One provider workspace; tenant/account Scope is required on every method."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        provider: Literal["openai", "gemini"],
        account_scope_id: str,
    ) -> None:
        if provider not in ("openai", "gemini") or not account_scope_id.strip():
            raise ValueError("an alternate provider and a nonblank workspace are required")
        self._sessions = sessions
        self._provider = provider
        self._workspace = account_scope_id

    def _key(self, scope: Scope) -> dict[str, Any]:
        if (
            scope.is_platform
            or scope.is_legacy_host_authorized
            or not scope.authorization_id.strip()
            or not scope.principal_id.strip()
        ):
            raise ScopeViolation("catalog", "explicit tenant/account authorization required")
        try:
            tenant, account = uuid.UUID(scope.tenant_id), uuid.UUID(scope.account_id)
        except ValueError:
            raise ScopeViolation("catalog", "Daimon tenant/account UUIDs required") from None
        return {
            "tenant_id": tenant,
            "account_id": account,
            "provider": self._provider,
            "account_scope_id": self._workspace,
        }

    def _where(self, table: CatalogTable, scope: Scope) -> list[ColumnElement[bool]]:
        key = self._key(scope)
        return [
            table.tenant_id == key["tenant_id"],
            table.account_id == key["account_id"],
            table.provider == self._provider,
            table.account_scope_id == self._workspace,
        ]

    async def _authorize(self, session: AsyncSession, scope: Scope) -> None:
        key = self._key(scope)
        account = await session.scalar(
            select(Account.id).where(
                Account.id == key["account_id"], Account.tenant_id == key["tenant_id"]
            )
        )
        if account is None:
            raise ScopeViolation("catalog", "account is outside the authorized tenant")

    async def _lock(self, session: AsyncSession, scope: Scope, kind: str, id_: str) -> None:
        # Full routing tuple, stable across processes; collisions only serialize.
        body = json.dumps([str(v) for v in self._key(scope).values()] + [kind, id_])
        key = int.from_bytes(hashlib.sha256(body.encode()).digest()[:8], signed=True)
        await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})

    @staticmethod
    def _agent(row: NeutralAgentRevision) -> AgentRevision:
        return AgentRevision(
            row.catalog_id, row.local_revision, Agent.model_validate(row.agent), row.principal_id
        )

    @staticmethod
    def _skill(row: NeutralSkillVersion) -> SkillVersion:
        return SkillVersion(
            row.skill_id,
            row.version,
            row.agent_name,
            row.principal_id,
            row.display_title,
            SkillBundle(SkillPreview.model_validate(row.preview), row.zip_bytes),
        )

    async def get_agent(
        self, scope: Scope, catalog_id: str, *, local_revision: int | None = None
    ) -> AgentRevision | None:
        self._key(scope)
        query = select(NeutralAgentRevision).where(
            *self._where(NeutralAgentRevision, scope), NeutralAgentRevision.catalog_id == catalog_id
        )
        if local_revision is not None:
            query = query.where(NeutralAgentRevision.local_revision == local_revision)
        async with self._sessions() as session:
            await self._authorize(session, scope)
            row = await session.scalar(query.order_by(NeutralAgentRevision.local_revision.desc()))
            return None if row is None else self._agent(row)

    async def list_agents(self, scope: Scope) -> tuple[AgentRevision, ...]:
        self._key(scope)
        async with self._sessions() as session:
            await self._authorize(session, scope)
            rows = await session.scalars(
                select(NeutralAgentRevision)
                .where(*self._where(NeutralAgentRevision, scope))
                .distinct(NeutralAgentRevision.catalog_id)
                .order_by(
                    NeutralAgentRevision.catalog_id, NeutralAgentRevision.local_revision.desc()
                )
            )
            return tuple(self._agent(row) for row in rows)

    async def put_agent(
        self, scope: Scope, catalog_id: str, agent: Agent, *, expected_revision: int
    ) -> AgentRevision:
        key = self._key(scope)
        if not catalog_id.strip() or expected_revision < 0:
            raise ValueError("catalog id and nonnegative expected revision required")
        if (
            agent.ref.provider != self._provider
            or agent.spec.model.provider != self._provider
            or agent.ref.account_scope_id != self._workspace
            or agent.ref.tenant_id != scope.tenant_id
            or agent.ref.account_id != scope.account_id
            or agent.ref.kind != "agent"
        ):
            raise ScopeViolation(agent.ref.id, "agent is outside the authorized catalog scope")
        async with self._sessions() as session, session.begin():
            await self._authorize(session, scope)
            await self._lock(session, scope, "agent", catalog_id)
            latest = await session.scalar(
                select(NeutralAgentRevision.local_revision)
                .where(
                    *self._where(NeutralAgentRevision, scope),
                    NeutralAgentRevision.catalog_id == catalog_id,
                )
                .order_by(NeutralAgentRevision.local_revision.desc())
                .limit(1)
            )
            actual = latest or 0
            if expected_revision != actual:
                raise CatalogConflict(
                    f"expected agent revision {expected_revision}, found {actual}"
                )
            row = NeutralAgentRevision(
                **key,
                catalog_id=catalog_id,
                local_revision=actual + 1,
                principal_id=scope.principal_id,
                agent=agent.model_dump(mode="json"),
            )
            session.add(row)
            await session.flush()
            return self._agent(row)

    async def put_skill(
        self,
        scope: Scope,
        *,
        skill_id: str,
        version: str,
        agent_name: str,
        display_title: str,
        bundle: SkillBundle,
    ) -> SkillVersion:
        key = self._key(scope)
        if (
            any(not value.strip() for value in (skill_id, version, agent_name, display_title))
            or version == "latest"
        ):
            raise ValueError("owned skill and concrete version required")
        checked = bundle_from_upload(bundle.zip_bytes, filename="bundle.zip")
        if checked != bundle:
            raise ValueError("bundle bytes and preview must be the checked canonical bundle")
        async with self._sessions() as session, session.begin():
            await self._authorize(session, scope)
            await self._lock(session, scope, "skill", skill_id)
            versions = tuple(
                await session.scalars(
                    select(NeutralSkillVersion).where(
                        *self._where(NeutralSkillVersion, scope),
                        NeutralSkillVersion.skill_id == skill_id,
                    )
                )
            )
            for old in versions:
                if old.principal_id != scope.principal_id or old.agent_name != agent_name:
                    raise ScopeViolation(skill_id, "skill belongs to another principal or agent")
                if old.version == version:
                    if self._skill(old) != SkillVersion(
                        skill_id, version, agent_name, scope.principal_id, display_title, bundle
                    ):
                        raise CatalogConflict("immutable skill version differs")
                    return self._skill(old)
            row = NeutralSkillVersion(
                **key,
                skill_id=skill_id,
                version=version,
                agent_name=agent_name,
                principal_id=scope.principal_id,
                display_title=display_title,
                preview=bundle.preview.model_dump(mode="json"),
                zip_bytes=bundle.zip_bytes,
            )
            session.add(row)
            await session.flush()
            return self._skill(row)

    async def get_skill(self, scope: Scope, skill_id: str, version: str) -> SkillVersion | None:
        self._key(scope)
        async with self._sessions() as session:
            await self._authorize(session, scope)
            row = await session.scalar(
                select(NeutralSkillVersion).where(
                    *self._where(NeutralSkillVersion, scope),
                    NeutralSkillVersion.skill_id == skill_id,
                    NeutralSkillVersion.version == version,
                )
            )
            return None if row is None else self._skill(row)

    async def list_skills(self, scope: Scope) -> tuple[SkillVersion, ...]:
        self._key(scope)
        async with self._sessions() as session:
            await self._authorize(session, scope)
            rows = await session.scalars(
                select(NeutralSkillVersion)
                .where(*self._where(NeutralSkillVersion, scope))
                .order_by(NeutralSkillVersion.skill_id, NeutralSkillVersion.version)
            )
            return tuple(self._skill(row) for row in rows)
