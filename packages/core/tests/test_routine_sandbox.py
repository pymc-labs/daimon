"""The agent version and environment revision a routine sandbox opt-in covers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from daimon.core.routine_sandbox import (
    SandboxPin,
    SandboxPinUnavailable,
    current_pin,
    run_matches_pin,
)
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.routines import create_routine
from daimon.testing.factories import make_tenant
from daimon.testing.ma import MARouter
from daimon.testing.ma_models import ma_agent, ma_environment
from mux.testing.drivers.anthropic.ma import build_fake_anthropic, list_response
from mux.testing.drivers.anthropic.ma_models import ma_session

EDITED = "2026-10-02T09:00:00Z"


def _client(tenant_id: Any, *, archived: datetime | None = None, env_updated: str = EDITED):
    environment = ma_environment(
        id="env_1", name="default", tenant_id=tenant_id, updated_at=env_updated
    )
    router = MARouter()
    router.add(
        "GET",
        r"/v1/agents/ag$",
        lambda r, m: httpx.Response(
            200,
            json=ma_agent(id="ag", version=7, archived_at=archived).model_dump(mode="json"),
        ),
    )
    router.add(
        "GET",
        r"/v1/environments$",
        lambda r, m: list_response([environment.model_dump(mode="json")]),
    )
    router.add(
        "GET",
        r"/v1/environments/env_1$",
        lambda r, m: httpx.Response(200, json=environment.model_dump(mode="json")),
    )
    return build_fake_anthropic(router.dispatch)


async def test_current_pin_reads_the_agent_version_and_environment_revision(
    db_session, db_session_factory
) -> None:
    tenant = await make_tenant(db_session)
    row = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="u1",
        agent_id="ag",
        agent_name="daimon",
        cron_expr="0 9 * * *",
        timezone_="UTC",
        trigger_message="go",
    )
    await db_session.commit()

    pin = await current_pin(
        _client(tenant.id), db_session_factory, row, default=DeploymentDefault()
    )

    assert pin == SandboxPin("ag", 7, "env_1", datetime(2026, 10, 2, 9, tzinfo=UTC))


async def test_current_pin_refuses_an_archived_agent(db_session, db_session_factory) -> None:
    tenant = await make_tenant(db_session)
    row = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="u1",
        agent_id="ag",
        agent_name="daimon",
        cron_expr="0 9 * * *",
        timezone_="UTC",
        trigger_message="go",
    )
    await db_session.commit()

    with pytest.raises(SandboxPinUnavailable):
        await current_pin(
            _client(tenant.id, archived=datetime(2026, 10, 3, tzinfo=UTC)),
            db_session_factory,
            row,
            default=DeploymentDefault(),
        )


@pytest.mark.parametrize(
    ("session_version", "env_updated", "tenant", "matches"),
    [
        (7, EDITED, True, True),
        (8, EDITED, True, False),
        (7, "2026-10-04T00:00:00Z", True, False),
        (7, EDITED, False, False),
    ],
    ids=["unchanged", "agent_edited", "environment_edited", "no_tenant"],
)
async def test_a_run_matches_only_the_exact_agent_version_and_environment(
    session_version: int, env_updated: str, tenant: bool, matches: bool
) -> None:
    import uuid

    tenant_id = uuid.uuid4()
    pin = SandboxPin("ag", 7, "env_1", datetime(2026, 10, 2, 9, tzinfo=UTC))
    session = ma_session(agent_id="ag", environment_id="env_1")
    session = session.model_copy(
        update={"agent": session.agent.model_copy(update={"version": session_version})}
    )

    assert (
        await run_matches_pin(
            _client(tenant_id, env_updated=env_updated),
            pin,
            session,
            tenant_id=tenant_id if tenant else None,
        )
        is matches
    )
