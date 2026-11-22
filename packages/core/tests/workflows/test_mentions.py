"""Mention triggers: a tag in a listened-to channel starts one run as its author; anything
else stays ordinary chat and records nothing."""

from __future__ import annotations

import uuid
from typing import Any, Literal

import pytest
from daimon.core._models import Account, PlatformPrincipal, WorkflowRun, WorkflowTriggerEvent
from daimon.core.config import Settings
from daimon.core.workflows import delivery, mentions, runs
from daimon.core.workflows.mentions import Mention
from daimon.core.workflows.preflight import Refusal
from daimon.core.workflows.runs import Sessions
from daimon.testing.workflows import NOW, make_settings
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_triggers import _publish, _workspace  # pyright: ignore[reportPrivateUsage]

DONE: dict[str, Any] = {"f": {"kind": "finish"}}
TAG = {"id": "tag", "kind": "mention", "channels": ["C1"], "match": "message.text.contains('help')"}
ON = {"workflows": "on", "workflow_mentions": "on"}


async def NO_LINKS(*_: object) -> None:
    return None


def _mention(
    tenant_id: uuid.UUID, text: str = "help me", *, channel: str = "C1", author: str = "u1"
) -> Mention:
    return Mention(
        tenant_id=tenant_id,
        platform="slack",
        author=author,
        message_id="m1",
        text=text,
        channel_id=channel,
        thread_id="t1",
        link="https://chat/m1",
    )


async def _setup(
    db: AsyncSession, nodes: dict[str, Any] = DONE, **spec: Any
) -> tuple[Sessions, uuid.UUID, uuid.UUID]:
    tenant_id, owner = await _workspace(db)
    db.add(_principal(tenant_id, "u1", owner))
    start = next(iter(nodes))
    flow = await _publish(db, tenant_id, owner, start=start, nodes=nodes, triggers=[TAG], **spec)
    await db.commit()
    return async_sessionmaker(bind=db.bind, expire_on_commit=False), tenant_id, flow


def _principal(tenant_id: uuid.UUID, user: str, account_id: uuid.UUID) -> PlatformPrincipal:
    return PlatformPrincipal(
        tenant_id=tenant_id, platform="slack", external_id=user, account_id=account_id
    )


def _on(features: dict[str, str] = ON) -> Settings:
    return make_settings().model_copy(update={"features": features})


async def _counts(db: AsyncSession) -> tuple[int, int]:
    events = await db.scalar(select(func.count()).select_from(WorkflowTriggerEvent))
    run_count = await db.scalar(select(func.count()).select_from(WorkflowRun))
    return events or 0, run_count or 0


async def test_a_mention_starts_one_run_as_its_author_and_a_redelivery_starts_none(
    db_session: AsyncSession,
) -> None:
    sm, tenant_id, _ = await _setup(db_session)
    mention = _mention(tenant_id)
    claimed = await mentions.claim_mention(sm, _on(), mention, NO_LINKS, NOW)
    assert claimed is not None and claimed.card is not None, claimed
    assert "Run 1" in claimed.card.headline, claimed.card
    run = await db_session.scalar(select(WorkflowRun))
    assert run is not None and run.trigger_kind == "mention" and run.untrusted_inputs, run
    owner = await db_session.scalar(select(PlatformPrincipal.account_id))
    assert run.run_as_account_id == owner, "the author runs it"
    assert run.inputs["thread"]["id"] == "t1", "the thread is the input"
    assert run.origin == {
        "platform": "slack",
        "channel": "C1",
        "message": "m1",
        "thread": "t1",
        "link": "https://chat/m1",
    }
    target = delivery.origin_target(run.origin or {}, home=None)
    assert target["channel_id"] == "C1:t1", "replies in the thread"
    again = await mentions.claim_mention(sm, _on(), mention, NO_LINKS, NOW)
    assert again == mentions.Claimed(), "a redelivered message is claimed with nothing to post"
    assert (await _counts(db_session))[1] == 1


@pytest.mark.parametrize("case", ["mentions_off", "other_channel", "no_match", "stranger"])
async def test_a_mention_that_starts_nothing_stays_chat(
    db_session: AsyncSession, case: str
) -> None:
    sm, tenant_id, flow = await _setup(db_session)
    stranger = Account(tenant_id=tenant_id)
    db_session.add(stranger)
    await db_session.flush()
    db_session.add(_principal(tenant_id, "u2", stranger.id))
    await db_session.commit()
    mention = {
        "mentions_off": _mention(tenant_id),
        "other_channel": _mention(tenant_id, channel="C2"),
        "no_match": _mention(tenant_id, "hello"),
        "stranger": _mention(tenant_id, author="u2"),
    }[case]
    settings = _on({"workflows": "on"}) if case == "mentions_off" else _on()
    assert await mentions.claim_mention(sm, settings, mention, NO_LINKS, NOW) is None
    assert await _counts(db_session) == (0, 0), "nothing is recorded"
    if case == "mentions_off":
        refused = await runs.start_run(
            sm,
            settings,
            tenant_id=tenant_id,
            start=runs.Start(flow, run_as_account_id=None, triggered_by={"kind": "person"}),
            now=NOW,
        )
        assert isinstance(refused, Refusal) and refused.reason == "mentions_off", refused


async def test_a_held_concurrency_key_leaves_the_mention_to_chat(db_session: AsyncSession) -> None:
    agent = {"a": {"kind": "agent", "instructions": "Do it", "next": "f"}, **DONE}
    sm, tenant_id, flow = await _setup(
        db_session, agent, concurrency={"key": "'one'", "on_conflict": "skip"}
    )
    owner = await db_session.scalar(select(PlatformPrincipal.account_id))
    busy = await runs.start_run(
        sm,
        _on(),
        tenant_id=tenant_id,
        start=runs.Start(flow, run_as_account_id=owner, triggered_by={"kind": "person"}),
        now=NOW,
    )
    assert not isinstance(busy, Refusal) and busy.status in ("queued", "running"), busy
    assert await mentions.claim_mention(sm, _on(), _mention(tenant_id), NO_LINKS, NOW) is None
    event = await db_session.scalar(select(WorkflowTriggerEvent))
    assert event is not None and (event.status, event.reason) == ("skipped", "skipped"), event
    assert (await _counts(db_session))[1] == 1, "no second run"


@pytest.mark.parametrize("verdict", ["skipped", "started"])
async def test_a_mention_the_starts_loop_took_first_goes_on_as_chat_when_it_refuses(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    verdict: Literal["skipped", "started"],
) -> None:
    sm, tenant_id, _ = await _setup(db_session)

    async def loop_first(
        session: AsyncSession, *, limit: int, event_id: uuid.UUID | None = None
    ) -> list[object]:
        """The starts loop holds the event and settles it before our claim reads it."""
        assert event_id is not None, "the mention claims its own event"
        await mentions.store.mark_event(session, event_id, status=verdict, reason=verdict)
        return []

    monkeypatch.setattr(mentions.store, "claim_accepted_events", loop_first)
    claimed = await mentions.claim_mention(sm, _on(), _mention(tenant_id), NO_LINKS, NOW)
    if verdict == "skipped":
        assert claimed is None, "the loop's refusal leaves the message to chat"
    else:
        assert claimed == mentions.Claimed(), "the loop started the run, so nothing is posted"
