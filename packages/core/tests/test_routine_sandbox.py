"""The agent version and environment revision a routine sandbox opt-in covers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from daimon.core.channel_changes import ChannelChangeRefused
from daimon.core.mux_backend import resource_scope
from daimon.core.mux_compat import create_owned_environment
from daimon.core.routine_sandbox import (
    AgentSandboxPin,
    SandboxPin,
    current_agent_pin,
    current_pin,
    run_matches_pin,
)
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.routines import create_routine
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import MARouter
from daimon.testing.ma_models import ma_agent, ma_environment
from mux.contracts.extensions import ExtensionConfig
from mux.contracts.resources import EnvironmentSpec
from mux.testing.drivers.anthropic.ma import build_fake_anthropic, list_response
from mux.testing.drivers.anthropic.ma_models import ma_session
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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

    with pytest.raises(ChannelChangeRefused):
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


async def test_a_run_in_a_daimon_owned_environment_matches_its_pin(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    created = await create_owned_environment(
        None,
        EnvironmentSpec(
            name="default",
            execution="self_hosted",
            native_config=ExtensionConfig(
                namespace="selfhosted.environment",
                version=1,
                value={
                    "harness_url": "https://harness.example",
                    "auth_mode": "bearer_env",
                    "auth_ref": "HARNESS_TOKEN",
                },
            ),
        ),
        scope=resource_scope(tenant_id=str(tenant.id), account_id=str(account.id)),
        records=db_session_factory,
    )
    pin = SandboxPin("ag", 7, created.id, datetime.fromisoformat(created.updated_at))
    session = ma_session(agent_id="ag", environment_id=created.id)
    session = session.model_copy(update={"agent": session.agent.model_copy(update={"version": 7})})

    assert await run_matches_pin(
        _client(tenant.id), pin, session, tenant_id=tenant.id, records=db_session_factory
    ), "the pinned environment is read from the records, not from Managed Agents"
    assert not await run_matches_pin(_client(tenant.id), pin, session, tenant_id=tenant.id), (
        "without the records the same id cannot be read and the run is a mismatch"
    )


AGENT_PIN = AgentSandboxPin("ag", "default")


@pytest.mark.parametrize(
    ("session_version", "env_updated", "agent_id", "environment_id", "matches"),
    [
        (7, EDITED, "ag", "env_1", True),
        (8, EDITED, "ag", "env_1", True),
        (7, "2026-10-04T00:00:00Z", "ag", "env_1", True),
        (7, EDITED, "other", "env_1", False),
        (7, EDITED, "ag", "env_2", False),
    ],
    ids=["unchanged", "agent_edited", "environment_edited", "other_agent", "other_environment"],
)
async def test_an_agent_opt_in_matches_the_agent_and_environment_but_not_a_version(
    session_version: int, env_updated: str, agent_id: str, environment_id: str, matches: bool
) -> None:
    """An agent edit or an in-place environment edit is not drift for an agent pin."""
    import uuid

    tenant_id = uuid.uuid4()
    session = ma_session(agent_id=agent_id, environment_id=environment_id)
    session = session.model_copy(
        update={"agent": session.agent.model_copy(update={"version": session_version})}
    )

    assert (
        await run_matches_pin(
            _client(tenant_id, env_updated=env_updated), AGENT_PIN, session, tenant_id=tenant_id
        )
        is matches
    ), "only the agent id and the environment its name resolves to count"


async def test_an_agent_edit_keeps_builtins_under_the_agent_pin_and_drops_them_under_a_routine_pin():
    """The same edited agent matches an agent pin and no longer matches a routine pin."""
    import uuid

    tenant_id = uuid.uuid4()
    routine_pin = SandboxPin("ag", 7, "env_1", datetime(2026, 10, 2, 9, tzinfo=UTC))
    session = ma_session(agent_id="ag", environment_id="env_1")
    edited = session.model_copy(update={"agent": session.agent.model_copy(update={"version": 8})})
    client = _client(tenant_id)

    assert await run_matches_pin(client, AGENT_PIN, edited, tenant_id=tenant_id)
    assert not await run_matches_pin(client, routine_pin, edited, tenant_id=tenant_id)


async def test_an_agent_pin_needs_a_tenant() -> None:
    """Without a tenant nothing can be read, so the run is a mismatch."""
    session = ma_session(agent_id="ag", environment_id="env_1")
    import uuid

    assert not await run_matches_pin(_client(uuid.uuid4()), AGENT_PIN, session, tenant_id=None)


async def test_current_agent_pin_names_the_agent_and_the_default_environment(
    db_session, db_session_factory
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    router = MARouter()
    agent = ma_agent(id="ag", name="daimon", tenant_id=tenant.id)
    environment = ma_environment(id="env_1", name="default", tenant_id=tenant.id)
    router.add("GET", r"/v1/agents$", lambda r, m: list_response([agent.model_dump(mode="json")]))
    router.add(
        "GET",
        r"/v1/environments$",
        lambda r, m: list_response([environment.model_dump(mode="json")]),
    )
    client = build_fake_anthropic(router.dispatch)

    pin = await current_agent_pin(
        client,
        db_session_factory,
        tenant_id=tenant.id,
        agent_name="daimon",
        environment_name=None,
        default=DeploymentDefault(),
    )
    assert pin == AgentSandboxPin("ag", "default")
    with pytest.raises(ChannelChangeRefused):
        await current_agent_pin(
            client,
            db_session_factory,
            tenant_id=tenant.id,
            agent_name="daimon",
            environment_name="missing",
            default=DeploymentDefault(),
        )
