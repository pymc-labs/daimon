"""Webhook ingress against the database: verify, map, dedupe, limit and refuse."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.fernet import Fernet
from daimon.core._models import Tenant, WorkflowRun, WorkflowTriggerEvent
from daimon.core.config import AnthropicSettings, DatabaseSettings, DiscordSettings, Settings
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.stores import workflow_triggers as store
from daimon.core.stores import workflows
from daimon.core.workflows import ingress, triggers
from daimon.core.workflows.signatures import hint_of, new_secret
from daimon.testing.factories import make_account, make_tenant
from pydantic import PostgresDsn, SecretStr
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_runs import INLINE

NOW = datetime(2026, 11, 20, 12, tzinfo=UTC)
KEY = Fernet.generate_key().decode()
HOOK = {
    "id": "form",
    "kind": "webhook",
    "verify": "standard-webhooks",
    "inputs": {"n": "${{ payload.data.n }}"},
    "start_when": "payload.event == 'form.submitted'",
}


def settings(webhooks: str = "on", **limits: Any) -> Settings:
    return Settings.model_validate(
        {
            "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
            "anthropic": AnthropicSettings(api_key=SecretStr("test")),
            "discord": DiscordSettings(bot_token=SecretStr("test")),
            "features": {"workflows": "on", "workflow_webhooks": webhooks},
            "crypto": {"keys": [KEY]},
            "workflows": limits,
        }
    )


@dataclass
class Hook:
    sm: async_sessionmaker[AsyncSession]
    trigger_id: uuid.UUID
    workflow_id: uuid.UUID
    owner: uuid.UUID
    secret: str

    def signed(self, payload: dict[str, Any], msg_id: str, at: datetime = NOW) -> dict[str, Any]:
        body = json.dumps(payload).encode()
        stamp = str(int(at.timestamp()))
        key = base64.b64decode(self.secret.removeprefix("whsec_"))
        mac = hmac.new(key, f"{msg_id}.{stamp}.".encode() + body, hashlib.sha256).digest()
        headers = {
            "content-type": "application/json",
            "webhook-id": msg_id,
            "webhook-timestamp": stamp,
            "webhook-signature": f"v1,{base64.b64encode(mac).decode()}",
            "authorization": "Bearer never-stored",
        }
        return {"headers": headers, "body": body}

    async def post(self, cfg: Settings | None = None, *, test: bool = False, **req: Any) -> Any:
        return await ingress.receive(
            self.sm, cfg or settings(), trigger_id=str(self.trigger_id), test=test, now=NOW, **req
        )


async def make_hook(db: AsyncSession) -> Hook:
    tenant = await make_tenant(db)
    owner = await make_account(db, tenant=tenant)
    await db.execute(
        update(Tenant).where(Tenant.id == tenant.id).values(funding_mode="operator_funded")
    )
    spec = {
        "schema": "daimon.workflow/v1",
        "name": "T",
        "start": "b",
        "nodes": INLINE,
        "inputs": {"type": "object", "required": ["n"], "properties": {"n": {"type": "integer"}}},
        "triggers": [HOOK],
    }
    flow = await workflows.create_workflow(
        db, tenant_id=tenant.id, owner_account_id=owner.id, name="T", spec=spec
    )
    await workflows.publish_version(
        db,
        tenant_id=tenant.id,
        workflow_id=flow.id,
        spec=spec,
        spec_hash="h",
        source="web",
        published_by=owner.id,
        now=NOW,
    )
    trigger = await store.find_trigger(db, tenant_id=tenant.id, workflow_id=flow.id, key="form")
    assert trigger is not None, "publishing made the row"
    secret = new_secret()
    await store.add_secret(
        db,
        trigger=trigger,
        encrypted=encrypt_token(build_multifernet((KEY,)), secret),
        hint=hint_of(secret),
        created_by=owner.id,
        now=NOW,
        overlap=timedelta(0),
    )
    await db.commit()
    return Hook(
        async_sessionmaker(bind=db.bind, expire_on_commit=False),
        trigger.id,
        flow.id,
        owner.id,
        secret,
    )


GOOD = {"event": "form.submitted", "data": {"n": 3}}


async def test_a_signed_delivery_starts_a_run_with_mapped_inputs_once(
    db_session: AsyncSession,
) -> None:
    hook = await make_hook(db_session)
    reply = await hook.post(**hook.signed(GOOD, "msg_1"))
    assert reply.status == 202 and str(reply.body["delivery"]).startswith("dlv_"), reply
    again = await hook.post(**hook.signed(GOOD, "msg_1"))
    assert (again.status, again.body["delivery"], again.body["duplicate"]) == (
        200,
        reply.body["delivery"],
        True,
    ), "a retry answers the first delivery"
    assert await triggers.start_from_events(hook.sm, settings(), now=NOW) == 1, "one run"
    run = await db_session.scalar(select(WorkflowRun))
    assert run is not None and run.inputs == {"n": 3} and run.untrusted_inputs, run
    assert run.trigger_kind == "webhook" and run.mode == "live", run.trigger_kind
    headers = await db_session.scalar(select(WorkflowTriggerEvent.headers))
    assert headers is not None and "authorization" not in headers, "no secret header is kept"


async def test_a_bad_signature_is_logged_without_body_or_delivery_id(
    db_session: AsyncSession,
) -> None:
    hook = await make_hook(db_session)
    forged = hook.signed(GOOD, "msg_1")
    forged["headers"]["webhook-signature"] = "v1,AAAA"
    assert (await hook.post(**forged)).status == 401, "refused"
    event = await db_session.scalar(select(WorkflowTriggerEvent))
    assert event is not None and (event.status, event.payload, event.delivery_id) == (
        "rejected",
        None,
        None,
    ), "nothing the sender wrote is kept but headers"
    assert (await hook.post(**hook.signed(GOOD, "msg_1"))).status == 202, "the id is not taken"
    stale = hook.signed(GOOD, "msg_2", at=NOW - timedelta(minutes=6))
    assert (await hook.post(**stale)).status == 401, "an old timestamp is a replay"


@pytest.mark.parametrize(
    ("payload", "status", "reason"),
    [
        ({"event": "other"}, 200, "start_when"),
        ({"event": "form.submitted"}, 422, "mapping"),
        ({"event": "form.submitted", "data": {"n": "x"}}, 422, "invalid_inputs"),
    ],
)
async def test_filters_and_mapping_answer_the_sender(
    db_session: AsyncSession, payload: dict[str, Any], status: int, reason: str
) -> None:
    hook = await make_hook(db_session)
    reply = await hook.post(**hook.signed(payload, "msg_1"))
    assert reply.status == status, reply
    stored = await db_session.scalar(select(WorkflowTriggerEvent.reason))
    assert stored == reason, stored
    assert await triggers.start_from_events(hook.sm, settings(), now=NOW) == 0, "no run"


async def test_limits_and_switches_refuse_before_anything_is_stored(
    db_session: AsyncSession,
) -> None:
    hook = await make_hook(db_session)
    req = hook.signed(GOOD, "msg_1")
    assert (await hook.post(settings("off"), **req)).status == 404, "webhooks off"
    off = {"workflows": "off", "workflow_webhooks": "on"}
    no_workflows = settings().model_copy(update={"features": off})
    assert (await hook.post(no_workflows, **req)).status == 404, "workflows off"
    small = settings(webhook_max_body_bytes=8)
    assert (await hook.post(small, **req)).status == 413, "over the cap"
    unknown = await ingress.receive(
        hook.sm, settings(), trigger_id=str(uuid.uuid4()), test=False, now=NOW, **req
    )
    assert unknown.status == 404 and unknown == ingress.NOT_FOUND, "an unknown id looks the same"
    assert (await hook.post(test=True, **req)).status == 404, "the test URL is not listening"
    assert await db_session.scalar(select(func.count(WorkflowTriggerEvent.id))) == 0, "none"
    one = settings(webhook_per_minute=1)
    assert (await hook.post(one, **req)).status == 202, "the first"
    assert (await hook.post(one, **hook.signed(GOOD, "msg_2"))).status == 429, "limited"


async def test_a_listening_test_url_captures_one_delivery_as_a_test_run(
    db_session: AsyncSession,
) -> None:
    hook = await make_hook(db_session)
    async with hook.sm.begin() as s:
        trigger = await store.get_trigger(s, hook.trigger_id)
        assert trigger is not None
        until = NOW + timedelta(minutes=10)
        await store.set_listening(s, trigger, until=until, listener=hook.owner)
    reply = await hook.post(test=True, **hook.signed(GOOD, "msg_1"))
    assert reply.status == 202 and str(reply.body["delivery"]).startswith("dlv_test_"), reply
    second = await hook.post(test=True, **hook.signed(GOOD, "msg_2"))
    assert second.status == 404, "the capture closes the test URL"
    assert await triggers.start_from_events(hook.sm, settings(), now=NOW) == 1, "a test run"
    mode = await db_session.scalar(select(WorkflowRun.mode))
    assert mode == "test", mode


async def test_a_body_nested_too_deep_is_refused_before_it_is_stored(
    db_session: AsyncSession,
) -> None:
    """Too deep for an expression to read, it never becomes an accepted event."""
    hook = await make_hook(db_session)
    deep: dict[str, Any] = {"n": 3}
    for _ in range(ingress.MAX_DEPTH):
        deep = {"d": deep}
    reply = await hook.post(**hook.signed({**GOOD, "data": deep}, "msg_1"))
    assert (reply.status, reply.body["error"]) == (422, "invalid_body"), reply
    event = await db_session.scalar(select(WorkflowTriggerEvent))
    assert event is not None and (event.status, event.reason) == ("rejected", "too_deep"), event
    brackets = b"[" * 100_000 + b"]" * 100_000
    assert ingress.parse(brackets, "application/json") is None, "no RecursionError escapes"
