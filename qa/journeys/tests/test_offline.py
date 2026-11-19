"""A scripted Discord turn with the real adapter, SDK, turn, and store effects."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))

import httpx
import pytest
from anthropic import APIConnectionError
from daimon.core.scope import ChannelScopeRef
from daimon.core.stores import tenant_ledger, usage_events
from daimon.core.stores.scoped_config_read import get_scope
from daimon.core.stores.scoped_config_write import set_fields
from daimon.testing import ma_agent, tenant_metadata
from daimon.testing.db import db_clean as db_clean
from daimon.testing.db import db_engine as db_engine
from daimon.testing.db import db_schema as db_schema
from daimon.testing.db import db_session as db_session
from daimon.testing.db import db_session_factory as db_session_factory
from daimon.testing.factories import make_tenant
from daimon.testing.ma import build_no_retry_anthropic, list_response
from daimon.testing.turn_router import build_turn_router
from parity.drivers.discord_driver import DiscordDriver  # pyright: ignore[reportMissingImports]
from parity.drivers.protocol import PlatformDriver  # pyright: ignore[reportMissingImports]
from parity.drivers.slack_driver import SlackDriver  # pyright: ignore[reportMissingImports]
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from qa.journeys.evidence import Check, Observation, RunReport, create_artifact_dir, write_report
from qa.journeys.offline import LockedRouter, _offline_environment, block_real_http_transport


@pytest.fixture(autouse=True)
def no_real_http() -> Iterator[None]:
    with block_real_http_transport():
        yield


@pytest.mark.parametrize(
    ("platform", "driver", "workspace_id", "channel_id", "user_id"),
    [
        pytest.param("discord", DiscordDriver(), "900001001", "100000", "555000111", id="discord"),
        pytest.param(
            "slack", SlackDriver(), "T_QA_JOURNEY", "C_QA_JOURNEY", "U_QA_JOURNEY", id="slack"
        ),
    ],
)
async def test_scripted_turn_matches_backend_and_delivery(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    platform: str,
    driver: PlatformDriver,
    workspace_id: str,
    channel_id: str,
    user_id: str,
) -> None:
    tenant = await make_tenant(db_session, platform=platform, workspace_id=workspace_id)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("100.00"),
        reason="trial",
        idempotency_key=f"trial:{tenant.id}",
    )
    await db_session.commit()

    router = LockedRouter()
    sent_bodies: list[dict[str, object]] = []
    stream_hits: list[str] = []
    build_turn_router(
        str(tenant.id),
        agent_text="Journey completed",
        input_tokens=37,
        output_tokens=11,
        sent_event_bodies=sent_bodies,
        stream_hits=stream_hits,
        router=router,
    )
    started = datetime.now(UTC)
    posted = await driver.dispatch_turn(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant.id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        text="perform journey",
    )
    usage = await usage_events.list_for_tenant(db_session, tenant_id=tenant.id)
    ledger = await tenant_ledger.list_for_tenant(db_session, tenant_id=tenant.id)
    debits = [row for row in ledger if row.delta_usd < 0]

    assert router.unexpected_requests == []
    assert len(sent_bodies) == 1
    assert stream_hits == ["sess_parity_test"]
    assert len(usage) == 1
    assert usage[0].input_tokens == 37
    assert usage[0].output_tokens == 11
    assert len(debits) == 1
    assert any("Journey completed" in item for item in posted)
    run_id = f"scripted-{platform}-turn-{started.strftime('%Y%m%dT%H%M%S%fZ')}"
    report = RunReport(
        run_id,
        f"{platform}-turn-delivery-and-ledger",
        "scripted",
        started,
        finished_at=datetime.now(UTC),
        # Scripted usage is a store effect; the run spent zero provider tokens.
        app_input_tokens=0,
        app_output_tokens=0,
        checks=[
            Check(
                "turn reached scripted SDK transport",
                "PASS",
                [
                    Observation(
                        "scripted_transport", True, True, f"{len(sent_bodies)} send; {stream_hits}"
                    )
                ],
            ),
            Check(
                "reply delivered through adapter",
                "PASS",
                [Observation("adapter_entry", True, True, str(posted))],
                detail="Parity driver stubs create_session and build_context_xml; session creation is outside this journey.",
            ),
            Check(
                "usage and ledger debit stored",
                "PASS",
                [
                    Observation(
                        "backend_effect", True, True, f"{len(usage)} usage; {len(debits)} debit"
                    )
                ],
            ),
            Check(
                "native client rendered reply",
                "PENDING",
                [
                    Observation(
                        "native_client", True, False, "offline adapter cannot see native client"
                    )
                ],
            ),
        ],
    )
    artifact_root = (
        Path(os.environ["DAIMON_QA_ARTIFACT_ROOT"])
        if "DAIMON_QA_ARTIFACT_ROOT" in os.environ
        else tmp_path
    )
    artifact = write_report(report, create_artifact_dir(run_id, artifact_root))
    assert report.status == "PENDING"
    assert report.app_input_tokens == report.app_output_tokens == 0
    assert artifact.exists()
    print(f"offline journey evidence: {artifact}")


@pytest.mark.parametrize(
    ("platform", "driver", "workspace_id", "channel_id", "user_id"),
    [
        pytest.param("discord", DiscordDriver(), "900001002", "100001", "555000112", id="discord"),
        pytest.param("slack", SlackDriver(), "T_QA_SETUP", "C_QA_SETUP", "U_QA_SETUP", id="slack"),
    ],
)
async def test_setup_panel_scopes_member_roster_without_changing_route(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    platform: str,
    driver: PlatformDriver,
    workspace_id: str,
    channel_id: str,
    user_id: str,
) -> None:
    """Real setup handlers limit a member's roster to the routed agent."""
    tenant = await make_tenant(db_session, platform=platform, workspace_id=workspace_id)
    scope = ChannelScopeRef(tenant_id=tenant.id, channel_id=channel_id)
    await set_fields(
        db_session, scope=scope, tenant_id=tenant.id, agent_name="answering-here", mode="agent"
    )
    await db_session.commit()
    routed_before = await get_scope(db_session, scope=scope)
    assert routed_before is not None and routed_before.agent_name == "answering-here"

    router = LockedRouter()
    agents = [
        ma_agent(
            id=f"ag_{name}",
            name=name,
            metadata=tenant_metadata(tenant.id, name),
        )
        for name in ("answering-here", "unrouted-agent")
    ]
    router.add_agent_list(*agents)
    for agent in agents:
        router.add_agent(agent)
    router.add("GET", r"/v1/skills", lambda _request, _match: list_response([]))
    router.add("GET", r"/v1/environments", lambda _request, _match: list_response([]))

    started = datetime.now(UTC)
    member_view = await driver.open_setup_panel(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant.id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        is_admin=False,
    )
    admin_view = await driver.open_setup_panel(
        sessionmaker=db_session_factory,
        router=router,
        tenant_id=tenant.id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        user_id=user_id,
        is_admin=True,
    )
    routed_after = await get_scope(db_session, scope=scope)
    assert routed_after is not None and routed_after.agent_name == "answering-here"
    assert "answering-here" in member_view.body
    assert "unrouted-agent" not in member_view.body
    assert "unrouted-agent" in admin_view.body
    assert router.unexpected_requests == []

    run_id = f"scripted-{platform}-setup-{started.strftime('%Y%m%dT%H%M%S%fZ')}"
    report = RunReport(
        run_id,
        f"{platform}-setup-roster-scope",
        "scripted",
        started,
        finished_at=datetime.now(UTC),
        checks=[
            Check(
                "member sees only reachable agent",
                "PASS",
                [Observation("adapter_entry", True, True, member_view.body)],
            ),
            Check(
                "admin sees tenant roster",
                "PASS",
                [Observation("adapter_entry", True, True, admin_view.body)],
            ),
            Check(
                "channel route unchanged by reads",
                "PASS",
                [Observation("backend_effect", True, True, routed_after.agent_name)],
            ),
            Check(
                "native client rendered setup panel",
                "PENDING",
                [Observation("native_client", True, False, "offline handler has no native client")],
            ),
        ],
    )
    artifact_root = (
        Path(os.environ["DAIMON_QA_ARTIFACT_ROOT"])
        if "DAIMON_QA_ARTIFACT_ROOT" in os.environ
        else tmp_path
    )
    artifact = write_report(report, create_artifact_dir(run_id, artifact_root))
    assert report.status == "PENDING"
    assert artifact.exists()
    print(f"offline journey evidence: {artifact}")


async def test_unexpected_scripted_request_is_rejected_before_network() -> None:
    router = LockedRouter()
    client = build_no_retry_anthropic(router)
    with pytest.raises(APIConnectionError):
        await client.beta.sessions.retrieve("sess_unplanned")
    assert router.unexpected_requests == [
        "GET https://api.anthropic.com/v1/sessions/sess_unplanned?beta=true"
    ]


async def test_new_real_http_client_is_refused() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(AssertionError, match="offline journey attempted real HTTP transport"):
            await client.get("https://api.anthropic.com/v1/sessions")


def test_launcher_environment_excludes_provider_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DAIMON_DATABASE__TEST_URL", "postgresql+asyncpg://local/test_db")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-escape")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-escape")
    env = _offline_environment(tmp_path)
    assert "ANTHROPIC_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert env["DAIMON_DATABASE__TEST_URL"].endswith("test_db")
