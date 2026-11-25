"""The Triggers tab's webhook commands: URLs, the secret, Listen, deliveries, preview and replay.

Editors see a trigger's URLs and deliveries and listen for a test delivery. Rotating the
secret and replaying a delivery also need the run right, since either lets the caller
start live runs. A secret is shown once, in the rotate answer, and stored encrypted under
the deployment's crypto keys; after that only its hint is shown.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Final, Literal, cast

from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.stores import workflow_triggers as store
from daimon.core.stores import workflows
from daimon.core.stores.workflow_triggers import EventRow, TriggerRow
from daimon.core.workflows import gate, runs
from daimon.core.workflows.access import Rights, rights_of
from daimon.core.workflows.expressions import ExpressionError, render
from daimon.core.workflows.ingress import delivery_ref
from daimon.core.workflows.preflight import Refusal
from daimon.core.workflows.service import (
    GONE,
    Env,
    Forbidden,
    Invalid,
    NotFound,
    Off,
    Refused,
    check_routine_run,
)
from daimon.core.workflows.signatures import hint_of, new_secret
from daimon.core.workflows.triggers import MappingError, map_inputs
from pydantic import BaseModel, Field, JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

LISTEN: Final = timedelta(minutes=10)
DELIVERIES: Final = 50
OVERLAPS: Final[dict[str, timedelta]] = {
    "none": timedelta(0),
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
}
NO_TRIGGER = "This webhook trigger is not in the workflow."
NO_DELIVERY = "This delivery is gone. Deliveries are kept for 7 days."


class WebhookInfo(BaseModel):
    live_url: str
    test_url: str
    secret_hint: str | None
    secret_created_at: datetime | None
    previous_expires_at: datetime | None
    listening_until: datetime | None


class Secret(BaseModel):
    secret: str
    created_at: datetime
    previous_expires_at: datetime | None


class RunRef(BaseModel):
    id: uuid.UUID
    number: int


class DeliveryRow(BaseModel):
    id: str
    received_at: datetime
    verified: bool
    outcome: Literal["accepted", "skipped", "rejected", "duplicate"]
    status: int
    run: RunRef | None
    headers: dict[str, Any]
    payload: Any


class Delivery(BaseModel):
    id: str
    received_at: datetime
    inputs: dict[str, Any]


class Rotate(BaseModel):
    overlap: str = "24h"
    #: A secret the sender made (Stripe and Slack make their own); else one is made here.
    secret: str | None = Field(default=None, min_length=16, max_length=512)


class Preview(BaseModel):
    inputs: JsonValue = None
    delivery: str


# ---------- loading ----------


def _named(spec: dict[str, Any], key: str) -> dict[str, Any] | None:
    """Trigger `key` of a spec document, which may not be valid."""
    found: object = spec.get("triggers")
    for item in cast("list[object]", found) if isinstance(found, list) else []:
        if isinstance(item, dict) and cast("dict[str, Any]", item).get("id") == key:
            return cast("dict[str, Any]", item)
    return None


async def _trigger(
    session: AsyncSession, env: Env, workflow_id: uuid.UUID, key: str, *, create: bool = False
) -> tuple[TriggerRow | None, Rights]:
    """The webhook trigger `key` of a workflow the caller may edit, made on first use when
    the draft names it."""
    workflow = await workflows.get_workflow(
        session, tenant_id=env.who.tenant_id, workflow_id=workflow_id
    )
    if workflow is None:
        raise NotFound(GONE)
    access = await workflows.list_access(
        session, tenant_id=workflow.tenant_id, workflow_id=workflow.id
    )
    rights = rights_of(workflow, access, env.who)
    if not rights.view:
        raise NotFound(GONE)
    if not rights.edit:
        raise Forbidden("Only the owner or an editor can see webhook settings. Ask the owner.")
    features = await gate.features_at(env.sm, env.settings, workflow.tenant_id)
    if features is None or not features.webhooks:
        raise Off("Webhooks are off in this workspace. Ask an admin to turn them on.")
    row = await store.find_trigger(
        session, tenant_id=workflow.tenant_id, workflow_id=workflow.id, key=key
    )
    if row is None and create:
        named = _named(workflow.draft_spec, key)
        if named and named.get("kind") == "webhook":
            row = await store.ensure_trigger(
                session,
                tenant_id=workflow.tenant_id,
                workflow_id=workflow.id,
                key=key,
                kind="webhook",
            )
    if row is not None and row.kind != "webhook":
        row = None
    if row is None and create:
        raise NotFound(NO_TRIGGER)
    return row, rights


def _need_run(rights: Rights) -> None:
    if not rights.run:
        raise Forbidden("You can edit this workflow but not run it. Ask the owner.")


async def _info(session: AsyncSession, env: Env, trigger: TriggerRow, base: str) -> WebhookInfo:
    secrets = await store.live_secrets(session, trigger_id=trigger.id, now=env.now)
    current = secrets[0] if secrets else None
    previous = [s.expires_at for s in secrets[1:] if s.expires_at]
    url = f"{base.rstrip('/')}/hooks/w/{trigger.id}"
    listening = trigger.listening_until
    return WebhookInfo(
        live_url=url,
        test_url=f"{url}/test",
        secret_hint=current.hint if current else None,
        secret_created_at=current.created_at if current else None,
        previous_expires_at=max(previous) if previous else None,
        listening_until=listening if listening and listening > env.now else None,
    )


# ---------- commands ----------


async def info(env: Env, workflow_id: uuid.UUID, key: str, *, base_url: str) -> WebhookInfo:
    async with env.sm.begin() as session:
        trigger, _ = await _trigger(session, env, workflow_id, key, create=True)
        assert trigger is not None
        return await _info(session, env, trigger, base_url)


async def listen(
    env: Env, workflow_id: uuid.UUID, key: str, *, on: bool, base_url: str
) -> WebhookInfo:
    """Open the test URL for ten minutes; its first delivery starts a test run of the draft
    as the caller, and closes it again."""
    async with env.sm.begin() as session:
        trigger, _ = await _trigger(session, env, workflow_id, key, create=True)
        assert trigger is not None
        until = env.now + LISTEN if on else None
        await store.set_listening(
            session, trigger, until=until, listener=env.who.account_id if on else None
        )
        found = await store.get_trigger(session, trigger.id)
        assert found is not None
        return await _info(session, env, found, base_url)


async def rotate(env: Env, workflow_id: uuid.UUID, key: str, body: Rotate) -> Secret:
    overlap = OVERLAPS.get(body.overlap)
    if overlap is None:
        raise Invalid("Choose an overlap of none, 1 hour, 24 hours or 7 days.")
    keys = tuple(k.get_secret_value() for k in env.settings.crypto.keys)
    if not keys:
        raise Refused("This server cannot store secrets yet. Ask the operator to add a key.")
    secret = body.secret.strip() if body.secret else new_secret()
    async with env.sm.begin() as session:
        trigger, rights = await _trigger(session, env, workflow_id, key, create=True)
        assert trigger is not None
        _need_run(rights)
        await store.add_secret(
            session,
            trigger=trigger,
            encrypted=encrypt_token(build_multifernet(keys), secret),
            hint=hint_of(secret),
            created_by=env.who.account_id,
            now=env.now,
            overlap=overlap,
        )
        older = await store.live_secrets(session, trigger_id=trigger.id, now=env.now)
    previous = [s.expires_at for s in older[1:] if s.expires_at]
    return Secret(
        secret=secret, created_at=env.now, previous_expires_at=max(previous) if previous else None
    )


def _outcome(status: str) -> Literal["accepted", "skipped", "rejected", "duplicate"]:
    match status:
        case "accepted" | "started":
            return "accepted"
        case "skipped" | "rejected" | "duplicate":
            return status
        case _:
            return "rejected"


async def _rows(session: AsyncSession, events: list[EventRow]) -> list[DeliveryRow]:
    numbers = await store.run_numbers(session, [e.run_id for e in events if e.run_id])
    return [
        DeliveryRow(
            id=delivery_ref(e.id, e.test),
            received_at=e.received_at,
            verified=e.verified,
            outcome=_outcome(e.status),
            status=e.http_status or 202,
            run=RunRef(id=e.run_id, number=numbers[e.run_id])
            if e.run_id and e.run_id in numbers
            else None,
            headers=e.headers or {},
            payload=e.payload,
        )
        for e in events
    ]


async def deliveries(env: Env, workflow_id: uuid.UUID, key: str) -> list[DeliveryRow]:
    async with env.sm() as session:
        trigger, _ = await _trigger(session, env, workflow_id, key)
        if trigger is None:
            return []
        events = await store.list_events(session, trigger_id=trigger.id, limit=DELIVERIES)
        return await _rows(session, events)


async def _event(session: AsyncSession, trigger: TriggerRow, ref: str) -> EventRow:
    raw = ref.removeprefix("dlv_").removeprefix("test_")
    try:
        event_id = uuid.UUID(hex=raw)
    except ValueError:
        raise NotFound(NO_DELIVERY) from None
    event = await store.get_event(session, trigger_id=trigger.id, event_id=event_id)
    if event is None:
        raise NotFound(NO_DELIVERY)
    return event


async def preview(env: Env, workflow_id: uuid.UUID, key: str, body: Preview) -> dict[str, Any]:
    """A mapping evaluated with the real expression engine against one stored delivery."""
    async with env.sm() as session:
        trigger, _ = await _trigger(session, env, workflow_id, key)
        if trigger is None:
            raise NotFound(NO_DELIVERY)
        event = await _event(session, trigger, body.delivery)
    if event.payload is None:
        return {"ok": False, "error": "This delivery kept no payload"}
    context: dict[str, JsonValue] = {"payload": event.payload, "headers": event.headers or {}}
    try:
        return {"ok": True, "value": render(body.inputs, context, now=env.now)}
    except ExpressionError as err:
        return {"ok": False, "error": str(err)}


async def replay(env: Env, workflow_id: uuid.UUID, key: str, ref: str) -> DeliveryRow:
    """Start a run from a stored delivery again, mapped by today's trigger: a test delivery
    tests the draft as the caller, a live one runs the live version."""
    async with env.sm.begin() as session:
        trigger, rights = await _trigger(session, env, workflow_id, key)
        if trigger is None:
            raise NotFound(NO_DELIVERY)
        _need_run(rights)
        event = await _event(session, trigger, ref)
        workflow = await workflows.get_workflow(
            session, tenant_id=trigger.tenant_id, workflow_id=trigger.workflow_id
        )
        assert workflow is not None
        run_as: uuid.UUID | None = env.who.account_id
        if event.test:
            spec: dict[str, Any] = workflow.draft_spec
        elif workflow.live_version is None or trigger.live is None:
            raise Refused("Publish this workflow before replaying live deliveries.")
        else:
            live = await workflows.get_version(
                session,
                tenant_id=workflow.tenant_id,
                workflow_id=workflow.id,
                number=workflow.live_version,
            )
            assert live is not None
            spec, run_as = live.spec, live.published_by_account_id or env.who.account_id
            await check_routine_run(session, workflow, rights, live.number)
    if event.payload is None or event.status in ("rejected", "duplicate"):
        raise Refused("Only accepted deliveries can be replayed.")
    config = _named(spec, key) or {}
    context: dict[str, JsonValue] = {"payload": event.payload, "headers": event.headers or {}}
    try:
        inputs = map_inputs(config.get("inputs"), context, now=env.now)
    except MappingError as err:
        raise Invalid(f"The input mapping failed: {err}") from err
    result = await runs.start_run(
        env.sm,
        env.settings,
        tenant_id=trigger.tenant_id,
        start=runs.Start(
            workflow_id=trigger.workflow_id,
            run_as_account_id=run_as,
            triggered_by={
                "kind": "webhook",
                "trigger_id": str(trigger.id),
                "replay_of": str(event.id),
                "account_id": str(env.who.account_id),
            },
            trigger_kind="webhook",
            trigger_key=key,
            inputs=inputs,
            mode="test" if event.test else "live",
            untrusted_inputs=True,
        ),
        now=env.now,
    )
    if isinstance(result, Refusal):
        raise Refused(result.message)
    async with env.sm.begin() as session:
        row, _ = await store.record_event(
            session,
            trigger=trigger,
            status="started",
            received_at=env.now,
            test=event.test,
            verified=event.verified,
            http_status=201,
            reason="replay",
            headers=event.headers,
            payload=event.payload,
            inputs=inputs,
        )
        await store.mark_event(session, row.id, status="started", run_id=result.id)
        (out,) = await _rows(session, [row.model_copy(update={"run_id": result.id})])
    return out


async def last_delivery(env: Env, workflow_id: uuid.UUID) -> Delivery:
    """The newest delivery's inputs, for a test run that uses them."""
    async with env.sm() as session:
        workflow = await workflows.get_workflow(
            session, tenant_id=env.who.tenant_id, workflow_id=workflow_id
        )
        if workflow is None:
            raise NotFound(GONE)
        access = await workflows.list_access(
            session, tenant_id=workflow.tenant_id, workflow_id=workflow.id
        )
        if not rights_of(workflow, access, env.who).edit:
            raise NotFound(GONE)
        event = await store.last_with_inputs(session, workflow_id=workflow.id)
    if event is None or event.inputs is None:
        raise NotFound("Nothing has arrived yet.")
    return Delivery(
        id=delivery_ref(event.id, event.test), received_at=event.received_at, inputs=event.inputs
    )
