"""Admission applies a conversation's model choice to that conversation's turns only.

Each admission reads the choice afresh, so it holds across turns and across a
restart (new deps, new caches, same database), and no other thread sees it.
"""

from __future__ import annotations

from pathlib import Path

from daimon.core.stores.thread_models import set_thread_model
from daimon.core.thread_models import choose_thread_model
from daimon.core.turn.admission import Admission, admit
from daimon.core.turn_hold import RequesterOrg
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_admission import _NOW, _admittable_router, _deps, _seed_admittable_tenant


async def _admit_in(
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    tenant,  # type: ignore[no-untyped-def]
    thread_id: str,
) -> Admission:
    deps = _deps(
        sessionmaker=db_session_factory, defaults_root=tmp_path, router=_admittable_router(tenant)
    )
    return await admit(
        deps,
        origin="chat",
        tenant_id=tenant.id,
        platform="discord",
        external_user_id="member-1",
        channel_id="chan-1",
        thread_id=thread_id,
        now=_NOW,
        verified_membership=True,
        requester_org=RequesterOrg.HOST,
    )


async def test_the_chosen_model_runs_its_thread_across_turns_and_restarts(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    own_model = (await _admit_in(db_session_factory, tmp_path, tenant, "thr-1")).agent.model.id
    assert own_model != "claude-opus-5-5"

    await choose_thread_model(
        db_session_factory,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="thr-1",
        choice="opus",
    )

    for _restart in range(2):
        # Fresh deps each time: nothing but the database carries the choice.
        chosen = await _admit_in(db_session_factory, tmp_path, tenant, "thr-1")
        assert chosen.agent.model.id == "claude-opus-5-5"
        assert chosen.model_override == "claude-opus-5-5", "the session is created with it"
        assert not chosen.model_fell_back

    other = await _admit_in(db_session_factory, tmp_path, tenant, "thr-2")
    assert other.agent.model.id == own_model, "another thread keeps the agent's own model"
    assert other.model_override is None


async def test_a_choice_no_longer_allowed_runs_the_agents_own_model_and_says_so(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    tenant = await _seed_admittable_tenant(db_session, policy=None)
    own_model = (await _admit_in(db_session_factory, tmp_path, tenant, "thr-1")).agent.model.id
    async with db_session_factory.begin() as session:
        await set_thread_model(
            session,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="thr-1",
            model="claude-retired-1",
        )

    admission = await _admit_in(db_session_factory, tmp_path, tenant, "thr-1")

    assert admission.agent.model.id == own_model, "fail closed to the agent's own model"
    assert admission.model_override is None
    assert admission.model_fell_back, "the turn carries the plain notice"
