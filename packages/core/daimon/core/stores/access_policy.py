"""Async store for the per-tenant access policy.

Callers own the transaction; writes end with `await session.flush()`.
Caller-owned writes fail fast on contention and must unwind their transaction.
Use policy_write_transaction for bounded retries before any reads or writes.
A missing row is the open default. A row that no longer validates raises
`AccessPolicyUnreadable` so every caller refuses instead of falling open.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from typing import Any, cast

import structlog
from daimon.core._models import Tenant, TenantAccessPolicyRecord
from daimon.core.access_policy import (
    OPEN_ACCESS_POLICY,
    OPEN_RULE,
    ChannelRule,
    TenantAccessPolicy,
    attach_automatic_rules,
)
from daimon.core.errors import DaimonError, UserFacingError
from daimon.core.provider_keys import default_enabled_backends
from daimon.core.session_fence_retry import FenceUnavailable, retry_fences, try_fence
from daimon.core.stores.audience_facts import (
    StoredChannelFact,
    list_channel_facts,
    read_status,
    retire_facts,
)
from mux.profiles import MANAGED_AGENTS
from pydantic import ValidationError
from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)


class AccessPolicyUnreadable(UserFacingError):
    """The stored policy does not parse; callers must refuse, never assume open."""

    def __init__(self, *, tenant_id: uuid.UUID, detail: str | None = None) -> None:
        # Adapters render this text as-is, so it names no tenant id.
        super().__init__(
            detail
            or (
                "the access settings here can't be read, so Daimon can't reply; "
                "ask an admin to fix them"
            )
        )
        self.tenant_id = tenant_id


class AudienceHoldNeedsInternalRoles(UserFacingError):
    """The change would leave `audience_hold` on with no internal roles to tell people apart."""

    reason = "audience_hold_needs_internal_roles"

    def __init__(self) -> None:
        super().__init__(
            "The audience hold is on, so the server needs its internal roles. "
            "Switch the audience hold off first, or keep at least one internal role."
        )


async def _role_lists_changed(
    session: AsyncSession, tenant_id: uuid.UUID, policy: TenantAccessPolicy
) -> None:
    """Make the server's room facts unknown when a role list that built them changes.

    The facts say which rooms outsiders can read, from `internal_role_ids` and
    `team_role_ids`. After a change they are no longer true, so they stop being trusted in
    this transaction; the next reconcile computes them again. Clearing `internal_role_ids`
    while `audience_hold` is `on` is refused: nobody could be told apart.
    """
    try:
        before = await load_access_policy(session, tenant_id=tenant_id)
    except AccessPolicyUnreadable:
        before = None
    if before is not None and (
        before.internal_role_ids == policy.internal_role_ids
        and before.team_role_ids == policy.team_role_ids
    ):
        return
    if (
        not policy.internal_role_ids
        and (before is None or before.internal_role_ids)
        and await audience_mode(session, tenant_id) == "on"
    ):
        raise AudienceHoldNeedsInternalRoles
    await retire_facts(session, tenant_id=tenant_id, now=datetime.now(UTC))


def _policy_write_key(tenant_id: uuid.UUID) -> str:
    # Separate namespace from preparation, mutation, support and tidy fences.
    # Taken with schema_scoped=True, so the lock is per schema and tenant.
    return f"policy_writes:{tenant_id}"


# Longer than tidy's 15-second platform effect, but finite for all policy callers.
POLICY_WRITE_TIMEOUT_S = 20.0


class PolicyBusyError(DaimonError):
    """The policy fence could not be acquired within this caller's budget."""

    def __init__(self) -> None:
        super().__init__("policy is busy, try again")


async def lock_policy_writes_shared(session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
    """Try once; retry_fences must unwind the tidy transaction AND gate on a miss."""
    await try_fence(
        session,
        _policy_write_key(tenant_id),
        shared=True,
        timeout_s=POLICY_WRITE_TIMEOUT_S,
        schema_scoped=True,
    )


async def lock_policy_writes_exclusive(session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
    """Try BEFORE tenant/other rows, including before a policy read/merge.

    A caller-owned transaction cannot safely be replayed here. Contention fails
    fast with PolicyBusyError, so its owner unwinds without waiting in the pool.
    CLI/isolation use policy_write_transaction to retry acquisition before any
    work. Writers never take tidy, post, preparation/mutation or support fences.
    """
    try:
        await try_fence(
            session,
            _policy_write_key(tenant_id),
            timeout_s=POLICY_WRITE_TIMEOUT_S,
            schema_scoped=True,
        )
    except FenceUnavailable:
        raise PolicyBusyError() from None


#: An admin save that meets a holder tries again for about POLICY_WRITE_TIMEOUT_S: a message
#: tidy can hold the fence across platform I/O for up to 15 s. A waiting try holds no lock.
POLICY_LOCK_RETRY_S = 0.25
POLICY_LOCK_ATTEMPTS = int(POLICY_WRITE_TIMEOUT_S / POLICY_LOCK_RETRY_S)


async def lock_policy_writes_exclusive_retrying(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    attempts: int | None = None,
    delay_s: float | None = None,
) -> None:
    """Take the exclusive policy fence, trying again a few times when a short holder has it.

    A failed try takes no lock and leaves the transaction intact, so trying again is safe
    when nothing else ran first. Gives up with `PolicyBusyError` after the last try.
    """
    tries = POLICY_LOCK_ATTEMPTS if attempts is None else attempts
    wait = POLICY_LOCK_RETRY_S if delay_s is None else delay_s
    for attempt in range(1, tries + 1):
        try:
            await lock_policy_writes_exclusive(session, tenant_id=tenant_id)
            return
        except PolicyBusyError:
            if attempt == tries:
                raise
            await asyncio.sleep(wait)


@asynccontextmanager
async def policy_write_transaction(
    factory: async_sessionmaker[AsyncSession], *, tenant_id: uuid.UUID
) -> AsyncIterator[AsyncSession]:
    """Acquire exclusive -> tenant row; release every failed transaction before backoff.

    Retry only acquisition, never the caller's writes or MA side effects. Store
    functions can reacquire this transaction's exclusive lock without waiting.
    """

    async def acquire() -> tuple[AsyncExitStack, AsyncSession]:
        stack = AsyncExitStack()
        session: AsyncSession | None = None
        try:
            session = await stack.enter_async_context(factory.begin())
            await try_fence(
                session,
                _policy_write_key(tenant_id),
                timeout_s=POLICY_WRITE_TIMEOUT_S,
                schema_scoped=True,
            )
        except BaseException:
            if session is not None:
                await session.rollback()
            await stack.aclose()
            raise
        return stack, session

    stack, session = await retry_fences(acquire, busy_error=PolicyBusyError)
    async with stack:
        yield session


async def lock_access_policy(session: AsyncSession, *, tenant_id: uuid.UUID) -> None:
    """Serialize operator policy edits, including when no policy row exists.

    Policy writers take lock_policy_writes_exclusive BEFORE this lock and
    before loading/merging or clearing, in the same transaction as the write.
    Non-policy mutations retain this row lock alone. A private form's consume
    takes it too, before its pin check. FOR NO KEY UPDATE: holders exclude
    each other, but rows keyed to the tenant can still
    be inserted (their foreign-key check only takes KEY SHARE), so a writer
    holding another lock before such an insert can't deadlock with a holder.
    """
    (
        await session.execute(
            select(Tenant.id).where(Tenant.id == tenant_id).with_for_update(key_share=True)
        )
    ).scalar_one()


async def _roles_supported(
    session: AsyncSession, tenant_id: uuid.UUID, policy: TenantAccessPolicy
) -> bool:
    """Role access needs a Discord tenant and never names @everyone (the guild id)."""
    role_ids = (
        *policy.invoker_role_ids,
        *policy.internal_role_ids,
        *policy.team_role_ids,
        *(role for rule in policy.agent_rules.values() for role in rule.invoker_role_ids),
    )
    if not role_ids:
        return True
    tenant = await session.get(Tenant, tenant_id)
    return (
        tenant is not None and tenant.platform == "discord" and tenant.external_id not in role_ids
    )


def _with_default_backends(policy: TenantAccessPolicy) -> TenantAccessPolicy:
    default = default_enabled_backends()
    if policy.enabled_backends == default:
        return policy
    return policy.model_copy(update={"enabled_backends": default})


async def load_access_policy(session: AsyncSession, *, tenant_id: uuid.UUID) -> TenantAccessPolicy:
    """The tenant's stored policy, with the computed narrow-room rules when the server's
    audience mode is `on`. Writers save without them: they are never dumped.
    """
    return await _with_automatic_rules(
        session, tenant_id, await _load_stored_policy(session, tenant_id=tenant_id)
    )


_INSIDE_RULE = ChannelRule(readers="inside")
_SHADOW_LOGGED: dict[uuid.UUID, float] = {}
_SHADOW_EVERY = 60.0


async def audience_mode(session: AsyncSession, tenant_id: uuid.UUID) -> str:
    """The server's `audience_hold` mode (`daimon.core.channel_audience`). Imported here, not
    at the top: the feature and config modules import this store."""
    from daimon.core.channel_audience import audience_mode as read_mode
    from daimon.core.features.snapshot import FeaturePlace

    return await read_mode(session, FeaturePlace(tenant_id, "discord"))


_Automatic = tuple[dict[str, ChannelRule], bool, bool]
"""The rules, whether a room with no fact is narrow, and whether the facts are missing."""
_CACHE_KEY = "audience_automatic_rules"
_Held = tuple[object, dict[uuid.UUID, _Automatic | None]]
"""A transaction and what was read inside it."""


async def _with_automatic_rules(
    session: AsyncSession, tenant_id: uuid.UUID, policy: TenantAccessPolicy
) -> TenantAccessPolicy:
    """Attach the computed rules, if the mode is `on`. Off costs one mode read, and a
    transaction reads each server once: a later load in it reuses the answer."""
    # Valid for one transaction only: a commit or rollback starts a new one, and facts
    # can expire or turn narrow between them.
    transaction = session.get_transaction()
    held = cast(
        "tuple[object, dict[uuid.UUID, _Automatic | None]] | None", session.info.get(_CACHE_KEY)
    )
    if held is None or held[0] is not transaction:
        held = (transaction, dict[uuid.UUID, _Automatic | None]())
        session.info[_CACHE_KEY] = held
    cache = held[1]
    if tenant_id not in cache:
        cache[tenant_id] = await _automatic_rules(session, tenant_id)
    automatic = cache[tenant_id]
    if automatic is None:
        return policy
    rules, unknown_narrow, facts_missing = automatic
    return attach_automatic_rules(
        policy, rules, unknown_narrow=unknown_narrow, facts_missing=facts_missing
    )


async def _automatic_rules(session: AsyncSession, tenant_id: uuid.UUID) -> _Automatic | None:
    # The mode first: it is the only read a server with the hold off pays for.
    mode = await audience_mode(session, tenant_id)
    if mode == "off":
        return None
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None or tenant.platform != "discord":
        return None
    if mode == "shadow":
        # Compute and log, at most once a minute for each server; enforce nothing.
        if _shadow_due(tenant_id):
            # Shadow enforces nothing: a failed facts read is logged, never a refusal.
            try:
                async with session.begin_nested():
                    rooms, usable = await _stored_rooms(session, tenant_id)
            except Exception:
                log.warning("audience.shadow_facts_failed", tenant_id=str(tenant_id), exc_info=True)
            else:
                _log_shadow(tenant_id, narrow=sum(room.narrow for room in rooms), usable=usable)
        return None
    rooms, usable = await _stored_rooms(session, tenant_id)
    rules = {room.channel_id: _INSIDE_RULE if room.narrow else OPEN_RULE for room in rooms}
    return rules, True, not usable  # a room with no fact is narrow


async def _stored_rooms(
    session: AsyncSession, tenant_id: uuid.UUID
) -> tuple[list[StoredChannelFact], bool]:
    status = await read_status(session, tenant_id=tenant_id, now=datetime.now(UTC))
    usable = status is not None and status.usable
    return (await list_channel_facts(session, tenant_id=tenant_id) if usable else []), usable


def _shadow_due(tenant_id: uuid.UUID) -> bool:
    last = _SHADOW_LOGGED.get(tenant_id)
    return last is None or time.monotonic() - last >= _SHADOW_EVERY


def _log_shadow(tenant_id: uuid.UUID, *, narrow: int, usable: bool) -> None:
    """One `audience.shadow` line a minute for each server: what the automatic rule would seal."""
    _SHADOW_LOGGED[tenant_id] = time.monotonic()
    log.info(
        "audience.shadow",
        kind="narrow_rule",
        tenant_id=str(tenant_id),
        channel_id=None,
        agent=None,
        reason=f"narrow_rooms={narrow} facts_usable={usable}",
    )


async def _load_stored_policy(session: AsyncSession, *, tenant_id: uuid.UUID) -> TenantAccessPolicy:
    # Select the row, not just its payload: a JSON `null` payload would
    # otherwise read as "no row" and fall open.
    record = (
        await session.execute(
            select(TenantAccessPolicyRecord).where(TenantAccessPolicyRecord.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if record is None:
        return _with_default_backends(OPEN_ACCESS_POLICY)
    payload = cast(object, record.policy)
    if isinstance(payload, dict):
        if "enabled_backends" in payload or "backends_explicit" in payload:
            raise AccessPolicyUnreadable(tenant_id=tenant_id)
        explicit = record.enabled_backends_explicit
        payload = {
            **cast("dict[str, Any]", payload),
            "enabled_backends": record.enabled_backends
            if explicit
            else list(default_enabled_backends()),
            "backends_explicit": explicit,
        }
    try:
        # JSON null, an array or a string fail validation like a bad field does.
        policy = TenantAccessPolicy.model_validate(payload)
        if not await _roles_supported(session, tenant_id, policy):
            raise AccessPolicyUnreadable(
                tenant_id=tenant_id,
                detail=(
                    "this space has unsupported role access settings; ask an admin to fix them"
                ),
            )
        return policy
    except ValidationError as exc:
        raise AccessPolicyUnreadable(tenant_id=tenant_id) from exc


async def set_access_policy(
    session: AsyncSession, *, tenant_id: uuid.UUID, policy: TenantAccessPolicy
) -> None:
    await lock_policy_writes_exclusive_retrying(session, tenant_id=tenant_id)
    await lock_access_policy(session, tenant_id=tenant_id)
    if not await _roles_supported(session, tenant_id, policy):
        raise ValueError(
            "Discord roles require a Discord tenant and cannot include @everyone (guild id)"
        )
    await _role_lists_changed(session, tenant_id, policy)
    payload = policy.model_dump(mode="json")
    # A list that differs from the deployment default is a choice even when the
    # caller rebuilt the policy from a defaulted one (a model_dump round trip).
    explicit = payload.pop("backends_explicit") or tuple(policy.enabled_backends) != (
        default_enabled_backends()
    )
    # A tenant on the deployment default keeps following it: its loaded default is
    # never written back as a choice, and older builds read Anthropic only.
    chosen = payload.pop("enabled_backends")
    backends = chosen if explicit else [MANAGED_AGENTS.provider]
    # Older readers already imply the default admin bypass and forbid unknown keys.
    if policy.invoker_admin_bypass:
        payload.pop("invoker_admin_bypass")
    # Leave empty keys out, so a build from before them still reads a row
    # that sets none of them.
    for key in (
        "channel_rules",
        "category_rules",
        "agent_rules",
        "invoker_role_ids",
        "internal_role_ids",
        "team_role_ids",
    ):
        if not payload.get(key):
            payload.pop(key, None)
    await session.execute(
        insert(TenantAccessPolicyRecord)
        .values(
            tenant_id=tenant_id,
            policy=payload,
            enabled_backends=backends,
            enabled_backends_explicit=explicit,
        )
        .on_conflict_do_update(
            index_elements=[TenantAccessPolicyRecord.tenant_id],
            set_={
                "policy": payload,
                "enabled_backends": backends,
                "enabled_backends_explicit": explicit,
                "updated_at": func.now(),
            },
        )
    )
    await session.flush()


async def clear_access_policy(session: AsyncSession, *, tenant_id: uuid.UUID) -> bool:
    """Delete the tenant's row, putting it back on the open default. True if one existed."""
    await lock_policy_writes_exclusive_retrying(session, tenant_id=tenant_id)
    await lock_access_policy(session, tenant_id=tenant_id)
    await _role_lists_changed(session, tenant_id, TenantAccessPolicy())
    result = await session.execute(
        delete(TenantAccessPolicyRecord).where(TenantAccessPolicyRecord.tenant_id == tenant_id)
    )
    await session.flush()
    return cast(CursorResult[Any], result).rowcount > 0
