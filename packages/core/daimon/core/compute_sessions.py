"""Leased compute lifecycle.

Only the authenticated session owner, through an authorized attended conversation,
may launch, execute in, write to or extend their registered session within the
configured caps; the owner, a tenant admin or the fenced reconciler may stop it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from daimon.core.compute_models import ComputeResource, ComputeSession, ComputeStatus
from daimon.core.compute_provider import (
    ComputeProvider,
    ComputeProviderError,
    app_name,
    session_token,
)
from daimon.core.config import ComputeSettings
from daimon.core.errors import StoreError
from daimon.core.stores import compute_sessions as store
from daimon.core.stores.domain import TurnOriginRow
from daimon.core.stores.tenants import list_tenants_by_platform
from daimon.core.stores.turn_origins import get_active_origin
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ComputeCaller:
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    origin: TurnOriginRow
    chat_session_id: str
    external_user_id: str
    admin: bool = False


class ComputeSessions:
    def __init__(
        self,
        sm: async_sessionmaker[AsyncSession],
        settings: ComputeSettings,
        provider: ComputeProvider,
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self.sm, self.settings, self.provider = sm, settings, provider
        self.clock = clock or (lambda: datetime.now(UTC))

    def ttl(self, hours: int | None) -> int:
        value = (
            hours
            if hours is not None
            else min(self.settings.default_idle_ttl_hours, self.settings.max_ttl_hours)
        )
        if not 1 <= value <= self.settings.max_ttl_hours:
            raise StoreError(f"Lifetime must be between 1 and {self.settings.max_ttl_hours} hours")
        return value

    async def _origin(self, db: AsyncSession, caller: ComputeCaller) -> None:
        origin = await get_active_origin(
            db,
            tenant_id=caller.tenant_id,
            account_id=caller.account_id,
            origin_id=caller.origin.id,
            platform=caller.origin.platform,
            now=self.clock(),
        )
        if (
            origin is None
            or not origin.attended
            or origin.human_invocation is not True
            or not caller.chat_session_id
            or origin.chat_session_id != caller.chat_session_id
            or origin != caller.origin
        ):
            raise StoreError("Use an active attended conversation with the session owner.")

    async def _owned(
        self, db: AsyncSession, caller: ComputeCaller, session_id: uuid.UUID, *, stop: bool = False
    ) -> ComputeSession:
        await self._origin(db, caller)
        session = await store.get(db, caller.tenant_id, session_id, lock=True)
        if session is None or session.deployment_id != self.settings.deployment_id:
            raise StoreError("Session not found")
        if stop and caller.admin:
            return session
        if session.owner_account_id != caller.account_id or (
            session.platform,
            session.thread_id,
            session.parent_channel_id,
        ) != (caller.origin.platform, caller.origin.thread_id, caller.origin.parent_channel_id):
            raise StoreError("Session not found in this owner's conversation")
        return session

    async def launch(
        self,
        caller: ComputeCaller,
        *,
        profile: str,
        region: str,
        ttl_hours: int | None,
        approval_key: str,
        source_metadata: dict[str, str],
    ) -> ComputeSession:
        if not self.settings.enabled:
            raise StoreError("Fly session launches are disabled. Ask an admin.")
        image = self.settings.profiles.get(profile)
        if image is None or region not in image.regions:
            raise StoreError("Choose an available image profile and region.")
        ttl = self.ttl(ttl_hours)
        session_id = uuid.uuid4()
        # Refuse missing authentication before registering.
        session_token(self.settings, session_id)
        if not approval_key:
            raise StoreError("Owner approval is required")
        now = self.clock()
        session = ComputeSession(
            id=session_id,
            tenant_id=caller.tenant_id,
            owner_account_id=caller.account_id,
            owner_external_user_id=caller.external_user_id,
            deployment_id=self.settings.deployment_id,
            platform=caller.origin.platform,
            parent_channel_id=caller.origin.parent_channel_id,
            thread_id=caller.origin.thread_id,
            agent_id=caller.origin.responder_ma_agent_id,
            agent_name=caller.origin.responder_name,
            source_metadata=source_metadata,
            request_key=approval_key,
            app_name=app_name(self.settings.deployment_id, session_id),
            profile=profile,
            image=image.image,
            region=region,
            cpu_kind=image.cpu_kind,
            cpus=image.cpus,
            memory_mb=image.memory_mb,
            ttl_hours=ttl,
            expires_at=now + timedelta(hours=ttl),
            created_at=now,
        )
        async with self.sm.begin() as db:
            await self._origin(db, caller)
            session = await store.register(db, session, cap=self.settings.per_person_sessions)
        # Registration commits before any external mutation. A lost response is
        # recovered by the deterministic app name, not by launching a second app.
        async with self.sm.begin() as db:
            current = await self._owned(db, caller, session.id)
            if current.state == "launching" and current.expires_at <= self.clock():
                current = current.model_copy(update={"state": "stopping"})
                await store.save(db, current)
            elif current.state == "launching":
                current = await self._provision(current)
                await store.save(db, current)
            return current

    async def _provision(self, session: ComputeSession) -> ComputeSession:
        resource = await self.provider.ensure(session, session_token(self.settings, session.id))
        return session.model_copy(update={"state": "running", "machine_id": resource.machine_id})

    async def extend(
        self, caller: ComputeCaller, session_id: uuid.UUID, *, hours: int, approval_key: str
    ) -> ComputeSession:
        ttl = self.ttl(hours)
        if not approval_key:
            raise StoreError("Owner approval is required")
        async with self.sm.begin() as db:
            session = await self._owned(db, caller, session_id)
            if session.state != "running" or session.expires_at <= self.clock():
                raise StoreError("The session is stopping or expired. Launch a new session.")
            if approval_key not in session.extension_keys:
                session = session.model_copy(
                    update={
                        "expires_at": self.clock() + timedelta(hours=ttl),
                        "warned_for": None,
                        "extension_keys": [*session.extension_keys, approval_key],
                    }
                )
                await store.save(db, session)
            return session

    async def stop(self, caller: ComputeCaller, session_id: uuid.UUID) -> ComputeSession:
        async with self.sm.begin() as db:
            session = await self._owned(db, caller, session_id, stop=True)
            if session.state != "deleted":
                session = session.model_copy(update={"state": "stopping"})
                await store.save(db, session)
        # Stopping intent survives failed provider calls and process restarts.
        await self._delete(session)
        async with self.sm.begin() as db:
            result = await store.get(db, caller.tenant_id, session_id)
            assert result is not None
            return result

    async def _delete(self, session: ComputeSession) -> None:
        async with self.sm.begin() as db:
            current = await store.get(db, session.tenant_id, session.id, lock=True)
            if current is None or current.state == "deleted":
                return
            if current.state != "stopping":
                raise StoreError("Session is not marked for deletion")
            await self.provider.delete(
                ComputeResource(
                    app_name=current.app_name,
                    deployment_id=current.deployment_id,
                    session_id=current.id,
                    machine_id=current.machine_id,
                )
            )
            await store.save(db, current.model_copy(update={"state": "deleted"}))

    async def get(self, caller: ComputeCaller, session_id: uuid.UUID) -> ComputeSession:
        async with self.sm.begin() as db:
            return await self._owned(db, caller, session_id)

    async def status(self, caller: ComputeCaller, session_id: uuid.UUID) -> ComputeStatus:
        async with self.sm.begin() as db:
            session = await self._owned(db, caller, session_id)
            resource = await self.provider.inspect(session)
            return ComputeStatus(**session.model_dump(), machine_state=resource.machine_state)

    async def list(self, caller: ComputeCaller) -> list[ComputeSession]:
        async with self.sm.begin() as db:
            await self._origin(db, caller)
            sessions = await store.list_sessions(db, caller.tenant_id, owner=caller.account_id)
            return [
                s
                for s in sessions
                if s.deployment_id == self.settings.deployment_id
                and s.platform == caller.origin.platform
                and s.thread_id == caller.origin.thread_id
                and s.parent_channel_id == caller.origin.parent_channel_id
                and s.state != "deleted"
            ]

    async def call(
        self,
        caller: ComputeCaller,
        session_id: uuid.UUID,
        *,
        tool: str,
        arguments: dict[str, JsonValue],
    ) -> JsonValue:
        if tool not in {"run_bash", "write_file", "read_file", "list_files"}:
            raise StoreError("Unsupported session operation")
        async with self.sm.begin() as db:
            session = await self._owned(db, caller, session_id)
            if session.state != "running" or session.expires_at <= self.clock():
                raise StoreError("The session is expired or stopping. Launch a new session.")
            result = await self.provider.call(
                session, session_token(self.settings, session.id), tool, arguments
            )
            if tool in {"run_bash", "write_file"}:
                await store.save(
                    db,
                    session.model_copy(
                        update={
                            "expires_at": self.clock()
                            + timedelta(hours=min(session.ttl_hours, self.settings.max_ttl_hours)),
                            "warned_for": None,
                        }
                    ),
                )
            return result

    async def reconcile(self) -> None:
        if not self.settings.deployment_id:
            return
        # Inventory precedes the registration snapshot: every app's intent must
        # have committed before its creation, including concurrent launches.
        try:
            resources = await self.provider.inventory()
        except ComputeProviderError:
            # A listing outage must not prevent expiry/deletion of known rows.
            resources = []
            log.warning("compute.inventory_retry")
        known: set[uuid.UUID] = set()
        for tenant in await list_tenants_by_platform(self.sm):
            async with self.sm.begin() as db:
                sessions = await store.list_sessions(db, tenant.id)
            for session in sessions:
                if session.deployment_id != self.settings.deployment_id:
                    continue
                if session.state != "deleted":
                    known.add(session.id)
                try:
                    await self._reconcile_one(session, archived=tenant.archived_at is not None)
                except (ComputeProviderError, StoreError):
                    # Never log remote exceptions: requests may include credentials.
                    log.warning("compute.reconcile_retry", session_id=str(session.id))
        for resource in resources:
            if (
                resource.deployment_id == self.settings.deployment_id
                and resource.session_id not in known
            ):
                try:
                    await self.provider.delete(resource)
                except ComputeProviderError:
                    log.warning("compute.orphan_delete_retry", session_id=str(resource.session_id))

    async def _reconcile_one(self, session: ComputeSession, *, archived: bool) -> None:
        async with self.sm.begin() as db:
            current = await store.get(
                db, session.tenant_id, session.id, lock=True, skip_locked=True
            )
            if current is None or current.state == "deleted":
                return
            now = self.clock()
            if (
                archived
                or current.expires_at <= now
                or current.state == "stopping"
                or (current.state == "launching" and not self.settings.enabled)
            ):
                current = current.model_copy(update={"state": "stopping"})
                await store.save(db, current)
            elif current.state == "launching":
                # Unknown create outcomes are never blindly re-POSTed. ensure()
                # recovers an existing machine by its deterministic app identity.
                current = await self._provision(current)
                await store.save(db, current)
            elif (
                current.expires_at - now <= timedelta(hours=min(1, current.ttl_hours / 4))
                and current.warned_for != current.expires_at
            ):
                await store.warn(db, current, now)
        if current.state == "stopping":
            await self._delete(current)
