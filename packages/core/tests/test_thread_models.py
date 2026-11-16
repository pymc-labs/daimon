"""A conversation's model choice: what can be picked, where it is kept, who may change it.

Real Postgres throughout: the choice is a row, and every property here (kept
across turns, unseen by other threads, refused before any write) is about
what that row says when the next turn is admitted.
"""

from __future__ import annotations

import uuid

import pytest
from anthropic.types.beta import BetaManagedAgentsModelConfig
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.thread_models import (
    get_thread_effort,
    get_thread_model,
    set_thread_model,
)
from daimon.core.thread_models import (
    _MODEL_EFFORTS,  # pyright: ignore[reportPrivateUsage]
    THREAD_MODEL_CHOICES,
    ThreadModelRefused,
    apply_thread_model,
    choose_thread_model,
    conversation_key,
    describe_thread_model,
    may_choose_thread_model,
    model_efforts,
    resolve_choice,
    sent_effort,
)
from daimon.testing import ma_agent
from daimon.testing.factories import make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_AGENT = ma_agent(id="ag_1", name="daimon", model="claude-sonnet-5-5")


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        ("haiku", "claude-haiku-5-5"),
        ("Sonnet", "claude-sonnet-5-5"),
        (" opus ", "claude-opus-5-5"),
        ("fable", "claude-fable-5-2"),
        ("default", None),
    ],
)
def test_a_choice_names_an_allowed_model_or_the_agents_own(
    choice: str, expected: str | None
) -> None:
    assert resolve_choice(choice) == expected


@pytest.mark.parametrize("choice", ["gpt-6", "claude-opus-5-5-fast", ""])
def test_a_model_this_deployment_does_not_offer_is_refused_in_plain_words(choice: str) -> None:
    with pytest.raises(ThreadModelRefused, match="isn't a model this conversation can use"):
        resolve_choice(choice)


def test_a_dm_is_kept_under_its_own_channel_and_a_bare_channel_turn_under_nothing() -> None:
    assert conversation_key(channel_id="C", thread_id="T", is_dm=False) == "T"
    assert conversation_key(channel_id="D", thread_id=None, is_dm=True) == "D"
    assert conversation_key(channel_id="C", thread_id=None, is_dm=False) is None


async def test_a_choice_is_kept_for_its_thread_alone_until_set_back_to_default(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()

    change = await choose_thread_model(
        db_session_factory,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="T1",
        choice="opus",
    )
    assert change.model_id == "claude-opus-5-5"
    assert change.note == "This conversation runs on Opus 5.5 from the next reply."

    for _turn in range(2):
        # Each turn reads the row afresh, so a restart in between changes nothing.
        async with db_session_factory() as session:
            chosen = await apply_thread_model(
                session,
                agent=_AGENT,
                tenant_id=tenant.id,
                platform="discord",
                conversation_id="T1",
                anthropic_backend=True,
            )
            other = await apply_thread_model(
                session,
                agent=_AGENT,
                tenant_id=tenant.id,
                platform="discord",
                conversation_id="T2",
                anthropic_backend=True,
            )
        assert chosen.agent.model.id == "claude-opus-5-5", "the thread keeps its choice"
        assert chosen.model_override == "claude-opus-5-5"
        assert chosen.agent.id == _AGENT.id and chosen.agent.system == _AGENT.system
        assert _AGENT.model.id == "claude-sonnet-5-5", "the stored agent never changes"
        assert other.agent is _AGENT and other.model_override is None, (
            "another thread runs the agent's own model"
        )
        assert not other.fell_back

    assert (
        await describe_thread_model(
            db_session_factory, tenant_id=tenant.id, platform="discord", thread_id="T1"
        )
        == "This conversation runs on Opus 5.5."
    )
    async with db_session_factory() as session:
        assert (
            await get_thread_model(session, tenant_id=tenant.id, platform="slack", thread_id="T1")
            is None
        ), "the same id on another platform is another conversation"

    back = await choose_thread_model(
        db_session_factory,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="T1",
        choice="default",
    )
    assert back.model_id is None
    async with db_session_factory() as session:
        assert (
            await get_thread_model(session, tenant_id=tenant.id, platform="discord", thread_id="T1")
            is None
        )
    assert (
        await describe_thread_model(
            db_session_factory, tenant_id=tenant.id, platform="discord", thread_id="T1"
        )
        == "This conversation runs on the agent's own model."
    )


async def test_a_refused_choice_writes_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    with pytest.raises(ThreadModelRefused):
        await choose_thread_model(
            db_session_factory,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="T1",
            choice="gpt-6",
        )
    async with db_session_factory() as session:
        assert (
            await get_thread_model(session, tenant_id=tenant.id, platform="discord", thread_id="T1")
            is None
        )


@pytest.mark.parametrize(
    ("stored", "anthropic_backend"),
    [("claude-fable-5-1", True), ("claude-opus-5-5", False)],
    ids=["no_longer_allowed", "other_backend"],
)
async def test_a_choice_that_cannot_run_falls_back_to_the_agents_own_model(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    stored: str,
    anthropic_backend: bool,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    async with db_session_factory.begin() as session:
        await set_thread_model(
            session,
            tenant_id=tenant.id,
            platform="discord",
            thread_id="T1",
            model=stored,
        )
    async with db_session_factory() as session:
        applied = await apply_thread_model(
            session,
            agent=_AGENT,
            tenant_id=tenant.id,
            platform="discord",
            conversation_id="T1",
            anthropic_backend=anthropic_backend,
        )
    assert applied.agent is _AGENT, "fail closed: the agent's own model runs"
    assert applied.model_override is None
    assert applied.fell_back, "and the turn is told to say so"


def test_every_offered_model_is_one_this_deployment_prices() -> None:
    from daimon.core.constants import ALLOWED_MODEL_IDS

    assert set(THREAD_MODEL_CHOICES.values()) <= set(ALLOWED_MODEL_IDS)


@pytest.mark.parametrize(
    ("policy", "is_admin", "allowed"),
    [
        (None, False, True),
        (TenantAccessPolicy(protected_channel_ids=("C1",)), False, False),
        (TenantAccessPolicy(protected_channel_ids=("C1",)), True, False),
        (TenantAccessPolicy(protected_channel_ids=("C_OTHER",)), False, True),
    ],
    ids=["open", "protected_member", "protected_admin", "protected_elsewhere"],
)
async def test_only_people_who_may_start_a_turn_there_may_change_the_model(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    policy: TenantAccessPolicy | None,
    is_admin: bool,
    allowed: bool,
) -> None:
    tenant = await make_tenant(db_session)
    if policy is not None:
        await set_access_policy(db_session, tenant_id=tenant.id, policy=policy)
    await db_session.commit()
    assert (
        await may_choose_thread_model(
            db_session_factory,
            None,
            tenant_id=tenant.id,
            platform="discord",
            user_id="U1",
            is_admin=is_admin,
            channel_id="C1",
            thread_id="T1",
        )
        is allowed
    )


_EFFORT_AGENT = _AGENT.model_copy(
    update={
        "model": BetaManagedAgentsModelConfig(
            id="claude-sonnet-5-5", speed="standard", effort="high"
        )
    }
)


async def _applied(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, conversation: str = "T1"
):
    async with factory() as session:
        return await apply_thread_model(
            session,
            agent=_EFFORT_AGENT,
            tenant_id=tenant_id,
            platform="discord",
            conversation_id=conversation,
            anthropic_backend=True,
        )


async def test_a_model_pick_keeps_the_agents_effort_and_the_chosen_one_survives_a_new_model(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    pick = {"tenant_id": tenant.id, "platform": "discord", "thread_id": "T1"}

    await choose_thread_model(db_session_factory, choice="opus", **pick)
    kept = await _applied(db_session_factory, tenant.id)
    assert kept.model_override == "claude-opus-5-5"
    assert kept.model_effort is None, "the agent's own effort is sent, not recorded as a choice"
    assert (kept.agent.model.model_extra or {}).get("effort") == "high"
    assert sent_effort(_EFFORT_AGENT, "claude-opus-5-5", kept.model_effort) == "high"
    assert sent_effort(_EFFORT_AGENT, "claude-haiku-5-5", None) == "high"
    assert sent_effort(_EFFORT_AGENT, "claude-haiku-4-5", None) is None

    change = await choose_thread_model(db_session_factory, effort="low", **pick)
    assert (change.model_id, change.effort) == ("claude-opus-5-5", "low")
    assert change.note == "This conversation runs on Opus 5.5 at low effort from the next reply."

    await choose_thread_model(db_session_factory, choice="haiku", **pick)
    swapped = await _applied(db_session_factory, tenant.id)
    assert swapped.model_override == "claude-haiku-5-5"
    assert swapped.model_effort == "low", "a new model keeps the chosen effort"
    assert (
        await describe_thread_model(db_session_factory, **pick)
        == "This conversation runs on Haiku 5.5 at low effort."
    )


async def test_a_model_that_takes_no_effort_drops_the_agents_and_refuses_a_chosen_one(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(_MODEL_EFFORTS, "claude-haiku-5-5")
    tenant = await make_tenant(db_session)
    await db_session.commit()
    pick = {"tenant_id": tenant.id, "platform": "discord", "thread_id": "T1"}
    agent = _EFFORT_AGENT.model_copy(
        update={
            "model": BetaManagedAgentsModelConfig(
                id="claude-sonnet-5-5", speed="standard", effort="max"
            )
        }
    )

    await choose_thread_model(db_session_factory, choice="opus", effort="low", **pick)
    moved = await choose_thread_model(db_session_factory, choice="haiku", **pick)
    assert (moved.effort, moved.note) == (
        None,
        "This conversation runs on Haiku 5.5 from the next reply.",
    ), "a stored effort the picked model does not take is not shown"
    assert await describe_thread_model(db_session_factory, **pick) == (
        "This conversation runs on Haiku 5.5."
    )
    async with db_session_factory() as session:
        applied = await apply_thread_model(
            session,
            agent=agent,
            tenant_id=tenant.id,
            platform="discord",
            conversation_id="T1",
            anthropic_backend=True,
        )
    assert applied.model_override == "claude-haiku-5-5"
    assert applied.model_effort is None, "the agent's max effort is not sent to Haiku"
    with pytest.raises(ThreadModelRefused, match="Haiku 5.5 doesn't take an effort"):
        await choose_thread_model(db_session_factory, effort="low", **pick)
    with pytest.raises(ThreadModelRefused, match="Haiku 5.5 doesn't take an effort"):
        await choose_thread_model(db_session_factory, choice="haiku", effort="low", **pick)


def test_each_model_takes_only_the_efforts_confirmed_for_it() -> None:
    for model_id in THREAD_MODEL_CHOICES.values():
        assert model_efforts(model_id) == ("low", "medium", "high", "xhigh", "max")
    assert model_efforts("claude-opus-4-6") == ("low", "medium", "high", "max")
    assert model_efforts("claude-haiku-4-5") == ()
    assert model_efforts("claude-unknown") == ()


async def test_an_effort_alone_runs_the_agents_own_model_and_default_hands_it_back(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    pick = {"tenant_id": tenant.id, "platform": "discord", "thread_id": "T1"}

    change = await choose_thread_model(db_session_factory, effort="max", **pick)
    assert change.model_id is None
    assert (
        change.note
        == "This conversation runs on the agent's own model at max effort from the next reply."
    )
    async with db_session_factory() as session:
        assert await get_thread_model(session, **pick) is None
        assert await get_thread_effort(session, **pick) == "max"
    applied = await _applied(db_session_factory, tenant.id)
    assert applied.model_override == "claude-sonnet-5-5"
    assert applied.model_effort == "max"
    assert applied.agent.model.id == "claude-sonnet-5-5"
    assert (await _applied(db_session_factory, tenant.id, "T2")).model_override is None

    back = await choose_thread_model(db_session_factory, effort="default", **pick)
    assert back.effort is None
    assert (await _applied(db_session_factory, tenant.id)).model_override is None

    await choose_thread_model(db_session_factory, choice="opus", effort="low", **pick)
    await choose_thread_model(db_session_factory, choice="default", **pick)
    async with db_session_factory() as session:
        assert await get_thread_effort(session, **pick) is None, "default clears both"


async def test_a_refused_effort_or_an_empty_pick_writes_nothing(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    pick = {"tenant_id": tenant.id, "platform": "discord", "thread_id": "T1"}

    with pytest.raises(ThreadModelRefused, match="isn't an effort"):
        await choose_thread_model(db_session_factory, choice="opus", effort="turbo", **pick)
    with pytest.raises(ThreadModelRefused, match="model, an effort"):
        await choose_thread_model(db_session_factory, **pick)
    async with db_session_factory() as session:
        assert await get_thread_model(session, **pick) is None
        assert await get_thread_effort(session, **pick) is None
