"""Every routine becomes an agent-step workflow of its shape (A-D) and comes back as it was."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from daimon.core import routine_files
from daimon.core._models import (
    FeatureValue,
    LeadIntakeConfig,
    Routine,
    Tenant,
    Workflow,
    WorkflowDelivery,
    WorkflowGrant,
)
from daimon.core.delivery import job_payload
from daimon.core.stores import routines
from daimon.core.stores.domain import FeatureValueRow
from daimon.core.stores.workflow_deliveries import DeliveryRow
from daimon.core.workflows import convert, engine
from daimon.core.workflows import spec as specs
from daimon.core.workflows.access import Who
from daimon.core.workflows.delivery import dm_target, target_for
from daimon.core.workflows.routine_translation import (
    FILES_PREFIX,
    OUTBOX_FILES,
    Delivery,
    Move,
    Refusal,
    RoutineSnapshot,
    WorkflowSnapshot,
    ids,
    translate,
    untranslate,
)
from daimon.core.workflows.service import Env, Refused
from daimon.core.workflows.validate import validate_document
from daimon.testing.builders import new_routine
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.workflows import NOW, World, agents_with, make_settings
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SLOT = NOW + timedelta(hours=1)
STUCK = timedelta(minutes=50)
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
FILE = {"file_id": "f1", "filename": "a.csv", "mime_type": "text/csv", "size_bytes": 3}
#: As the routine outbox writes it.
ENVELOPE = (
    routine_files.PREFIX
    + routine_files.RoutinePayload(
        text="Hi", files=[routine_files.RoutineFile.model_validate(FILE)]
    ).model_dump_json()
)
SHAPES: dict[str, tuple[dict[str, Any], str, tuple[str, ...]]] = {
    "channel": ({}, "exact", ()),
    "thread": ({"destination_kind": "thread", "destination_id": "t1"}, "exact", ()),
    "slack thread": ({"destination_kind": "thread", "destination_id": "C0AB:1.2"}, "exact", ()),
    "another channel": ({"destination_id": "c2"}, "exact", ()),
    "dm": ({"destination_kind": "dm", "destination_id": "u1"}, "exact", ()),
    "no destination": ({"destination_kind": None, "destination_id": None}, "exact", ()),
    "personal": (
        {"destination_kind": None, "destination_id": None, "channel_id": None},
        "exact",
        (),
    ),
    "unnamed": ({"name": None}, "exact", ()),
    "named agent": ({"agent_name": "Deal Desk"}, "exact", ()),
    "disabled": ({"enabled": False}, "paused", ("disabled",)),
    "ownerless": ({"created_by_user_id": None, "owner_account_id": None}, "paused", ("no owner",)),
    "intake": (
        {"intake": {"routine_id": str(BASE.id), "mailbox": "m"}},
        "exact",
        ("intake binding",),
    ),
    "header": ({"header": True}, "exact", ("header dropped",)),
    "pending post": (
        {"delivery_status": "pending", "delivery_payload": "Hi"},
        "exact",
        ("1 pending post",),
    ),
    "pending files": (
        {"delivery_status": "pending", "delivery_payload": ENVELOPE, "delivery_note": OUTBOX_FILES},
        "exact",
        ("1 pending post",),
    ),
    "pending dm": (
        {
            "destination_kind": "dm",
            "destination_id": "u1",
            "delivery_status": "pending",
            "delivery_payload": "Hi",
            "delivery_hold": {"held": True},
        },
        "exact",
        ("1 pending post",),
    ),
    "owner notice": (
        {"owner_notice": NOTICE, "last_error": "timed out"},
        "exact",
        ("owner notice",),
    ),
    "long message": ({"trigger_message": "x" * 9000, "name": None}, "exact", ()),
    "empty message": ({"trigger_message": "", "name": None}, "exact", ()),
    "blank message": ({"trigger_message": "  "}, "exact", ()),
    "expression message": ({"trigger_message": "Sum ${{ deals }}"}, "exact", ()),
    "source-linked": ({"source_routine_id": uuid.UUID(int=9)}, "exact", ()),
    "never fired": (
        {"next_fire_at": None, "last_fired_at": None, "last_finished_at": None},
        "exact",
        (),
    ),
}


def _move(**fields: Any) -> Move:
    move = translate(replace(BASE, **fields), now=NOW, stuck_after=STUCK)
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
    validation = validate_document(move.spec, None)
    assert not validation.errors and not validation.grants, "nothing for an admin to approve"
    flow = _values(move, "workflows")
    assert flow["status"] == ("active" if outcome == "exact" else "paused")
    assert flow["converted_from"]["routine_id"] == str(BASE.id)
    assert {w.table for w in move.writes}.isdisjoint({"feature_values"}), "no feature moves"
    assert move.writes[-1].op == "delete" and move.writes[-1].table == "routines"


#: Per shape: the run step's `at` and `memory`, and where the Post goes (None: no Post).
WHERE = {
    "channel": ("home", None, "home"),
    "thread": ("t1", None, "t1"),
    "another channel": ("c2", None, "c2"),
    "dm": (None, "personal", "run_as"),
    "no destination": ("home", None, None),
    "personal": (None, "personal", None),
}


@pytest.mark.parametrize("name", list(WHERE))
def test_a_routine_works_where_it_posts_and_posts_unless_it_did(name: str) -> None:
    at, memory, to = WHERE[name]
    nodes = _move(**SHAPES[name][0]).spec["nodes"]
    run = nodes["run"]
    assert {k: run[k] for k in ("kind", "agent", "instructions", "writes", "can_finish_early")} == {
        "kind": "agent",
        "agent": "daimon",
        "instructions": "Post the digest",
        "writes": "policy",
        "can_finish_early": True,
    }
    assert (run.get("at"), run.get("memory")) == (at, memory)
    if to is None:
        assert list(nodes) == ["run", "done"] and run["next"] == "done"
        return
    assert list(nodes) == ["run", "seen", "post", "done"] and run["next"] == "seen"
    assert nodes["seen"]["cases"] == [
        {"when": f"${{{{ '{to}' in steps.run.posted }}}}", "to": "done"}
    ]
    assert nodes["seen"]["else"] == "post"
    post = nodes["post"]
    assert (post["to"], post["as_agent"], post["next"]) == (to, "daimon", "done")
    assert (post["message"], post["files"]) == (
        "${{ steps.run.summary }}",
        "${{ steps.run.files }}",
    )


def test_the_agent_key_and_the_dm_text_and_source_links_are_kept() -> None:
    """The page hides a DM's text from everyone else (`test_a_moved_personal_routines_...`)."""
    named = _move(agent_name="Deal Desk").spec["nodes"]
    assert named["run"]["agent"] == named["post"]["as_agent"] == specs.slug("Deal Desk")
    dm = _move(destination_kind="dm", destination_id="u1")
    assert _values(dm, "workflows")["converted_from"]["last"]["text"] == "All quiet"
    linked = _move(source_routine_id=uuid.UUID(int=9))
    assert _values(linked, "workflows")["converted_from"]["source_routine_id"] == str(
        uuid.UUID(int=9)
    )


@pytest.mark.parametrize(
    ("fields", "target"),
    [
        ({}, target_for("c1", home="c1", mode="live", run_as=None, owner=None)),
        (
            {"destination_kind": "thread", "destination_id": "C0AB:1.2"},
            target_for("C0AB:1.2", home="c1", mode="live", run_as=None, owner=None),
        ),
        (
            {"destination_kind": "thread", "destination_id": "19:a;messageid=9"},
            target_for("19:a;messageid=9", home="c1", mode="live", run_as=None, owner=None),
        ),
        (
            {"destination_kind": "dm", "destination_id": "u1"},
            target_for("run_as", home="c1", mode="live", run_as=OWNER, owner=None),
        ),
    ],
    ids=["channel", "slack thread", "teams thread", "dm"],
)
def test_a_pending_result_moves_as_a_post_where_the_post_step_would_send_it(
    fields: dict[str, Any], target: dict[str, Any]
) -> None:
    move = _move(
        **fields, delivery_status="pending", delivery_payload=ENVELOPE, delivery_note=OUTBOX_FILES
    )
    post = _values(move, "workflow_deliveries")
    assert (post["kind"], post["target"], post["id"]) == ("post", target, ids(BASE.id)["post"])
    unsent = dict.fromkeys(("run_id", "step_id", "wait_id", "lease_owner", "lease_expires_at"))
    row = DeliveryRow.model_validate(post | unsent | {"status": "pending", "sent_at": None})
    sent = job_payload(row)
    assert (sent.text, [f.file_id for f in sent.files]) == ("Hi", ["f1"]), "text and file refs"
    assert post["payload"]["speaker"] == str(OWNER)


def test_the_vendored_routine_constants_match() -> None:
    assert (FILES_PREFIX, OUTBOX_FILES) == (routine_files.PREFIX, routine_files.OUTBOX_FILES)


def test_sandbox_becomes_grant() -> None:
    """An opt-in that decides the routine becomes `builtins: all` and a grant its setter
    approved, pinned to the node; it cannot come back, so the move back waits for it to go."""
    setter = uuid.UUID(int=7)
    move = _move(sandbox=True, sandbox_set_by=setter)
    assert move.notes == ("built-ins grant",)
    run = move.spec["nodes"]["run"]
    assert run["builtins"] == "all"
    validation = validate_document(move.spec, None)
    assert [(g.kind, g.node) for g in validation.grants] == [("builtins", "run")]
    grant = _values(move, "workflow_grants")
    node = specs.parse_spec(move.spec).nodes["run"]
    assert {k: grant[k] for k in ("kind", "node_id", "version", "approved_by_account_id")} == {
        "kind": "builtins",
        "node_id": "run",
        "version": 1,
        "approved_by_account_id": setter,
    }
    assert grant["pins"] == {"published_by": str(OWNER), "node_hash": specs.node_hash(node)}
    back = _back(move)
    assert untranslate(back, now=NOW) == Refusal("changed"), "a sandbox cannot move back"
    off = {k: v for k, v in run.items() if k != "builtins"}
    plain = replace(back, spec={**back.spec, "nodes": {**back.spec["nodes"], "run": off}})
    assert isinstance(untranslate(plain, now=NOW), Move), "with built-ins off it moves back"


def test_header_dropped() -> None:
    """The result's own header has no workflow form: Post's attribution line replaces it."""
    move = _move(header=True)
    assert move.notes == ("header dropped",)
    assert move.spec == _move().spec and len(move.writes) == len(_move().writes)


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"handback_paused_at": NOW}, "handback_paused"),
        ({"next_slot": None}, "invalid_schedule"),
        ({"owner_account_id": None}, "owner_unlinked"),
        ({"delivery_status": "claimed"}, "busy"),
        ({"last_fired_at": NOW - timedelta(minutes=49), "last_finished_at": None}, "busy"),
    ],
    ids=["handback", "schedule", "unlinked", "posting", "running"],
)
def test_routines_that_cannot_translate_are_refused(fields: dict[str, Any], reason: str) -> None:
    assert translate(replace(BASE, **fields), now=NOW, stuck_after=STUCK) == Refusal(reason)  # pyright: ignore[reportArgumentType]


def test_a_routine_stuck_past_the_dispatch_timeout_counts_as_finished() -> None:
    stuck = replace(BASE, last_fired_at=NOW - timedelta(minutes=50), last_finished_at=None)
    assert isinstance(translate(stuck, now=NOW, stuck_after=STUCK), Move), "a crashed fire is done"
    longer = translate(stuck, now=NOW, stuck_after=2 * STUCK)
    assert longer == Refusal("busy"), "a longer dispatch timeout keeps the fire running"


def test_a_translation_is_deterministic() -> None:
    move = _move()
    assert move == _move(), "the same routine and time make the same move"
    assert (
        move.id
        == ids(BASE.id)["workflow"]
        == uuid.uuid5(uuid.UUID("5d2f7c1e-6a0b-4f3e-9c8d-7b1a2e4f6c90"), f"{BASE.id}:workflow")
    )
    assert move.spec_hash == "e4c26b026d8ed4a18bd3dd9e62bc46f16e49bff1430341bedc15f063b7762a53"


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
    snap = WorkflowSnapshot(
        id=move.id, tenant_id=flow["tenant_id"], name=flow["name"], status=flow["status"],
        owner_account_id=flow["owner_account_id"], home_channel_id=flow["home_channel_id"],
        made_in=flow["made_in"], converted_from=flow["converted_from"], spec=version["spec"],
        published_by=version["published_by_account_id"], next_fire_at=trigger["next_fire_at"],
        last_fired_slot=trigger["last_fired_slot"], seeded_after=trigger["seeded_after"],
        deliveries=posts, intake=intake,
    )  # fmt: skip
    return replace(snap, **over)


@pytest.mark.parametrize("shape", SHAPES.values(), ids=list(SHAPES))
def test_untranslate_gives_back_the_routine(
    shape: tuple[dict[str, Any], str, tuple[str, ...]],
) -> None:
    """The property: `untranslate(translate(r)) == r` for every shape."""
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


def _edit(move: Move, node: str, **fields: Any) -> dict[str, Any]:
    nodes = move.spec["nodes"]
    return {**move.spec, "nodes": {**nodes, node: {**nodes[node], **fields}}}


def test_untranslate_refuses_what_a_routine_cannot_be() -> None:
    move = _move()
    spec = move.spec
    extra = {**spec, "nodes": {**spec["nodes"], "x": {"title": "X", "kind": "finish"}}}
    weekly = {**spec, "triggers": [spec["triggers"][0] | {"every_weeks": 2}]}
    retitled = _edit(move, "run", title="Renamed")
    claimed = (Delivery(uuid.uuid4(), "post", "claimed", {}, None),)
    cases = {
        "not_a_routine": [_back(move, spec=weekly), _back(move, converted_from=None)],
        "changed": [
            _back(move, spec=extra),
            _back(move, spec=_edit(move, "run", agent="b")),
            _back(move, spec=_edit(move, "run", at="c9")),
            _back(move, spec=_edit(move, "post", to="c9")),
            _back(move, spec=_edit(move, "seen", **{"else": "done"})),
            _back(move, spec=_edit(move, "run", writes="allow")),
        ],
        "busy": [_back(move, active_runs=True), _back(move, deliveries=claimed)],
    }
    for reason, snaps in cases.items():
        for snap in snaps:
            assert untranslate(snap, now=NOW) == Refusal(reason)  # pyright: ignore[reportArgumentType]
    assert isinstance(untranslate(_back(move, spec=retitled), now=NOW), Move), "titles aside"


@pytest.mark.parametrize(
    ("ran", "error"),
    [({}, "boom"), ({"last_status": "succeeded"}, None), ({"last_status": "failed"}, "late")],
    ids=["no_run", "succeeded", "failed"],
)
def test_moving_back_takes_the_failure_streak_from_the_workflows_runs(
    ran: dict[str, Any], error: str | None
) -> None:
    move = translate(replace(BASE, last_error="boom"), now=NOW, stuck_after=STUCK)
    assert isinstance(move, Move)
    back = untranslate(_back(move, last_error="late", **ran), now=NOW)
    assert isinstance(back, Move)
    assert _values(back, "routines")["last_error"] == error, "the last run's error, or none"


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
    plan = await convert.preview(env, row.id)
    assert plan.move.notes == ("intake binding", "header dropped", "1 pending post", "owner notice")
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
    assert job_payload(DeliveryRow.model_validate(posts["post"])).text == "Hi", "it still posts"
    assert posts["notice"].payload["routine_notice"] == NOTICE
    assert feature is None, "the header goes with the routine"
    assert config == {"routine_id": None, "workflow_id": str(flow)}

    assert await convert.to_routine(env, flow) == row.id
    async with env.sm() as s:
        back = await routines.get_routine(s, row.id, tenant_id=tid)
        assert await s.scalar(select(Workflow.id)) is None, "the workflow goes"
        config = await s.scalar(select(LeadIntakeConfig.config))
    assert before is not None and back is not None
    skip = {"updated_at", "owner_notice"}
    assert back.model_dump(exclude=skip) == before.model_dump(exclude=skip)
    assert config == {"routine_id": str(row.id), "workflow_id": None}


async def test_a_sandboxed_routine_moves_with_its_grant_and_stays_until_it_is_off(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = await _env(db_session)
    tid = env.who.tenant_id
    row = await new_routine(
        db_session, tid, created_by_user_id="owner-1", cron_expr="0 * * * *", channel_id="c1",
        next_fire_at=SLOT,
    )  # fmt: skip
    await db_session.commit()
    opt_in = FeatureValueRow(
        tenant_id=tid, tier="routine", agent_id="", channel_id=str(row.id), thread_key="",
        routine_id=row.id, key="routine_sandbox_tools", value="on", locked=False, pin={},
        set_by=env.who.account_id, surface="cli", set_at=NOW, revision=1,
    )  # fmt: skip
    seen: list[uuid.UUID] = []

    async def pinned(session: AsyncSession, routine: Any, platform: str) -> FeatureValueRow:
        seen.append(routine.id)
        return opt_in

    monkeypatch.setattr(convert, "pinned_opt_in", pinned)
    flow = await convert.from_routine(env, row.id)
    async with env.sm() as s:
        grant = await s.scalar(select(WorkflowGrant).where(WorkflowGrant.workflow_id == flow))
        spec = await s.scalar(
            text("SELECT spec FROM workflow_versions WHERE workflow_id = :i"), {"i": flow}
        )
    assert seen == [row.id] and grant is not None
    assert (grant.kind, grant.node_id, grant.approved_by_account_id) == (
        "builtins",
        "run",
        env.who.account_id,
    )
    assert grant.pins["node_hash"] == specs.node_hash(specs.parse_spec(spec).nodes["run"])
    with pytest.raises(Refused, match="built-ins off"):
        await convert.to_routine(env, flow)


@pytest.mark.parametrize("channel", ["c1", None])
async def test_failure_notice_one_place(db_session: AsyncSession, channel: str | None) -> None:
    """A routine told its channel and its owner; a moved one tells home, else the owner."""
    env = await _env(db_session)
    tid, owner = env.who.tenant_id, env.who.account_id
    row = await new_routine(
        db_session, tid, created_by_user_id="owner-1", cron_expr="0 * * * *",
        channel_id=channel, next_fire_at=SLOT,
    )  # fmt: skip
    await db_session.commit()
    world = World(env.sm, env.settings, tid, owner, await convert.from_routine(env, row.id))
    for _ in range(2):
        run = await world.run()
        await world.execute(await world.claim(run.id, "run"), engine.Failed("fatal", "boom"))
        assert (await world.get(run.id)).status == "failed"
    async with env.sm() as s:
        mover = await s.scalar(select(Workflow.owner_account_id))
        notices = list(
            await s.scalars(select(WorkflowDelivery).where(WorkflowDelivery.kind == "notice"))
        )
    assert [n.target for n in notices] == [
        target_for("home", home="c1", mode="live", run_as=mover, owner=mover)
        if channel
        else dm_target(mover, home=None)
    ], "one notice per streak, in one place"
