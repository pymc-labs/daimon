"""The post step and where a run's posts may go: addresses, titled threads, one writer."""

from __future__ import annotations

import ast
import uuid
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from daimon.core._models import WorkflowDelivery
from daimon.core.delivery import DeliveryOutcome, JobPoster, NothingPosted, poll_once, sent_first
from daimon.core.stores import accounts, tenants
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.workflow_deliveries import DeliveryRow
from daimon.core.workflows import delivery, runs
from daimon.core.workflows.delivery import NOT_A_DESTINATION, Reach, WorkflowPost, address
from daimon.core.workflows.post_step import PostStepExecutor
from daimon.testing.factories import make_platform_principal
from daimon.testing.workflows import NOW, World, make_world
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

RUN_AS = uuid.uuid4()
LIVE: dict[str, Any] = {"home": "C0HOME", "mode": "live", "run_as": RUN_AS, "owner": None}
ORIGIN = {"platform": "slack", "channel": "C0ORIG", "message": "1.5"}
REACH = Reach(("C0LIST", "person:p1"), ORIGIN, frozenset({"C0OPEN:2.5"}))


@pytest.mark.parametrize(
    ("value", "templated", "expected"),
    [
        ("C0ELSE", False, {"channel_id": "C0ELSE"}),
        ("C0LIST", True, {"channel_id": "C0LIST"}),
        ("C0LIST:9.5", True, {"channel_id": "C0LIST", "thread_id": "9.5"}),
        ("C0OPEN:2.5", True, {"channel_id": "C0OPEN", "thread_id": "2.5"}),
        ("C0ORIG", True, {"channel_id": "C0ORIG"}),
        ("origin", False, {"channel_id": "C0ORIG", "thread_id": "1.5"}),
        ("C0ELSE", True, {"channel_id": "C0ELSE", "within": ["C0LIST"]}),
        ("person:p1", True, {"kind": "dm", "account_id": "p1"}),
        ("person:p2", True, {"kind": "dm", "refused": NOT_A_DESTINATION}),
        ("person:p2", False, {"kind": "dm", "account_id": "p2"}),
    ],
)
def test_addresses_land_in_reach_or_are_refused(
    value: str, templated: bool, expected: dict[str, Any]
) -> None:
    target = address(value, templated=templated, reach=REACH, **LIVE)
    assert target.items() >= expected.items(), target
    if "within" not in expected and "refused" not in expected:
        assert not {"within", "refused"} & target.keys(), target


def test_an_unlisted_address_with_no_channels_listed_is_refused_and_tests_dm_the_tester() -> None:
    bare = address("C0ELSE", templated=True, reach=Reach(("person:p1",)), **LIVE)
    assert bare["refused"] == NOT_A_DESTINATION, bare
    tested = address("C0ELSE", templated=False, reach=REACH, **LIVE | {"mode": "test"})
    assert tested == {"kind": "dm", "account_id": str(RUN_AS), "via": "C0HOME"}, tested
    home = address("origin", templated=False, reach=Reach(), title="Week", **LIVE)
    assert (home["channel_id"], home["title"]) == ("C0HOME", "Week"), "no origin: home"


def _post(to: str, *, nodes: dict[str, Any] | None = None) -> dict[str, Any]:
    return nodes or {
        "p": {"kind": "post", "to": to, "message": "Hi ${{ inputs.n }}", "next": "f"},
        "f": {"kind": "finish", "output": "${{ steps.p.output }}"},
    }


async def _world(db_session: AsyncSession, nodes: dict[str, Any], start: str) -> World:
    world = await make_world(db_session, nodes, start, platform="slack", home="C0HOME")
    tenant = await tenants.get_tenant(db_session, world.tenant_id)
    owner = await accounts.get_account(db_session, world.owner)
    assert tenant is not None and owner is not None
    await make_platform_principal(
        db_session, platform="slack", external_id="U1", tenant=tenant, account=owner
    )
    await db_session.commit()
    return world


async def _post_step(world: World, run_id: uuid.UUID, key: str) -> None:
    claimed = await world.claim(run_id, key)
    executors: dict[str, runs.StepExecutor] = {"post": PostStepExecutor(world.sm, lambda: NOW)}
    await runs.execute(world.sm, world.settings, executors, claimed, clock=lambda: NOW)


async def _deliver(world: World) -> list[DeliveryRow]:
    """Post every due post through the real rules; a cleared one lands in its channel."""
    seen: list[DeliveryRow] = []

    async def routine(row: RoutineRow) -> DeliveryOutcome:
        raise AssertionError("no routine is due")

    async def workflow(row: DeliveryRow) -> DeliveryOutcome:
        seen.append(row)
        cleared = await delivery.clear_post(world.sm, row, platform="slack", dm_policies={})
        if isinstance(cleared, str):
            return DeliveryOutcome(status="skipped", note=cleared)
        placed: dict[str, str | bool | None] = {"channel": cleared.channel_id, "thread": None}
        placed |= {"message": f"{len(seen)}.1", "link": None}
        return DeliveryOutcome(status="delivered", placed=placed)

    while await poll_once(world.sm, platform="slack", post=JobPoster(routine, workflow), now=NOW):
        pass
    await runs.sync_children(world.sm, world.settings, now=NOW)
    return seen


async def test_a_post_step_waits_for_its_post_and_outputs_where_it_landed(
    db_session: AsyncSession,
) -> None:
    world = await _world(db_session, _post("home"), "p")
    run = await world.run(inputs={"n": 3})
    await _post_step(world, run.id, "p")
    step = (await world.steps(run.id))["p"]
    assert (step.status, step.waiting_reason) == ("waiting", "event"), step
    [row] = await _deliver(world)
    assert (row.kind, row.step_id, row.payload["text"]) == ("post", step.id, "Hi 3"), row
    done = await world.get(run.id)
    assert done.status == "succeeded", done
    out = (await world.steps(run.id))["f"].output
    assert out == {"status": "sent", "channel": "C0HOME", "thread": None, "message": "1.1"} | {
        "link": None
    }, out
    types = await world.verified(run.id)
    assert "wait.opened" in types and "delivery.sent" in types, types


async def test_a_refused_post_fails_its_step(db_session: AsyncSession) -> None:
    world = await _world(db_session, _post("${{ inputs.where }}"), "p")
    run = await world.run(inputs={"n": 1, "where": "C0ELSE"})
    await _post_step(world, run.id, "p")
    [row] = await _deliver(world)
    assert row.target["refused"] == NOT_A_DESTINATION, row.target
    step = (await world.steps(run.id))["p"]
    assert step.status == "failed" and (await world.get(run.id)).status == "failed", step


async def test_a_post_in_for_each_posts_once_per_item(db_session: AsyncSession) -> None:
    nodes = {
        "each": {
            "kind": "for_each",
            "items": "${{ inputs.names }}",
            "as": "name",
            "do": {"kind": "post", "to": "home", "message": "Hi ${{ name }}"},
            "concurrency": 3,
            "next": "f",
        },
        "f": {"kind": "finish"},
    }
    world = await _world(db_session, nodes, "each")
    run = await world.run(inputs={"names": ["A", "B", "C"]})
    for index in range(3):
        await _post_step(world, run.id, f"each[{index}]")
    posted = await _deliver(world)
    assert sorted(r.payload["text"] for r in posted) == ["Hi A", "Hi B", "Hi C"], posted
    assert len({r.step_id for r in posted}) == 3, "each item has its own post"
    assert (await world.get(run.id)).status == "succeeded"


async def test_a_titled_thread_is_found_or_made_once(db_session: AsyncSession) -> None:
    deliver = {"to": "home", "thread": "Week ${{ inputs.n }}", "message": "x"}
    world = await _world(db_session, {"f": {"kind": "finish", "deliver": deliver}}, "f")
    for _ in range(2):
        await world.run(inputs={"n": 7})
    made: list[str] = []

    async def create(title: str) -> str:
        made.append(title)
        return f"C0HOME:{len(made)}.0"

    async def find(title: str) -> str | None:
        return None

    async with world.sm() as s:
        found = await s.scalars(select(WorkflowDelivery).order_by(WorkflowDelivery.created_at))
        rows = [DeliveryRow.model_validate(r) for r in found]
    posts = [
        WorkflowPost(
            row=row,
            policy=cast("Any", None),
            speaker="U1",
            channel_id="C0HOME",
            thread_id=None,
            gate=None,
            link=None,
            title=row.target["title"],
        )
        for row in rows
    ]
    assert [p.title for p in posts] == ["Week 7"] * 2, rows
    threads = [
        await delivery.titled_thread(world.sm, p, platform="slack", create=create, find=find)
        for p in posts
    ]
    assert (made, threads) == (["Week 7"], ["C0HOME:1.0"] * 2), "one thread per title"
    other = replace(posts[0], title="Other")
    assert await delivery.titled_thread(world.sm, other, platform="slack", create=create) == (
        "C0HOME:2.0"
    )


_CORE = Path(delivery.__file__).parents[1]
#: The only writers of a workflow post; every other post (Finish, post step, notice) goes
#: through `delivery.queue_post`. Asks write their own ask, reminder and update rows.
_WRITERS = {("delivery.py", "queue_post"), ("asks.py", "queue"), ("asks.py", "_queue_on_posts")}
_VIA_QUEUE_POST = {
    ("delivery.py", "queue_finish"),
    ("delivery.py", "queue_deliver"),
    ("delivery.py", "queue_failure_notice"),
    ("asks.py", "tell_owner_undelivered"),
    ("post_step.py", "__call__"),
}


def _calls(fn: ast.AST) -> set[str]:
    return {
        c.func.attr if isinstance(c.func, ast.Attribute) else getattr(c.func, "id", "")
        for c in ast.walk(fn)
        if isinstance(c, ast.Call)
    }


def test_every_workflow_post_goes_through_queue_post() -> None:
    writers: set[tuple[str, str]] = set()
    funcs: dict[tuple[str, str], ast.AST] = {}
    for path in _CORE.rglob("*.py"):
        for fn in ast.walk(ast.parse(path.read_text())):
            if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                funcs[(path.name, fn.name)] = fn
                if "queue_delivery" in _calls(fn) and fn.name != "queue_delivery":
                    writers.add((path.name, fn.name))
    assert writers == _WRITERS, "a workflow post written outside queue_post"
    for key in _VIA_QUEUE_POST:
        assert _calls(funcs[key]) & {"queue_post", "queue_deliver"}, key


async def test_a_post_none_of_which_went_retries_and_goes_on_the_next_attempt(
    db_session: AsyncSession,
) -> None:
    """A first send that never connected sent nothing, so the step's `retry` sends the same
    post again."""
    nodes = _post("home")
    nodes["p"]["retry"] = {"attempts": 2}
    world = await _world(db_session, nodes, "p")
    run = await world.run(inputs={"n": 3})
    await _post_step(world, run.id, "p")
    tries: list[uuid.UUID] = []

    async def routine(row: RoutineRow) -> DeliveryOutcome:
        raise AssertionError("no routine is due")

    async def flaky(row: DeliveryRow) -> DeliveryOutcome:
        async def send() -> str:
            raise httpx.ConnectError("refused")

        tries.append(row.id)
        if len(tries) == 1:
            await sent_first(send(), first=True)
        return DeliveryOutcome(status="delivered", placed={"channel": "C0HOME", "message": "1"})

    poster = JobPoster(routine, flaky)
    await poll_once(world.sm, platform="slack", post=poster, now=NOW)
    await runs.sync_children(world.sm, world.settings, now=NOW)
    step = (await world.steps(run.id))["p"]
    assert (step.status, step.error_message) == ("ready", "The post did not go."), step
    later = NOW + timedelta(minutes=1)
    claimed = await world.claim(run.id, "p", later)
    executors: dict[str, runs.StepExecutor] = {"post": PostStepExecutor(world.sm, lambda: later)}
    await runs.execute(world.sm, world.settings, executors, claimed, clock=lambda: later)
    await poll_once(world.sm, platform="slack", post=poster, now=later)
    await runs.sync_children(world.sm, world.settings, now=later)
    assert len(tries) == 2 and tries[0] == tries[1], f"the same post goes again: {tries}"
    assert (await world.get(run.id)).status == "succeeded", "the second attempt posts"
    await world.verified(run.id)


def _error(**attrs: object) -> Exception:
    """A platform client's error carrying `attrs` (`status`, `response`)."""
    err = Exception("send failed")
    err.__dict__.update(attrs)
    return err


@pytest.mark.parametrize(
    ("error", "unsent"),
    [
        (httpx.ConnectError("refused"), True),
        (_error(response=SimpleNamespace(status_code=429)), True),
        (_error(status=403), True),
        (_error(response={"ok": False}), True),
        (httpx.ReadTimeout("slow"), False),
        (TimeoutError(), False),
        (_error(response=SimpleNamespace(status_code=503)), False),
    ],
)
async def test_sent_first_when_the_error_shows_nothing_was_accepted_then_it_is_nothing_posted(
    error: Exception, unsent: bool
) -> None:
    async def send() -> None:
        raise error

    with pytest.raises(NothingPosted if unsent else type(error)):
        await sent_first(send(), first=True)
