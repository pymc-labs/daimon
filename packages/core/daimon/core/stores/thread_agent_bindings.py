"""Tenant and location scoped setup conversations, shared by their participants."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from daimon.core._models import ThreadAgentBinding
from daimon.core.continuity.handoff import HandoffRefusedInSetupThread
from daimon.core.stores.domain import ThreadAgentBindingRow
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession


async def create_binding(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    parent_channel_id: str,
    thread_id: str,
    responder_ma_agent_id: str,
    responder_name: str,
    configuration_target_ma_agent_id: str | None = None,
    configuration_target_name: str | None = None,
    creator_account_id: uuid.UUID | None = None,
    kind: Literal["setup", "handoff"] = "setup",
) -> ThreadAgentBindingRow:
    """Bind a thread to a responder.

    `kind` says why the binding exists: `'setup'` is a setup conversation,
    `'handoff'` records that a task in an ordinary thread was handed to a
    different agent. They share the table because both answer the same
    question — who replies in this thread — but only setup bindings are
    setup conversations, which is why `list_active_bindings` filters on it.
    """
    binding = ThreadAgentBinding(
        tenant_id=tenant_id,
        platform=platform,
        parent_channel_id=parent_channel_id,
        thread_id=thread_id,
        kind=kind,
        responder_ma_agent_id=responder_ma_agent_id,
        responder_name=responder_name,
        configuration_target_ma_agent_id=configuration_target_ma_agent_id,
        configuration_target_name=configuration_target_name,
        creator_account_id=creator_account_id,
    )
    session.add(binding)
    await session.flush()
    return ThreadAgentBindingRow.model_validate(binding)


async def get_binding(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    parent_channel_id: str,
    thread_id: str,
) -> ThreadAgentBindingRow | None:
    binding = (
        await session.execute(
            select(ThreadAgentBinding).where(
                ThreadAgentBinding.tenant_id == tenant_id,
                ThreadAgentBinding.platform == platform,
                ThreadAgentBinding.parent_channel_id == parent_channel_id,
                ThreadAgentBinding.thread_id == thread_id,
            )
        )
    ).scalar_one_or_none()
    return ThreadAgentBindingRow.model_validate(binding) if binding is not None else None


async def get_binding_by_id(
    session: AsyncSession, *, id: uuid.UUID
) -> ThreadAgentBindingRow | None:
    """Read a binding the config cascade already resolved, by its row id.

    The turn pipeline carries `thread_binding_id` on the resolved config, so a
    caller that needs the binding itself (to see whether this thread's task was
    handed over) has the id and not the location the location-scoped
    `get_binding` above requires.
    """
    binding = await session.get(ThreadAgentBinding, id)
    return ThreadAgentBindingRow.model_validate(binding) if binding is not None else None


async def list_active_bindings(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    parent_channel_id: str,
    limit: int = 10,
) -> list[ThreadAgentBindingRow]:
    """Live setup conversations under a channel.

    Setup only, by design: every caller of this lists setup conversations to a
    user. A handoff binding is a fact about one ordinary thread, not a setup
    conversation, and surfacing it here would misdescribe the thread.
    """
    bindings = (
        await session.execute(
            select(ThreadAgentBinding)
            .where(
                ThreadAgentBinding.tenant_id == tenant_id,
                ThreadAgentBinding.platform == platform,
                ThreadAgentBinding.parent_channel_id == parent_channel_id,
                ThreadAgentBinding.kind == "setup",
                ThreadAgentBinding.archived.is_(False),
                ThreadAgentBinding.locked.is_(False),
                ThreadAgentBinding.deleted.is_(False),
            )
            .order_by(ThreadAgentBinding.updated_at.desc(), ThreadAgentBinding.id)
            .limit(min(limit, 10))
        )
    ).scalars()
    return [ThreadAgentBindingRow.model_validate(binding) for binding in bindings]


async def list_active_setup_bindings_for_tenant(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    limit: int = 10,
) -> tuple[list[ThreadAgentBindingRow], bool]:
    """Live setup conversations anywhere in one tenant, newest first.

    The tenant-wide sibling of `list_active_bindings`: same filters minus the
    channel, because a reader asking where setup is happening is asking about
    the whole install, not one channel. Setup only, for the reason
    `list_active_bindings` gives.

    Reads one row past `limit` so the second element of the return can say
    whether more live conversations exist without a second COUNT query; the
    extra row is dropped from the list.
    """
    bindings = (
        (
            await session.execute(
                select(ThreadAgentBinding)
                .where(
                    ThreadAgentBinding.tenant_id == tenant_id,
                    ThreadAgentBinding.platform == platform,
                    ThreadAgentBinding.kind == "setup",
                    ThreadAgentBinding.archived.is_(False),
                    ThreadAgentBinding.locked.is_(False),
                    ThreadAgentBinding.deleted.is_(False),
                )
                .order_by(ThreadAgentBinding.updated_at.desc(), ThreadAgentBinding.id)
                .limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    rows = [ThreadAgentBindingRow.model_validate(binding) for binding in bindings[:limit]]
    return rows, len(bindings) > limit


async def update_target(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    parent_channel_id: str,
    thread_id: str,
    configuration_target_ma_agent_id: str,
    configuration_target_name: str,
) -> ThreadAgentBindingRow | None:
    binding = (
        await session.execute(
            update(ThreadAgentBinding)
            .where(
                ThreadAgentBinding.tenant_id == tenant_id,
                ThreadAgentBinding.platform == platform,
                ThreadAgentBinding.parent_channel_id == parent_channel_id,
                ThreadAgentBinding.thread_id == thread_id,
                ThreadAgentBinding.deleted.is_(False),
            )
            .values(
                configuration_target_ma_agent_id=configuration_target_ma_agent_id,
                configuration_target_name=configuration_target_name,
                updated_at=func.now(),
            )
            .returning(ThreadAgentBinding)
        )
    ).scalar_one_or_none()
    await session.flush()
    return ThreadAgentBindingRow.model_validate(binding) if binding is not None else None


async def update_lifecycle(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    parent_channel_id: str,
    thread_id: str,
    archived: bool | None = None,
    locked: bool | None = None,
    deleted: bool | None = None,
) -> None:
    values: dict[str, object] = {"updated_at": func.now()}
    for name, value in (("archived", archived), ("locked", locked), ("deleted", deleted)):
        if value is not None:
            values[name] = value
    await session.execute(
        update(ThreadAgentBinding)
        .where(
            ThreadAgentBinding.tenant_id == tenant_id,
            ThreadAgentBinding.platform == platform,
            ThreadAgentBinding.parent_channel_id == parent_channel_id,
            ThreadAgentBinding.thread_id == thread_id,
            ThreadAgentBinding.deleted.is_(False),
        )
        .values(**values)
    )
    await session.flush()


async def update_channel_lifecycle(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    parent_channel_id: str,
    archived: bool | None = None,
    deleted: bool | None = None,
) -> None:
    values: dict[str, object] = {"updated_at": func.now()}
    if archived is not None:
        values["archived"] = archived
    if deleted is not None:
        values["deleted"] = deleted
    await session.execute(
        update(ThreadAgentBinding)
        .where(
            ThreadAgentBinding.tenant_id == tenant_id,
            ThreadAgentBinding.platform == platform,
            ThreadAgentBinding.parent_channel_id == parent_channel_id,
            ThreadAgentBinding.deleted.is_(False),
        )
        .values(**values)
    )
    await session.flush()


async def upsert_responder_binding(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    parent_channel_id: str,
    thread_id: str,
    responder_ma_agent_id: str,
    responder_name: str,
    created_by_account_id: uuid.UUID | None,
    now: datetime,
) -> ThreadAgentBindingRow:
    """Record that this thread's task was handed to `responder_ma_agent_id`.

    The location row is taken `FOR UPDATE` first, so two handoffs racing in one
    thread serialize instead of colliding on the unique location constraint,
    and a setup conversation opened in between is still seen. Over a
    `kind='setup'` row this raises `HandoffRefusedInSetupThread`: a setup
    conversation has to answer as the built-in Daimon, and overwriting its
    responder would break the admission path that asserts exactly that.

    Handing a task on again just rewrites the existing handoff row, and clears
    its configuration target -- a target belongs to a setup conversation, and a
    stale one here would tell the next turn to configure an agent nobody chose.
    """
    existing = (
        await session.execute(
            select(ThreadAgentBinding)
            .where(
                ThreadAgentBinding.tenant_id == tenant_id,
                ThreadAgentBinding.platform == platform,
                ThreadAgentBinding.parent_channel_id == parent_channel_id,
                ThreadAgentBinding.thread_id == thread_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing is not None and existing.kind == "setup":
        raise HandoffRefusedInSetupThread(
            "This is a setup conversation; it always answers as Daimon."
        )
    if existing is None:
        return await create_binding(
            session,
            tenant_id=tenant_id,
            platform=platform,
            parent_channel_id=parent_channel_id,
            thread_id=thread_id,
            responder_ma_agent_id=responder_ma_agent_id,
            responder_name=responder_name,
            creator_account_id=created_by_account_id,
            kind="handoff",
        )
    updated = (
        await session.execute(
            update(ThreadAgentBinding)
            .where(ThreadAgentBinding.id == existing.id)
            .values(
                responder_ma_agent_id=responder_ma_agent_id,
                responder_name=responder_name,
                configuration_target_ma_agent_id=None,
                configuration_target_name=None,
                deleted=False,
                updated_at=now,
            )
            .returning(ThreadAgentBinding)
        )
    ).scalar_one()
    await session.flush()
    return ThreadAgentBindingRow.model_validate(updated)
