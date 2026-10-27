"""Routines store — CRUD plus concurrency-safe `claim_due_fireable` and
`advance_stale` helpers.

`claim_due_fireable` is a 2-step null-out: step 1 atomically claims a
batch of due rows by `UPDATE ... WHERE id IN (SELECT id ... FOR UPDATE
SKIP LOCKED LIMIT N)` and zeroes their `next_fire_at` (Postgres has no
direct `UPDATE ... LIMIT`, hence the subquery). Step 2 walks the
returned rows and recomputes `next_fire_at` per row from cron. A crash
between the two phases leaves an orphan (`next_fire_at IS NULL`); the
companion `advance_stale` call recovers those plus any rows whose
`next_fire_at` slipped past the freshness cutoff.
"""

from __future__ import annotations

import uuid as _uuid
from collections.abc import Collection, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal, cast

import structlog
from daimon.core._models import (
    Account,
    PlatformPrincipal,
    Routine,
    RoutineSandboxHistoryRecord,
    Tenant,
)
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, Subject, authorize
from daimon.core.cron import next_slot_at_or_after
from daimon.core.errors import RoutineAlreadyRunning, StoreError
from daimon.core.permissions import limiting_ids_at
from daimon.core.routine_files import OUTBOX_FILES, PREFIX, plain_payload
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.domain import (
    CatchUpPolicy,
    Role,
    RoutineDestinationKind,
    RoutineRow,
    SandboxPin,
    UnattendedRequester,
)
from daimon.core.stores.security_audit import append_event
from sqlalchemy import and_, delete, false, func, or_, select, true, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

log = structlog.get_logger(__name__)

LAST_RESULT_TAIL_MAX = 1000
"""Maximum length of the stored preview; delivery keeps the complete reply."""

FAILURE_NOTICE = "failure_notice"
"""`delivery_note` of a pending post that tells the destination a run failed.

The poster renders such a payload as a failure notice instead of a result.
Settling replaces the note as for any post."""


def _captured_routine(row: RoutineRow) -> ColumnElement[bool]:
    """The same owner, destination and configuration revision as the fire.

    Resolver healing changes only agent_id, without a configuration revision.
    Keep matching that fire's outcome even if its captured ID is now stale.
    """
    return and_(
        Routine.tenant_id == row.tenant_id,
        Routine.created_by_user_id.is_not_distinct_from(row.created_by_user_id),
        Routine.agent_name.is_not_distinct_from(row.agent_name),
        Routine.updated_at == row.updated_at,
        Routine.destination_kind.is_not_distinct_from(row.destination_kind),
        Routine.destination_id.is_not_distinct_from(row.destination_id),
    )


async def create_routine(
    session: AsyncSession,
    *,
    tenant_id: _uuid.UUID,
    created_by_user_id: str | None,
    agent_id: str,
    agent_name: str,
    cron_expr: str,
    timezone_: str,
    trigger_message: str,
    internal_routine_id: _uuid.UUID | None = None,
    enabled: bool = True,
    catch_up_policy: CatchUpPolicy = "skip",
    next_fire_at: datetime | None = None,
    destination_kind: RoutineDestinationKind | None = None,
    destination_id: str | None = None,
    channel_id: str | None = None,
    source_thread_id: str | None = None,
    source_channel_id: str | None = None,
    source_provenance_known: bool | None = None,
) -> RoutineRow:
    """`channel_id` is the channel the routine's spend counts against (see `Routine`)."""
    if catch_up_policy not in ("skip", "run-once"):
        raise StoreError("catch_up_policy must be skip or run-once")
    destination_id = await _resolve_destination(
        session, tenant_id, destination_kind, destination_id, created_by_user_id
    )
    # Adapter-supplied provenance is an explicit creation snapshot, including
    # a verified source-free origin. Older host callers use their creation place.
    if source_provenance_known is None:
        source_channel_id = source_channel_id or channel_id
        if destination_kind == "thread":
            source_thread_id = source_thread_id or destination_id
        elif destination_kind == "channel":
            source_channel_id = source_channel_id or destination_id
        source_provenance_known = destination_kind != "thread" or source_channel_id is not None
    if destination_kind == "dm":
        if not source_provenance_known:
            raise StoreError("Recreate the routine from the channel or thread to get DM delivery.")
        await validate_dm_source(session, tenant_id, source_channel_id, source_thread_id)
    orm = Routine(
        tenant_id=tenant_id,
        internal_routine_id=internal_routine_id,
        created_by_user_id=created_by_user_id,
        agent_id=agent_id,
        agent_name=agent_name,
        cron_expr=cron_expr,
        timezone=timezone_,
        trigger_message=trigger_message,
        enabled=enabled,
        catch_up_policy=catch_up_policy,
        next_fire_at=next_fire_at,
        destination_kind=destination_kind,
        destination_id=destination_id,
        channel_id=channel_id,
        source_thread_id=source_thread_id,
        source_channel_id=source_channel_id,
        source_provenance_known=source_provenance_known,
    )
    session.add(orm)
    await session.flush()
    await session.refresh(orm)
    return RoutineRow.model_validate(orm)


async def get_routine(
    session: AsyncSession, routine_id: _uuid.UUID, *, tenant_id: _uuid.UUID
) -> RoutineRow | None:
    orm = (
        await session.execute(
            select(Routine).where(Routine.id == routine_id, Routine.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if orm is None:
        return None
    return RoutineRow.model_validate(orm)


async def list_routines_missing_agent_name(
    session: AsyncSession,
) -> list[RoutineRow]:
    """Return all routines where `agent_name IS NULL`.

    Used by the one-shot `daimon routines backfill-agent-names` CLI command
    Inherently idempotent: re-running after backfill returns
    an empty list. No pagination — the row count is bounded by tenant
    routine cardinality, which is small.
    """
    rows = (
        (await session.execute(select(Routine).where(Routine.agent_name.is_(None)))).scalars().all()
    )
    return [RoutineRow.model_validate(r) for r in rows]


async def list_routines_for_tenant(
    session: AsyncSession, *, tenant_id: _uuid.UUID
) -> list[RoutineRow]:
    rows = (
        (await session.execute(select(Routine).where(Routine.tenant_id == tenant_id)))
        .scalars()
        .all()
    )
    return [RoutineRow.model_validate(r) for r in rows]


async def list_routine_creators(
    session: AsyncSession,
    *,
    tenant_id: _uuid.UUID,
    platform: str,
    agent_names: Collection[str],
    agent_id: str | None = None,
) -> list[UnattendedRequester]:
    """Who made the routines that run the agent, paused ones included.

    A routine counts when it names any of `agent_names` or runs `agent_id`.

    A routine with no recorded creator never fires and is left out; a creator
    with no account yet fires as a plain member.
    """
    rows = await session.execute(
        select(Routine.created_by_user_id, Account.role, Account.platform_role_ids)
        .outerjoin(
            PlatformPrincipal,
            and_(
                PlatformPrincipal.tenant_id == Routine.tenant_id,
                PlatformPrincipal.platform == platform,
                PlatformPrincipal.external_id == Routine.created_by_user_id,
            ),
        )
        .outerjoin(Account, Account.id == PlatformPrincipal.account_id)
        .where(
            Routine.tenant_id == tenant_id,
            or_(Routine.agent_name.in_(agent_names), Routine.agent_id == agent_id),
            Routine.created_by_user_id.is_not(None),
        )
        .distinct()
    )
    return [
        UnattendedRequester(
            platform_user_id=user_id,
            is_admin=role == Role.ADMIN,
            role_ids=tuple(role_ids or ()),
        )
        for user_id, role, role_ids in rows.tuples()
        if user_id is not None
    ]


async def list_routine_channel_ids(
    session: AsyncSession,
    *,
    tenant_id: _uuid.UUID,
    agent_names: Collection[str],
    agent_id: str | None = None,
    caller_platform_user_id: str | None = None,
) -> list[str | None]:
    """The channels of the routines that run the agent, paused ones included.

    None stands for a routine with no channel. The caller's own routines are
    left out; None for `caller_platform_user_id` counts them all.
    """
    statement = select(Routine.channel_id).where(
        Routine.tenant_id == tenant_id,
        or_(Routine.agent_name.in_(agent_names), Routine.agent_id == agent_id),
    )
    if caller_platform_user_id is not None:
        statement = statement.where(
            Routine.created_by_user_id.is_distinct_from(caller_platform_user_id)
        )
    return list((await session.execute(statement.distinct())).scalars())


async def update_routine(
    session: AsyncSession,
    routine_id: _uuid.UUID,
    *,
    tenant_id: _uuid.UUID,
    cron_expr: str | None = None,
    timezone_: str | None = None,
    trigger_message: str | None = None,
    enabled: bool | None = None,
    catch_up_policy: CatchUpPolicy | None = None,
    agent_id: str | None = None,
    agent_name: str | None = None,
    next_fire_at: datetime | None = None,
    destination_kind: RoutineDestinationKind | None = None,
    destination_id: str | None = None,
    clear_destination: bool = False,
    channel_id: str | None = None,
    expected_updated_at: datetime | None = None,
    edited_by: str = "unknown",
) -> RoutineRow | None:
    """PATCH: `None` leaves a field alone. A destination is set as a pair, with
    the `channel_id` it resolves to; `clear_destination=True` removes the pair
    (and any pending delivery) but keeps the source channel/thread provenance
    and `channel_id`, so the routine's spend still counts toward the same budget.
    A concurrent edit raises StoreError; callers can also supply the revision
    used for authorization so a stale authorized view cannot be applied.
    `edited_by` must be the authenticated platform user id for behavior edits.
    Changing the message, agent or target transfers execution authority to
    that editor and audits the transfer atomically. Pause/resume retain it.
    """
    # Read exactly once: validation, erasure and the write share this revision.
    # A concurrent edit must fail the CAS rather than reuse stale classification.
    current = await get_routine(session, routine_id, tenant_id=tenant_id)
    if current is None:
        return None
    if expected_updated_at is not None and current.updated_at != expected_updated_at:
        raise StoreError("Routine changed while being edited; reload it and try again.")
    values: dict[str, str | bool | datetime | None] = {}
    if clear_destination:
        if destination_kind is not None or destination_id is not None:
            raise StoreError("clear_destination cannot be combined with a new destination")
        values.update(destination_kind=None, destination_id=None)
    elif destination_kind is not None or destination_id is not None:
        destination_id = await _resolve_destination(
            session, tenant_id, destination_kind, destination_id, current.created_by_user_id
        )
        if destination_kind == "dm":
            if not current.source_provenance_known:
                raise StoreError(
                    "Recreate the routine from the channel or thread to get DM delivery."
                )
            await validate_dm_source(
                session, tenant_id, current.source_channel_id, current.source_thread_id
            )
        values.update(
            destination_kind=destination_kind,
            destination_id=destination_id,
            channel_id=current.channel_id if destination_kind == "dm" else channel_id,
        )
    # Metadata-only edits must also respect a seal added after conversion.
    if current.destination_kind == "dm" and not clear_destination and destination_kind is None:
        await validate_dm_source(
            session,
            tenant_id,
            current.source_channel_id,
            current.source_thread_id,
        )
    if catch_up_policy is not None:
        if catch_up_policy not in ("skip", "run-once"):
            raise StoreError("catch_up_policy must be skip or run-once")
        values["catch_up_policy"] = catch_up_policy
    if cron_expr is not None:
        values["cron_expr"] = cron_expr
    if timezone_ is not None:
        values["timezone"] = timezone_
    if trigger_message is not None:
        values["trigger_message"] = trigger_message
    if enabled is not None:
        values["enabled"] = enabled
    if agent_id is not None:
        values["agent_id"] = agent_id
    if agent_name is not None:
        values["agent_name"] = agent_name
    if next_fire_at is not None:
        values["next_fire_at"] = next_fire_at
    if not values:
        return current

    return await _save_routine_edit(session, current, values, edited_by=edited_by)


#: What an admin approved when opting a routine in to sandbox tools: who runs
#: it, what it is told, and where it posts. Changing any of them clears the opt-in.
_SANDBOX_MATERIAL: Final = (
    "agent_id",
    "agent_name",
    "trigger_message",
    "destination_kind",
    "destination_id",
    "channel_id",
)


async def _save_routine_edit(
    session: AsyncSession,
    current: RoutineRow,
    values: dict[str, str | bool | datetime | None],
    *,
    expected_updated_at: datetime | None = None,
    edited_by: str = "unknown",
) -> RoutineRow:
    """All configuration writers share CAS, monotone revisions and erasure.

    An edit that changes what an admin approved (`_SANDBOX_MATERIAL`) turns
    the routine's sandbox opt-in off and logs it (`via="edit"`) in the same
    transaction, so the owner cannot carry an opt-in over to a new agent,
    prompt or destination.
    """
    if "enabled" in values:
        values.update(
            handback_paused_at=None,
            handback_enabled=None,
            handback_next_fire_at=None,
        )
    material_changed = any(
        key in values and values[key] != getattr(current, key) for key in _SANDBOX_MATERIAL
    )
    if material_changed and (not edited_by.strip() or edited_by == "unknown"):
        raise StoreError("Changing a routine's behavior requires its editor's platform user id.")
    authority_transferred = material_changed and edited_by != current.created_by_user_id
    if authority_transferred:
        values["created_by_user_id"] = edited_by
        if values.get("destination_kind", current.destination_kind) == "dm":
            # Private delivery follows the new author, never the prior author's
            # credential, inbox or personal memory.
            values["destination_id"] = await _resolve_destination(
                session, current.tenant_id, "dm", None, edited_by
            )
    sandbox_cleared = current.sandbox_tools_enabled and material_changed
    cleared_at = datetime.now(UTC)
    if sandbox_cleared:
        values.update(
            sandbox_tools_enabled=False,
            sandbox_tools_set_by=edited_by,
            sandbox_tools_set_at=cleared_at,
            **_PIN_CLEARED,
        )
    if expected_updated_at is not None and current.updated_at != expected_updated_at:
        raise StoreError("Routine changed while being edited; reload it and try again.")
    if (
        authority_transferred
        or current.destination_kind == "dm"
        or (
            "agent_id" in values
            or "agent_name" in values
            or "destination_kind" in values
            or "destination_id" in values
        )
    ):
        # Any private edit or producer/destination change invalidates content.
        # This also erases retained previews from legacy owner-only routines.
        # Agent reassignment and destination ABA cannot reclassify old content.
        cleared_public = (
            current.destination_kind != "dm"
            and "destination_kind" in values
            and values["destination_kind"] is None
        )
        values.update(
            last_result_tail=None,
            last_error=None,
            delivery_payload=None,
            delivery_status=None if cleared_public else "skipped",
            delivery_note=(
                None
                if cleared_public
                else "private_revision_changed"
                if current.destination_kind == "dm"
                else "destination_changed"
                if "destination_kind" in values
                else "authority_changed"
                if authority_transferred
                else "agent_changed"
            ),
            delivery_lease_owner=None,
            delivery_lease_expires_at=None,
        )

    stmt = (
        update(Routine)
        .where(
            Routine.id == current.id,
            Routine.tenant_id == current.tenant_id,
            Routine.updated_at == current.updated_at,
        )
        .values(
            **values,
            updated_at=func.greatest(
                func.clock_timestamp(), Routine.updated_at + timedelta(microseconds=1)
            ),
        )
        .returning(Routine)
        .execution_options(synchronize_session=False)
    )
    result = await session.execute(stmt)
    orm = result.scalar_one_or_none()
    if orm is None:
        raise StoreError("Routine changed while being edited; reload it and try again.")
    if authority_transferred:
        tenant = await session.get(Tenant, current.tenant_id)
        if tenant is None:
            raise StoreError("Routine tenant no longer exists.")
        await append_event(
            session,
            tenant_id=current.tenant_id,
            account_id=None,
            agent_id=None,
            platform=tenant.platform,
            platform_user_id=edited_by,
            admin_subject_kind="user" if current.created_by_user_id is not None else None,
            admin_subject_id=current.created_by_user_id,
            tool_name="routines",
            operation="transfer_authority",
            outcome="allowed",
            reason="material_edit",
            turn_ref=f"routine:{current.id}",
        )
    if sandbox_cleared:
        session.add(
            RoutineSandboxHistoryRecord(
                tenant_id=current.tenant_id,
                routine_id=current.id,
                before_enabled=True,
                after_enabled=False,
                set_by=edited_by,
                set_at=cleared_at,
                via="edit",
            )
        )
        log.info(
            "routine.sandbox_tools_cleared_by_edit",
            tenant_id=str(current.tenant_id),
            routine_id=str(current.id),
            edited_by=edited_by,
        )
    await session.flush()
    return RoutineRow.model_validate(orm)


async def update_routine_agent_id(
    session: AsyncSession,
    routine_id: _uuid.UUID,
    new_agent_id: str,
    *,
    expected_updated_at: datetime | None = None,
) -> bool:
    """Update only ``routines.agent_id``. Used by the scheduler resolver path
    when the resolver heals a stale id. Does NOT touch
    ``next_fire_at``, ``cron_expr``, ``agent_name``, etc.

    Returns True if a row was updated, False otherwise.
    """
    statement = update(Routine).where(Routine.id == routine_id)
    if expected_updated_at is not None:
        statement = statement.where(Routine.updated_at == expected_updated_at)
    result = await session.execute(statement.values(agent_id=new_agent_id))
    return cast(CursorResult[Any], result).rowcount > 0


async def delete_routine(
    session: AsyncSession, routine_id: _uuid.UUID, *, tenant_id: _uuid.UUID
) -> bool:
    result = await session.execute(
        delete(Routine).where(Routine.id == routine_id, Routine.tenant_id == tenant_id)
    )
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount > 0


async def delete_for_principal(
    session: AsyncSession,
    *,
    tenant_id: _uuid.UUID,
    external_id: str,
) -> int:
    """Delete all routines created by `(tenant_id, external_id)`. Idempotent.

    Returns rowcount; never raises on 0. Used by the GDPR purge orchestrator.
    `created_by_user_id` is a Text column holding the platform `external_id`.
    """
    result = await session.execute(
        delete(Routine).where(
            Routine.tenant_id == tenant_id,
            Routine.created_by_user_id == external_id,
        )
    )
    rowcount = cast(CursorResult[Any], result).rowcount
    await session.flush()
    return rowcount


async def count_routines_for_principal(
    session: AsyncSession,
    *,
    tenant_id: _uuid.UUID,
    external_id: str,
) -> int:
    """Count routines that `delete_for_principal` would delete. Read-only."""
    stmt = (
        select(func.count())
        .select_from(Routine)
        .where(
            Routine.tenant_id == tenant_id,
            Routine.created_by_user_id == external_id,
        )
    )
    return int((await session.execute(stmt)).scalar_one())


async def get_first_routine_for_principal(
    session: AsyncSession,
    *,
    tenant_id: _uuid.UUID,
    external_id: str,
) -> RoutineRow | None:
    """Return the first routine row for `(tenant_id, external_id)`, or None.

    Used for human-display "example" labels in /privacy cascade previews.
    Ordered by `created_at` so the example is stable.
    """
    stmt = (
        select(Routine)
        .where(
            Routine.tenant_id == tenant_id,
            Routine.created_by_user_id == external_id,
        )
        .order_by(Routine.created_at)
        .limit(1)
    )
    orm = (await session.execute(stmt)).scalar_one_or_none()
    return None if orm is None else RoutineRow.model_validate(orm)


type SandboxVia = Literal["mcp", "cli"]

PIN_UNAVAILABLE_REFUSAL = (
    "Can't read the routine's agent version or environment, so sandbox tools can't be "
    "turned on for it. Nothing was changed."
)
#: The setter a fire records when it finds the agent or environment changed.
SANDBOX_DRIFT_SETTER: Final = "agent_changed"

DM_SANDBOX_REFUSAL = (
    "DM routines can't use sandbox tools: they run as the owner's private conversation. "
    "Nothing was changed."
)


async def set_sandbox_tools(
    session: AsyncSession,
    routine_id: _uuid.UUID,
    *,
    tenant_id: _uuid.UUID,
    enabled: bool,
    set_by: str,
    actor: Subject,
    via: SandboxVia,
    pin: SandboxPin | None = None,
) -> tuple[RoutineRow, bool] | None:
    """Opt one routine's own runs in to (or out of) built-in sandbox tools.

    An opt-in covers exactly `pin`, the agent version and environment
    revision read just before (`routine_sandbox.current_pin`); without one it
    is refused (StoreError). Turning tools off forgets the pin. Opting in
    again with a different pin records the new one.

    Only a current human admin may (`Action.SET_ROUTINE_SANDBOX_TOOLS`); owning
    the routine is not enough. A DM routine cannot be opted in (StoreError):
    it runs as the owner's private DM turn, and converting a routine to DM
    clears an opt-in (`_save_routine_edit`). Returns the row and whether it changed, or None
    for an unknown id. Setting the value it already has writes nothing, so a
    repeated opt-in is a no-op; a change records `set_by`/`set_at` on the row
    and appends a `routine_sandbox_history` row.
    """
    if not authorize(TenantAccessPolicy(), subject=actor, action=Action.SET_ROUTINE_SANDBOX_TOOLS):
        raise PermissionError("only a workspace admin may change a routine's sandbox tools")
    orm = await session.scalar(
        select(Routine)
        .where(Routine.id == routine_id, Routine.tenant_id == tenant_id)
        .with_for_update()
    )
    if orm is None:
        return None
    if enabled and orm.destination_kind == "dm":
        raise StoreError(DM_SANDBOX_REFUSAL)
    if enabled and pin is None:
        raise StoreError(PIN_UNAVAILABLE_REFUSAL)
    before = orm.sandbox_tools_enabled
    recorded = pin_of_orm(orm)
    if before == enabled and (not enabled or recorded == pin):
        return RoutineRow.model_validate(orm), False
    now = datetime.now(UTC)
    kept = pin if enabled else None
    orm.sandbox_tools_enabled = enabled
    orm.sandbox_tools_set_by = set_by
    orm.sandbox_tools_set_at = now
    orm.sandbox_agent_id = kept.agent_id if kept else None
    orm.sandbox_agent_version = kept.agent_version if kept else None
    orm.sandbox_environment_id = kept.environment_id if kept else None
    orm.sandbox_environment_updated_at = kept.environment_updated_at if kept else None
    orm.updated_at = now
    session.add(
        RoutineSandboxHistoryRecord(
            tenant_id=tenant_id,
            routine_id=routine_id,
            before_enabled=before,
            after_enabled=enabled,
            set_by=set_by,
            set_at=now,
            via=via,
            agent_id=kept.agent_id if kept else None,
            agent_version=kept.agent_version if kept else None,
            environment_id=kept.environment_id if kept else None,
            environment_updated_at=kept.environment_updated_at if kept else None,
        )
    )
    await session.flush()
    log.info(
        "routine.sandbox_tools_set",
        tenant_id=str(tenant_id),
        routine_id=str(routine_id),
        enabled=enabled,
        set_by=set_by,
        via=via,
    )
    return RoutineRow.model_validate(orm), True


def pin_of_orm(orm: Routine) -> SandboxPin | None:
    if (
        orm.sandbox_agent_id is None
        or orm.sandbox_agent_version is None
        or orm.sandbox_environment_id is None
        or orm.sandbox_environment_updated_at is None
    ):
        return None
    return SandboxPin(
        agent_id=orm.sandbox_agent_id,
        agent_version=orm.sandbox_agent_version,
        environment_id=orm.sandbox_environment_id,
        environment_updated_at=orm.sandbox_environment_updated_at,
    )


_PIN_CLEARED: Final = {
    "sandbox_agent_id": None,
    "sandbox_agent_version": None,
    "sandbox_environment_id": None,
    "sandbox_environment_updated_at": None,
}


async def clear_sandbox_drift(
    session: AsyncSession,
    routine_id: _uuid.UUID,
    *,
    tenant_id: _uuid.UUID,
    pin: SandboxPin,
) -> bool:
    """Turn off an opt-in whose agent or environment changed since `pin` was recorded.

    Called when a fire found its session no longer matches the pin. Only the
    opt-in that recorded `pin` is cleared, so a newer admin opt-in is never
    undone. The routine's revision (`updated_at`) is left alone so the run
    that noticed, which went ahead without built-ins, still records its
    result. Logs a `via="edit"` history row by `SANDBOX_DRIFT_SETTER`.
    """
    now = datetime.now(UTC)
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(Routine)
            .where(
                Routine.id == routine_id,
                Routine.tenant_id == tenant_id,
                Routine.sandbox_tools_enabled.is_(True),
                Routine.sandbox_agent_id == pin.agent_id,
                Routine.sandbox_agent_version == pin.agent_version,
                Routine.sandbox_environment_id == pin.environment_id,
                Routine.sandbox_environment_updated_at == pin.environment_updated_at,
            )
            .values(
                sandbox_tools_enabled=False,
                sandbox_tools_set_by=SANDBOX_DRIFT_SETTER,
                sandbox_tools_set_at=now,
                **_PIN_CLEARED,
            )
            .execution_options(synchronize_session=False)
        ),
    )
    if not result.rowcount:
        return False
    session.add(
        RoutineSandboxHistoryRecord(
            tenant_id=tenant_id,
            routine_id=routine_id,
            before_enabled=True,
            after_enabled=False,
            set_by=SANDBOX_DRIFT_SETTER,
            set_at=now,
            via="edit",
        )
    )
    await session.flush()
    log.info(
        "routine.sandbox_tools_cleared_by_drift",
        tenant_id=str(tenant_id),
        routine_id=str(routine_id),
    )
    return True


SANDBOX_SETTER_ERASED: Final = "erased"


async def erase_sandbox_setter(
    session: AsyncSession,
    *,
    ids: Collection[str],
    platform_users: Collection[tuple[_uuid.UUID, str]] = (),
) -> int:
    """Privacy erasure: replace a purged person's id as a sandbox opt-in setter.

    `set_by` holds an account id (MCP), a CLI principal id (CLI) or, for a
    panel edit, a platform user id. `ids` are matched anywhere; each
    `platform_users` pair only within its tenant. The id becomes
    `SANDBOX_SETTER_ERASED`; when, which routine and through what are kept, so
    the change log stays complete. Returns the history rows rewritten.
    """
    erased = 0
    for tenant_id, setter in [(None, i) for i in ids] + [(t, u) for t, u in platform_users]:
        history = update(RoutineSandboxHistoryRecord).where(
            RoutineSandboxHistoryRecord.set_by == setter
        )
        routines = update(Routine).where(Routine.sandbox_tools_set_by == setter)
        if tenant_id is not None:
            history = history.where(RoutineSandboxHistoryRecord.tenant_id == tenant_id)
            routines = routines.where(Routine.tenant_id == tenant_id)
        result = cast(
            "CursorResult[Any]",
            await session.execute(history.values(set_by=SANDBOX_SETTER_ERASED)),
        )
        erased += result.rowcount or 0
        await session.execute(
            routines.values(sandbox_tools_set_by=SANDBOX_SETTER_ERASED).execution_options(
                synchronize_session=False
            )
        )
    return erased


async def sandbox_tools_history(
    session: AsyncSession, routine_id: _uuid.UUID, *, tenant_id: _uuid.UUID
) -> list[RoutineSandboxHistoryRecord]:
    """Every recorded sandbox opt-in change for one routine, oldest first."""
    return list(
        (
            await session.scalars(
                select(RoutineSandboxHistoryRecord)
                .where(
                    RoutineSandboxHistoryRecord.tenant_id == tenant_id,
                    RoutineSandboxHistoryRecord.routine_id == routine_id,
                )
                .order_by(RoutineSandboxHistoryRecord.set_at, RoutineSandboxHistoryRecord.id)
            )
        ).all()
    )


async def pause_routine(
    session: AsyncSession,
    routine_id: _uuid.UUID,
    *,
    tenant_id: _uuid.UUID,
    expected_updated_at: datetime | None = None,
) -> RoutineRow | None:
    """Pause the captured revision, erase private output and clear its next slot.

    Missing/cross-tenant rows return None. A stale authorized revision or a
    concurrent configuration edit raises StoreError rather than overwriting it.
    """
    row = await get_routine(session, routine_id, tenant_id=tenant_id)
    if row is None:
        return None
    return await _save_routine_edit(
        session,
        row,
        {"enabled": False, "next_fire_at": None},
        expected_updated_at=expected_updated_at,
    )


async def resume_routine(
    session: AsyncSession,
    routine_id: _uuid.UUID,
    *,
    tenant_id: _uuid.UUID,
    now: datetime,
    expected_updated_at: datetime | None = None,
) -> RoutineRow | None:
    """Resume only the revision whose cron/timezone supplied the next slot.

    Missing/cross-tenant rows return None. A concurrent edit raises StoreError;
    callers must reload/revalidate instead of saving an obsolete computed slot.
    """
    row = await get_routine(session, routine_id, tenant_id=tenant_id)
    if row is None:
        return None
    if row.enabled:
        raise RoutineAlreadyRunning("This routine is already on.")
    nxt = next_slot_at_or_after(row.cron_expr, row.timezone, now)
    return await _save_routine_edit(
        session,
        row,
        {
            "enabled": True,
            "next_fire_at": nxt,
        },
        expected_updated_at=expected_updated_at,
    )


async def record_result(
    session: AsyncSession,
    routine_id: _uuid.UUID,
    *,
    tail: str | None,
    error: str | None,
    delivery: Literal["pending", "skipped"] | None = None,
    delivery_note: str | None = None,
    delivery_payload: str | None = None,
    expected_destination: tuple[str | None, str | None] | None = None,
    expected_private: RoutineRow | None = None,
    expected_row: RoutineRow | None = None,
    producing_agent_id: str | None = None,
) -> None:
    """Set `last_result_tail` and `last_error` in a single UPDATE.

    `error=None` clears `last_error` (sets NULL); `error="..."` sets it.
    `tail` accepts the complete final reply. The preview keeps its last 1000
    characters, with an explicit truncation marker when shortened.

    `delivery_payload` may override the reply with a versioned file-reference
    envelope; the preview always remains plain text.

    `delivery` is only passed for a routine with a destination:
    `pending` queues this fire's complete reply for the adapter to post (copied into
    `delivery_payload`), `skipped` records why it will not be
    (`delivery_note`). Either replaces whatever an earlier fire left in the
    outbox, so only the newest result is ever posted. Omitted, the outbox
    columns are untouched — a routine without a destination, a failed fire,
    and an older scheduler all write exactly what they wrote before, and a
    result still pending from an earlier successful fire stays pending.
    """
    preview = tail
    if tail is not None and len(tail) > LAST_RESULT_TAIL_MAX:
        marker = "… (truncated)\n"
        preview = tail[-(LAST_RESULT_TAIL_MAX - len(marker)) :]
        # Start at a whole word when the retained suffix begins inside one.
        cut = len(tail) - len(preview)
        if not tail[cut - 1].isspace() and not preview[0].isspace():
            first_space = next((i for i, char in enumerate(preview) if char.isspace()), None)
            if first_space is not None:
                preview = preview[first_space + 1 :]
        preview = marker + preview
    values: dict[str, object] = {"last_result_tail": preview, "last_error": error}
    if delivery is not None:
        queued_payload = delivery_payload if delivery_payload is not None else plain_payload(tail)
        values.update(
            delivery_status=delivery,
            delivery_note=(
                OUTBOX_FILES
                if delivery == "pending"
                and queued_payload is not None
                and queued_payload.startswith(PREFIX)
                else delivery_note
            ),
            delivery_payload=queued_payload if delivery == "pending" else None,
            delivery_lease_owner=None,
            delivery_lease_expires_at=None,
            delivered_at=None,
        )
    statement = update(Routine).where(Routine.id == routine_id)
    if expected_destination is not None:
        kind, recipient = expected_destination
        statement = statement.where(
            Routine.destination_kind.is_not_distinct_from(kind),
            Routine.destination_id.is_not_distinct_from(recipient),
        )
    if expected_row is not None:
        statement = statement.where(_captured_routine(expected_row))
    if expected_private is not None:
        statement = statement.where(
            Routine.tenant_id == expected_private.tenant_id,
            Routine.created_by_user_id == expected_private.created_by_user_id,
            Routine.agent_id == expected_private.agent_id,
            Routine.agent_name == expected_private.agent_name,
            Routine.updated_at == expected_private.updated_at,
            Routine.destination_kind == "dm",
            Routine.destination_id == expected_private.destination_id,
        )
        if producing_agent_id is not None:
            # Persist the actual producer, including resolver self-healing, in
            # the same fenced write as its content. Delivery verifies THIS ID.
            values["agent_id"] = producing_agent_id
    await session.execute(statement.values(**values))
    # Completion is independent of content/configuration fences: even a fire
    # whose edited destination rejects its result has finished. Fence by claim
    # time so an older completion cannot settle a newer claim.
    completion = update(Routine).where(Routine.id == routine_id)
    fired_row = expected_row or expected_private
    if fired_row is not None:
        completion = completion.where(
            Routine.tenant_id == fired_row.tenant_id,
            Routine.last_fired_at == fired_row.last_fired_at,
        )
    await session.execute(
        completion.values(
            last_finished_at=datetime.now(UTC), last_finished_claim_at=Routine.last_fired_at
        )
    )
    await session.flush()


async def queue_failure_notice(
    session: AsyncSession,
    routine_id: _uuid.UUID,
    *,
    notice: str,
    expected_row: RoutineRow | None = None,
) -> bool:
    """Queue `notice` for the destination if this failure starts a failing streak.

    Call it before `record_result` writes the failure, in the same transaction:
    the notice is queued only while `last_error` is still null, so a routine
    that keeps failing posts once and stays quiet until a run succeeds. It is
    also never queued for a routine without a destination, and never replaces
    a post already waiting or in flight, so an undelivered result still wins.
    Returns whether a notice was queued.
    """
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(Routine)
            .where(
                Routine.id == routine_id,
                _captured_routine(expected_row) if expected_row is not None else true(),
                Routine.destination_kind.is_not(None),
                Routine.last_error.is_(None),
                or_(
                    Routine.delivery_status.is_(None),
                    Routine.delivery_status.not_in(("pending", "claimed")),
                ),
            )
            .values(
                delivery_status="pending",
                delivery_note=FAILURE_NOTICE,
                delivery_payload=notice,
                delivery_lease_owner=None,
                delivery_lease_expires_at=None,
                delivered_at=None,
            )
        ),
    )
    await session.flush()
    return result.rowcount == 1


async def routine_platform(session: AsyncSession, routine_id: _uuid.UUID) -> str | None:
    """The platform of the routine's tenant, or None for an unknown routine."""
    return await session.scalar(
        select(Tenant.platform)
        .join(Routine, Routine.tenant_id == Tenant.id)
        .where(Routine.id == routine_id)
    )


async def validate_dm_source(
    session: AsyncSession,
    tenant_id: _uuid.UUID,
    channel_id: str | None,
    thread_id: str | None = None,
) -> None:
    """Shared save/dry-run source sealing check, including the original thread."""
    if thread_id is not None and channel_id is None:
        raise StoreError("a source thread requires its parent channel")
    policy = await load_access_policy(session, tenant_id=tenant_id)
    if channel_id is not None and limiting_ids_at(
        policy, channel_id=channel_id, thread_id=thread_id
    ):
        raise StoreError("a routine from a restricted source cannot deliver by DM")


async def _resolve_destination(
    session: AsyncSession,
    tenant_id: _uuid.UUID,
    kind: str | None,
    destination_id: str | None,
    owner: str | None,
) -> str | None:
    if kind == "dm":
        if owner is None or not owner.isdecimal():
            raise StoreError("a DM routine requires a Discord owner")
        if destination_id is not None and destination_id != owner:
            raise StoreError("a DM routine can deliver only to its owner")
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None or tenant.platform != "discord":
            raise StoreError("DM routines are supported only on Discord")
        destination_id = owner
    _check_destination(kind, destination_id)
    return destination_id


def _check_destination(kind: str | None, destination_id: str | None) -> None:
    if (kind is None) != (destination_id is None):
        raise StoreError("destination_kind and destination_id are set together")
    if kind is not None and kind not in ("channel", "thread", "dm"):
        raise StoreError("destination_kind must be channel, thread or dm")
    if destination_id is not None and not destination_id.strip():
        raise StoreError("destination_id must not be empty")


async def claim_routine_deliveries(
    session: AsyncSession,
    *,
    platform: str,
    owner: str,
    now: datetime,
    lease: timedelta,
    limit: int = 20,
) -> list[RoutineRow]:
    """Claim pending result posts for `platform`'s tenants, oldest first.

    At most once: a claim whose lease ran out is never handed out again — the
    owner may have posted before it died — it is settled `skipped` with note
    `interrupted` first. Pending rows are then claimed with `FOR UPDATE SKIP
    LOCKED`, so two adapter processes never take the same row.
    """
    # Archived tenants are left alone: their rows stay pending and post if
    # the workspace comes back.
    tenant_ids = select(Tenant.id).where(Tenant.platform == platform, Tenant.archived_at.is_(None))
    await session.execute(
        update(Routine)
        .where(
            Routine.delivery_status == "claimed",
            Routine.delivery_lease_expires_at < now,
            Routine.tenant_id.in_(tenant_ids),
        )
        .values(
            delivery_status="skipped",
            delivery_note="interrupted",
            delivery_lease_owner=None,
            delivery_lease_expires_at=None,
        )
    )
    due = (
        select(Routine.id)
        .where(Routine.delivery_status == "pending", Routine.tenant_id.in_(tenant_ids))
        .order_by(Routine.last_fired_at.asc().nulls_first())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    result = await session.execute(
        update(Routine)
        .where(Routine.id.in_(due))
        .values(
            delivery_status="claimed",
            delivery_lease_owner=owner,
            delivery_lease_expires_at=now + lease,
        )
        .returning(Routine)
        .execution_options(synchronize_session=False)
    )
    rows = [RoutineRow.model_validate(orm) for orm in result.scalars().all()]
    await session.flush()
    return rows


async def settle_routine_delivery(
    session: AsyncSession,
    routine_id: _uuid.UUID,
    *,
    owner: str,
    status: Literal["delivered", "skipped"],
    now: datetime,
    note: str | None = None,
) -> bool:
    """Finish a claim `owner` still holds. False = the claim was lost."""
    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(Routine)
            .where(
                Routine.id == routine_id,
                Routine.delivery_status == "claimed",
                Routine.delivery_lease_owner == owner,
            )
            .values(
                delivery_status=status,
                delivery_note=note,
                delivered_at=now if status == "delivered" else None,
                delivery_lease_owner=None,
                delivery_lease_expires_at=None,
            )
        ),
    )
    await session.flush()
    return result.rowcount == 1


async def set_last_fired_at(
    session: AsyncSession, routine_id: _uuid.UUID, *, last_fired_at: datetime
) -> None:
    """Directly set `last_fired_at` on one routine.

    Test/seed support for building a routine that has already fired at a known
    timestamp — the scheduler's normal path sets this atomically as part of
    `claim_due_fireable`'s batch claim, which isn't suitable for seeding a
    single row at an arbitrary instant.
    """
    await session.execute(
        update(Routine).where(Routine.id == routine_id).values(last_fired_at=last_fired_at)
    )
    await session.flush()


async def claim_due_fireable(
    session: AsyncSession,
    *,
    now: datetime,
    max_age: timedelta = timedelta(minutes=15),
    limit: int = 20,
    exclude_ids: frozenset[_uuid.UUID] = frozenset(),
) -> list[RoutineRow]:
    """Step 1: atomically claim due rows. Step 2: recompute next_fire_at.

    Returned rows reflect the row state BEFORE step-2 recompute (matches
    predecessor): callers see `next_fire_at=None`, `last_fired_at=now`,
    the original scheduled slot in `last_fired_slot`, and no completion on
    each claimed row. The table itself has the freshly-computed next slot.
    Run-once coalescing retains the overdue slot selected, not the wall time.
    """
    window_start = now - max_age

    # Postgres does not support `UPDATE ... LIMIT`, so we select the ids in a
    # subquery with FOR UPDATE SKIP LOCKED + LIMIT, then UPDATE the outer set.
    id_subq = (
        select(Routine.id)
        .where(
            Routine.enabled.is_(True),
            Routine.tenant_id.in_(select(Tenant.id).where(Tenant.archived_at.is_(None))),
            Routine.next_fire_at.is_not(None),
            or_(Routine.catch_up_policy == "run-once", Routine.next_fire_at >= window_start),
            Routine.id.not_in(exclude_ids),
            Routine.next_fire_at <= now,
        )
        .order_by(Routine.next_fire_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )

    claim_stmt = (
        update(Routine)
        .where(Routine.id.in_(id_subq))
        .values(
            next_fire_at=None,
            last_fired_at=now,
            last_fired_slot=Routine.next_fire_at,
            last_finished_at=None,
            last_finished_claim_at=None,
        )
        .returning(Routine)
        .execution_options(synchronize_session=False)
    )
    claimed_orm = (await session.execute(claim_stmt)).scalars().all()
    claimed_rows = [RoutineRow.model_validate(r) for r in claimed_orm]

    # Step 2: per-row recompute. One bad routine must not block the others —
    # this is the documented predecessor pattern (named boundary catch).
    for row in claimed_rows:
        try:
            nxt = next_slot_at_or_after(row.cron_expr, row.timezone, now)
            await session.execute(
                update(Routine).where(Routine.id == row.id).values(next_fire_at=nxt)
            )
        except Exception:
            log.exception("claim_due_fireable step-2 recompute failed", routine_id=str(row.id))

    await session.flush()
    return claimed_rows


async def advance_stale(
    session: AsyncSession,
    *,
    now: datetime,
    max_age: timedelta = timedelta(minutes=15),
    limit: int = 200,
    in_flight_versions: Mapping[_uuid.UUID, datetime] | None = None,
) -> int:
    """Roll forward skipped-policy stale slots, active-run slots, and NULL orphans.

    Run-once stale slots remain due for claiming. Record skipped ranges without
    overwriting the result of an active run. Returns count touched.
    """
    cutoff = now - max_age
    versions = in_flight_versions or {}
    in_flight_ids = frozenset(versions)
    unchanged_active = or_(
        false(),
        *(
            and_(Routine.id == key, Routine.updated_at == version)
            for key, version in versions.items()
        ),
    )
    stmt = (
        select(Routine)
        .where(
            Routine.enabled.is_(True),
            or_(
                and_(
                    Routine.id.not_in(in_flight_ids),
                    or_(
                        and_(Routine.catch_up_policy == "skip", Routine.next_fire_at < cutoff),
                        Routine.next_fire_at.is_(None),
                    ),
                ),
                and_(unchanged_active, Routine.next_fire_at <= now),
            ),
        )
        .order_by(
            Routine.id.in_(in_flight_ids).desc(),
            Routine.next_fire_at.asc().nullsfirst(),
            Routine.id,
        )
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    rows_orm = (await session.execute(stmt)).scalars().all()
    rows = [RoutineRow.model_validate(r) for r in rows_orm]

    touched = 0
    for row in rows:
        try:
            nxt = next_slot_at_or_after(row.cron_expr, row.timezone, now)
            values: dict[str, datetime | str] = {"next_fire_at": nxt}
            if row.next_fire_at is not None:
                reason = "in_flight" if row.id in in_flight_ids else "stale"
                values.update(
                    last_skipped_from=row.next_fire_at,
                    last_skipped_until=now,
                    last_skip_reason=reason,
                )
                log.info(
                    "scheduler.slots_skipped",
                    routine_id=str(row.id),
                    skipped_from=row.next_fire_at.isoformat(),
                    skipped_until=now.isoformat(),
                    reason=reason,
                )
            await session.execute(update(Routine).where(Routine.id == row.id).values(**values))
            touched += 1
        except Exception:
            log.exception("advance_stale recompute failed", routine_id=str(row.id))

    await session.flush()
    return touched


async def skip_slots_during_fire(
    session: AsyncSession,
    *,
    routine_id: _uuid.UUID,
    finished_at: datetime,
    expected_updated_at: datetime,
) -> None:
    """Skip slots due before completion, unless the user edited this run's schedule."""
    orm = (
        await session.execute(
            select(Routine)
            .where(
                Routine.id == routine_id,
                Routine.enabled.is_(True),
                Routine.updated_at == expected_updated_at,
                Routine.next_fire_at <= finished_at,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if orm is None:
        return
    skipped_from = orm.next_fire_at
    nxt = next_slot_at_or_after(orm.cron_expr, orm.timezone, finished_at)
    await session.execute(
        update(Routine)
        .where(Routine.id == routine_id)
        .values(
            next_fire_at=nxt,
            last_skipped_from=skipped_from,
            last_skipped_until=finished_at,
            last_skip_reason="in_flight",
        )
    )
    log.info(
        "scheduler.slots_skipped",
        routine_id=str(routine_id),
        skipped_from=skipped_from,
        skipped_until=finished_at,
        reason="in_flight",
    )
