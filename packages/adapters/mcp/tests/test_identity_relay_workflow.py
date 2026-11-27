"""A workflow's routine step signs exactly the claims its routine would: the same room, owner,
DM and requester stamp, with its workflow named as the routine."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest
from daimon.core.audience_facts import RoomFacts
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.mcp_identity import RelayBinding, routine_destination_key, routine_revision
from daimon.core.stores import identity
from daimon.core.stores.domain import Role
from daimon.core.stores.lead_intake import workflow_execution
from daimon.core.stores.routines import create_routine
from daimon.core.stores.thread_sessions import create_thread_session
from daimon.core.stores.turn_origins import bind_origin_session, create_origin
from daimon.core.workflows.fire_store import bound_step
from daimon.testing.workflows import make_world
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_identity_relay import OWNER, SERVER, _Person, _token, _tool_call
from .test_identity_relay_room import _facts, _signed

DM_SERVER = SERVER.model_copy(
    update={"routine_claims": True, "dm_routine_claims": True, "requester_meta": True}
)
WS = "discord:guild-1"
TARGETS: dict[str, dict[str, Any]] = {
    "channel": {"kind": "channel", "id": "555"},
    "dm": {"kind": "dm"},
}


async def _dm_turn(sm: async_sessionmaker[AsyncSession], person: _Person, grant: str) -> uuid.UUID:
    """An unattended turn in a fresh `dm:` scope holding `grant`; its session's id."""
    now = dt.datetime.now(dt.UTC)
    scope, mapping_id = f"dm:{uuid.uuid4()}", uuid.uuid4()
    async with sm.begin() as db:
        await create_thread_session(
            db, id=mapping_id, tenant_id=person.tenant_id, platform="discord", thread_id=scope,
            account_id=person.account_id, ma_session_id=f"sess_{grant}", ma_agent_id="agent_a",
        )  # fmt: skip
        origin = await create_origin(
            db, tenant_id=person.tenant_id, account_id=person.account_id, platform="discord",
            parent_channel_id=scope, thread_id=scope, responder_ma_agent_id="agent_a",
            responder_name="Helper", configuration_target_ma_agent_id=None,
            configuration_target_name=None, role=Role.USER,
            expires_at=now + dt.timedelta(hours=1), now=now, sender_foreign=False,
            shared_channel=False, attended=False, own_dm_reach=True,
        )  # fmt: skip
        await bind_origin_session(
            db, origin_id=origin.id, chat_session_id=grant, human_invocation=False
        )
    return mapping_id


@pytest.mark.parametrize("target", list(TARGETS))
async def test_a_routine_step_signs_the_claims_its_routine_would(
    db_session: AsyncSession, target: str
) -> None:
    to = TARGETS[target]
    step = {"kind": "routine", "agent": "helper", "prompt": "go", "to": to, "next": "f"}
    world = await make_world(
        db_session, {"r": step, "f": {"kind": "finish"}}, "r", home="555", workspace_id="guild-1"
    )
    sm = world.sm
    person = _Person(
        world.tenant_id,
        world.owner,
        derive_agent_uuid(tenant_id=world.tenant_id, ma_agent_id="agent_a"),
    )
    async with sm.begin() as s:
        await identity.create_platform_principal(
            s, tenant_id=world.tenant_id, platform="discord", external_id=OWNER,
            account_id=world.owner,
        )  # fmt: skip
        row = await create_routine(
            s, tenant_id=world.tenant_id, created_by_user_id=OWNER, agent_id="agent_a",
            agent_name="helper", cron_expr="0 9 * * *", timezone_="UTC", trigger_message="go",
            destination_kind=to["kind"], destination_id=to.get("id", OWNER),
        )  # fmt: skip
    await _facts(sm, person, {"555": RoomFacts(contained=False, narrow=False)})
    run = await world.run()
    async with sm() as s:
        step = await bound_step(s, tenant_id=world.tenant_id, run_id=run.id)
    assert step is not None and step.subject.revision is not None
    subject = step.subject
    destination = routine_destination_key(row)
    body = _tool_call(requester_platform_user_id="666", requester_kind="person")
    if target == "channel":
        routine = RelayBinding("helper", routine_id=row.id, owner=OWNER, destination=destination)
        as_routine = await _signed(sm, await _token(sm, person, routine), DM_SERVER, body)
        as_step_binding = RelayBinding(
            "helper", workflow_run_id=run.id, owner=OWNER, destination=destination
        )
        as_step = await _signed(sm, await _token(sm, person, as_step_binding), DM_SERVER, body)
    else:
        execution = uuid.uuid4()
        async with sm.begin() as s:
            await s.execute(
                text(
                    "INSERT INTO lead_intake_executions (tenant_id, id, routine_id, expires_at)"
                    " VALUES (:tenant, :id, :routine, now() + interval '5 minutes')"
                ),
                {"tenant": world.tenant_id, "id": execution, "routine": row.id},
            )

        def dm(
            mapping: uuid.UUID, grant: str, live: uuid.UUID, revision: str, **holder: Any
        ) -> RelayBinding:
            return RelayBinding(
                "helper", mapping, grant=grant, owner=OWNER, destination=destination,
                execution=live, revision=revision, dm_channel="777", **holder,
            )  # fmt: skip

        routine = dm(
            await _dm_turn(sm, person, "g-routine"), "g-routine", execution,
            row.updated_at.isoformat(), routine_id=row.id,
        )  # fmt: skip
        as_routine = await _signed(sm, await _token(sm, person, routine), DM_SERVER, body)
        async with workflow_execution(sm, world.tenant_id, run.id, lifetime_s=60) as grant:
            stepped = dm(
                await _dm_turn(sm, person, "g-step"), "g-step", grant,
                subject.revision, workflow_run_id=run.id,
            )  # fmt: skip
            as_step = await _signed(sm, await _token(sm, person, stepped), DM_SERVER, body)
    claims, stamp = as_routine
    assert claims["room"] in ("team", "dm"), claims
    assert (claims["routine"], claims["rev"]) == (str(row.id), routine_revision(row, workspace=WS))
    named = {"routine": str(subject.id), "rev": routine_revision(subject, workspace=WS)}
    assert as_step == ({**claims, **named, "routine_name": subject.name}, stamp), (
        "the same claims and requester stamp, naming its workflow as the routine"
    )
