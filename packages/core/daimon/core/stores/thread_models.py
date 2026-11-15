"""Async store for the model a conversation's people chose (`thread_model_settings`),
and each agent's consent to run it on another provider (`thread_model_consents`).

Callers own the transaction; every write ends with `await session.flush()`.
No try/except -- exceptions propagate to the adapter boundary.
"""

from __future__ import annotations

import uuid

from daimon.core._models import ThreadModelConsent, ThreadModelSetting
from daimon.core.stores.domain import AgentBackendRevision
from mux.contracts.config import DEFAULT_BACKEND
from mux.contracts.ids import ModelRef, Provider
from pydantic import TypeAdapter
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession


async def get_thread_model(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, thread_id: str
) -> str | None:
    """The model id chosen for this conversation, or None for the agent's own."""
    return await session.scalar(
        select(ThreadModelSetting.model).where(
            ThreadModelSetting.tenant_id == tenant_id,
            ThreadModelSetting.platform == platform,
            ThreadModelSetting.thread_id == thread_id,
        )
    )


_PROVIDER: TypeAdapter[Provider] = TypeAdapter(Provider)


async def get_thread_choice(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, thread_id: str
) -> ModelRef | None:
    """The model chosen for this conversation with its provider; a legacy row is Claude's."""
    row = (
        await session.execute(
            select(ThreadModelSetting.model, ThreadModelSetting.provider).where(
                ThreadModelSetting.tenant_id == tenant_id,
                ThreadModelSetting.platform == platform,
                ThreadModelSetting.thread_id == thread_id,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    provider = _PROVIDER.validate_python(row.provider or DEFAULT_BACKEND)
    return ModelRef(provider=provider, id=row.model)


async def set_thread_model(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
    model: str,
    provider: Provider | None = None,
) -> None:
    """Choose `model`; `provider` None (or Claude's) keeps the legacy Claude-only row."""
    stored = None if provider in (None, DEFAULT_BACKEND) else provider
    stmt = (
        pg_insert(ThreadModelSetting)
        .values(
            tenant_id=tenant_id,
            platform=platform,
            thread_id=thread_id,
            model=model,
            provider=stored,
            updated_at=func.now(),
        )
        .on_conflict_do_update(
            constraint="pk_thread_model_settings",
            set_={"model": model, "provider": stored, "updated_at": func.now()},
        )
    )
    await session.execute(stmt)
    await session.flush()


async def get_thread_consent(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform: str,
    thread_id: str,
    agent_id: uuid.UUID,
) -> AgentBackendRevision | None:
    """The agent's consent to this conversation's model, as a backend revision."""
    raw = await session.scalar(
        select(ThreadModelConsent.revision).where(
            ThreadModelConsent.tenant_id == tenant_id,
            ThreadModelConsent.platform == platform,
            ThreadModelConsent.thread_id == thread_id,
            ThreadModelConsent.agent_id == agent_id,
        )
    )
    return AgentBackendRevision.model_validate(raw) if raw is not None else None


async def put_thread_consent(
    session: AsyncSession, *, platform: str, thread_id: str, revision: AgentBackendRevision
) -> None:
    """Record (or replace) `revision.agent_id`'s consent for this conversation."""
    body = revision.model_dump(mode="json")
    stmt = (
        pg_insert(ThreadModelConsent)
        .values(
            tenant_id=revision.tenant_id,
            platform=platform,
            thread_id=thread_id,
            agent_id=revision.agent_id,
            revision=body,
            digest=revision.digest,
        )
        .on_conflict_do_update(
            constraint="pk_thread_model_consents",
            set_={"revision": body, "digest": revision.digest, "created_at": func.now()},
        )
    )
    await session.execute(stmt)
    await session.flush()


async def clear_thread_model(
    session: AsyncSession, *, tenant_id: uuid.UUID, platform: str, thread_id: str
) -> None:
    """Back to the agent's own model; every agent's consent for it goes too."""
    await session.execute(
        delete(ThreadModelSetting).where(
            ThreadModelSetting.tenant_id == tenant_id,
            ThreadModelSetting.platform == platform,
            ThreadModelSetting.thread_id == thread_id,
        )
    )
    await session.execute(
        delete(ThreadModelConsent).where(
            ThreadModelConsent.tenant_id == tenant_id,
            ThreadModelConsent.platform == platform,
            ThreadModelConsent.thread_id == thread_id,
        )
    )
    await session.flush()


async def delete_consents_for_agent(session: AsyncSession, *, agent_id: uuid.UUID) -> None:
    """A deleted agent's thread consents: no agents table cascades them."""
    await session.execute(delete(ThreadModelConsent).where(ThreadModelConsent.agent_id == agent_id))
