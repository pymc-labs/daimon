"""Every routine shape becomes a one-step workflow and comes back as it was."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from daimon.core._models import (
    FeatureValue,
    LeadIntakeConfig,
    Routine,
    Tenant,
    Workflow,
    WorkflowDelivery,
)
from daimon.core.stores import routines
from daimon.core.stores.workflow_deliveries import DeliveryRow
from daimon.core.workflows import convert
from daimon.core.workflows import spec as specs
from daimon.core.workflows.access import Who
from daimon.core.workflows.delivery import result_job
from daimon.core.workflows.routine_translation import (
    Delivery,
    Move,
    Refusal,
    RoutineSnapshot,
    WorkflowSnapshot,
    ids,
    translate,
    untranslate,
)
from daimon.core.workflows.service import Env
from daimon.core.workflows.validate import validate_document
from daimon.testing.builders import new_routine
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.workflows import NOW, agents_with, make_settings
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SLOT = NOW + timedelta(hours=1)
OWNER = uuid.UUID(int=3)
BASE = RoutineSnapshot(
    id=uuid.UUID(int=1),
    tenant_id=uuid.UUID(int=2),
    created_by_user_id="u1",
    agent_id="agent_a",
    agent_name="daimon",
    name="Digest",
    cron_expr="0 9 * * *",
    timezone="Europe/Berlin",
    trigger_message="Post the digest",
    enabled=True,
    catch_up_policy="run-once",
    next_fire_at=SLOT,
    last_fired_at=NOW - timedelta(days=1),
    last_fired_slot=NOW - timedelta(days=1),
    last_finished_at=NOW - timedelta(days=1, minutes=-2),
    seeded_after=None,
    source_routine_id=None,
    handback_paused_at=None,
    last_error=None,
    last_result_tail="All quiet",
    destination_kind="channel",
    destination_id="c1",
    delivery_status=None,
    delivery_payload=None,
    delivery_note=None,
    delivery_hold=None,
    channel_id="c1",
    made_in_channel_id="c1",
    source_provenance_known=True,
    source_channel_id="c1",
    source_thread_id=None,
    created_at=NOW - timedelta(days=30),
    owner_account_id=OWNER,
    next_slot=SLOT,
)
NOTICE = {"title": "Digest failed", "body": "It timed out.", "text": "Digest failed: timed out"}
SHAPES: dict[str, tuple[dict[str, Any], str, tuple[str, ...]]] = {
    "channel": ({}, "exact", ()),
    "thread": ({"destination_kind": "thread", "destination_id": "t1"}, "exact", ()),
    "dm": ({"destination_kind": "dm", "destination_id": "u1"}, "exact", ()),
    "no destination": ({"destination_kind": None, "destination_id": None}, "exact", ()),
    "unnamed": ({"name": None}, "exact", ()),
    "disabled": ({"enabled": False}, "paused", ("disabled",)),
    "ownerless": ({"created_by_user_id": None, "owner_account_id": None}, "paused", ("no owner",)),
    "intake": (
        {"intake": {"routine_id": str(BASE.id), "mailbox": "m"}},
        "exact",
        ("intake binding",),
    ),
    "sandbox pin": ({"features": ("routine_sandbox_tools",)}, "exact", ("sandbox pin",)),
    "pending post": (
        {"delivery_status": "pending", "delivery_payload": "Hi"},
        "exact",
        ("1 pending post",),
    ),
    "owner notice": (
        {"owner_notice": NOTICE, "last_error": "timed out"},
        "exact",
        ("owner notice",),
    ),
    "source-linked": ({"source_routine_id": uuid.UUID(int=9)}, "exact", ()),
    # The fire core refuses another backend per fire, so the move does not look at it.
    "other backend": ({}, "exact", ()),
    "never fired": (
        {"next_fire_at": None, "last_fired_at": None, "last_finished_at": None},
        "exact",
        (),
    ),
}


def _move(**fields: Any) -> Move:
    move = translate(replace(BASE, **fields), now=NOW)
    assert isinstance(move, Move), move
    return move


def _values(move: Move, table: str) -> dict[str, Any]:
    return dict(next(w.values for w in move.writes if w.table == table and w.op == "insert"))


@pytest.mark.parametrize("shape", SHAPES.values(), ids=list(SHAPES))
def test_each_routine_shape_translates(shape: tuple[dict[str, Any], str, tuple[str, ...]]) -> None:
    fields, outcome, notes = shape
    move = _move(**fields)
    assert (move.outcome, move.notes) == (outcome, notes)
    assert specs.dump_spec(specs.parse_spec(move.spec)) == move.spec, "canonical as published"
    assert move.spec_hash == specs.spec_hash(specs.parse_spec(move.spec))
    assert not validate_document(move.spec, None).errors
    flow = _values(move, "workflows")
    assert flow["status"] == ("active" if outcome == "exact" else "paused")
    assert flow["converted_from"]["routine_id"] == str(BASE.id)
    assert move.writes[-1].op == "delete" and move.writes[-1].table == "routines"


def test_dm_text_stays_private_and_source_links_are_kept() -> None:
    dm = _move(destination_kind="dm", destination_id="u1")
    assert dm.spec["nodes"]["run"]["to"] == {"kind": "dm"}
    assert _values(dm, "workflows")["converted_from"]["last"]["text"] == ""
    linked = _move(source_routine_id=uuid.UUID(int=9))
    assert _values(linked, "workflows")["converted_from"]["source_routine_id"] == str(
        uuid.UUID(int=9)
    )


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"handback_paused_at": NOW}, "handback_paused"),
        ({"next_slot": None}, "invalid_schedule"),
        ({"trigger_message": "  "}, "invalid_prompt"),
        ({"trigger_message": "x" * 4001}, "invalid_prompt"),
        ({"owner_account_id": None}, "owner_unlinked"),
        ({"delivery_status": "claimed"}, "busy"),
        ({"last_finished_at": None}, "busy"),
    ],
    ids=["handback", "schedule", "blank", "long", "unlinked", "posting", "running"],
)
def test_routines_that_cannot_translate_are_refused(fields: dict[str, Any], reason: str) -> None:
    assert translate(replace(BASE, **fields), now=NOW) == Refusal(reason)  # pyright: ignore[reportArgumentType]


def test_a_translation_is_deterministic() -> None:
    move = _move()
    assert move == _move(), "the same routine and time make the same move"
    assert (
        move.id
        == ids(BASE.id)["workflow"]
        == uuid.uuid5(uuid.UUID("5d2f7c1e-6a0b-4f3e-9c8d-7b1a2e4f6c90"), f"{BASE.id}:workflow")
    )
    assert move.spec_hash == "cfb2b32b6ceca658800bfcb3f2da82d2868099db1458d327e2b021ebdd1fd777"


def _back(move: Move, **over: Any) -> WorkflowSnapshot:
    flow, version = _values(move, "workflows"), _values(move, "workflow_versions")
    trigger = _values(move, "workflow_triggers")
    posts = tuple(
        Delivery(w.values["id"], w.values["kind"], "pending", w.values["payload"], w.values["note"])
        for w in move.writes
        if w.table == "workflow_deliveries"
    )
    intake = next(
        (w.values["config"] for w in move.writes if w.table == "lead_intake_configs"), None
    )
    features = next(
        (("routine_sandbox_tools",) for w in move.writes if w.table == "feature_values"), ()
    )
    snap = WorkflowSnapshot(
        id=move.id, tenant_id=flow["tenant_id"], name=flow["name"], status=flow["status"],
        owner_account_id=flow["owner_account_id"], home_channel_id=flow["home_channel_id"],
        made_in=flow["made_in"], converted_from=flow["converted_from"], spec=version["spec"],
        published_by=version["published_by_account_id"], next_fire_at=trigger["next_fire_at"],
        last_fired_slot=trigger["last_fired_slot"], seeded_after=trigger["seeded_after"],
        deliveries=posts, features=features, intake=intake,
    )  # fmt: skip
    return replace(snap, **over)


@pytest.mark.parametrize("shape", SHAPES.values(), ids=list(SHAPES))
def test_untranslate_gives_back_the_routine(
    shape: tuple[dict[str, Any], str, tuple[str, ...]],
) -> None:
    fields, _, _ = shape
    before = replace(BASE, **fields)
    move = _move(**fields)
    back = untranslate(_back(move), now=NOW)
    assert isinstance(back, Move) and (back.id, back.outcome) == (BASE.id, move.outcome)
    row = _values(back, "routines")
    moved = {"next_fire_at", "seeded_after", "enabled"}  # an ownerless routine comes back off
    kept = {k: v for k, v in vars(before).items() if k in row and k not in moved}
    assert {k: row[k] for k in kept} == kept
    active = move.outcome == "exact"
    assert row["enabled"] is active
    assert row["next_fire_at"] == ((before.next_fire_at or SLOT) if active else None)
    assert row["seeded_after"] == (before.seeded_after if before.next_fire_at else NOW)
    assert row.get("owner_notice") == before.owner_notice


def test_untranslate_refuses_what_a_routine_cannot_be() -> None:
    move = _move()
    spec = move.spec
    two = {**spec, "nodes": {**spec["nodes"], "x": {"title": "X", "kind": "finish"}}}
    changed = {**spec, "nodes": {**spec["nodes"], "run": {**spec["nodes"]["run"], "agent": "b"}}}
    claimed = (Delivery(uuid.uuid4(), "result", "claimed", {}, None),)
    cases = {
        "not_a_routine": _back(move, spec=two),
        "changed": _back(move, spec=changed),
        "busy": _back(move, active_runs=True),
    }
    assert untranslate(_back(move, converted_from=None), now=NOW) == Refusal("not_a_routine")
    assert untranslate(_back(move, deliveries=claimed), now=NOW) == Refusal("busy")
    for reason, snap in cases.items():
        assert untranslate(snap, now=NOW) == Refusal(reason)  # pyright: ignore[reportArgumentType]


async def _env(db: AsyncSession) -> Env:
    tenant = await make_tenant(db)
    account = await make_account(db, tenant=tenant)
    await db.execute(
        update(Tenant).where(Tenant.id == tenant.id).values(funding_mode="operator_funded")
    )
    sm = async_sessionmaker(bind=db.bind, expire_on_commit=False)
    return Env(sm, make_settings(), Who(tenant.id, account.id, admin=True), NOW, agents_with({}))


async def test_a_routine_moves_with_its_state_and_comes_back_through_the_database(
    db_session: AsyncSession,
) -> None:
    env = await _env(db_session)
    tid = env.who.tenant_id
    row = await new_routine(
        db_session, tid, created_by_user_id="owner-1", cron_expr="0 * * * *", channel_id="c1",
        destination_kind="channel", destination_id="c1", next_fire_at=SLOT,
    )  # fmt: skip
    await db_session.execute(
        update(Routine)
        .where(Routine.id == row.id)
        .values(owner_notice=NOTICE, delivery_status="pending", delivery_payload="Hi")
    )
    db_session.add_all(
        [
            FeatureValue(
                tenant_id=tid, tier="routine", routine_id=row.id, channel_id=str(row.id),
                key="routine_result_header", value="off", surface="cli",
            ),
            LeadIntakeConfig(
                tenant_id=tid, config={"routine_id": str(row.id)}, encrypted_credentials=b"x"
            ),
        ]
    )  # fmt: skip
    await db_session.commit()
    async with env.sm() as s:
        before = await routines.get_routine(s, row.id, tenant_id=tid)
    flow = await convert.from_routine(env, row.id)

    async with env.sm() as s:
        assert await routines.get_routine(s, row.id, tenant_id=tid) is None, "the row goes"
        draft = await s.scalar(
            text("SELECT draft_spec::text FROM workflows WHERE id = :i"), {"i": flow}
        )
        posts = {d.kind: d for d in await s.scalars(select(WorkflowDelivery))}
        feature = await s.scalar(select(FeatureValue))
        config = await s.scalar(select(LeadIntakeConfig.config))
        status = await s.scalar(select(Workflow.status).where(Workflow.id == flow))
    assert draft is not None and draft.startswith('{"schema"'), "the spec keeps its order"
    assert status == "active"
    assert (
        posts["result"].payload["text"] == "Hi"
        and posts["notice"].payload["routine_notice"] == NOTICE
    )
    assert feature is not None and (feature.tier, feature.workflow_id) == ("workflow", flow)
    assert config == {"routine_id": None, "workflow_id": str(flow)}
    job = await result_job(env.sm, DeliveryRow.model_validate(posts["result"]), platform="discord")
    assert job is not None and (job.delivery_payload, job.id) == ("Hi", flow), "it still posts"

    assert await convert.to_routine(env, flow) == row.id
    async with env.sm() as s:
        back = await routines.get_routine(s, row.id, tenant_id=tid)
        assert await s.scalar(select(Workflow.id)) is None, "the workflow goes"
        feature = await s.scalar(select(FeatureValue))
        config = await s.scalar(select(LeadIntakeConfig.config))
    assert before is not None and back is not None
    skip = {"updated_at", "owner_notice"}
    assert back.model_dump(exclude=skip) == before.model_dump(exclude=skip)
    assert feature is not None and (feature.tier, feature.routine_id) == ("routine", row.id)
    assert config == {"routine_id": str(row.id), "workflow_id": None}
