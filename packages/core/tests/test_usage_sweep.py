"""Tests for daimon.core.usage_sweep — headless MCP turn metering backfill.

Headless agent-chat turns (start_turn over MCP) create an MA session and send a
user.message but never drive the SSE stream, so the live `record_turn_usage`
hook never fires and no usage_events/tenant_ledger rows land. This sweep reads
each MA session's `span.model_request_end` events out of band and replays them
through `record_turn_usage`, which is idempotent on (managed_session_id,
event_id) — so repeated sweeps never double-count.

Attribution comes off the session metadata that `create_session` stamps:
`daimon_tenant` (the billed tenant) and `daimon_account` (resolved to the
owning human's platform_user_id for per-member reporting).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from anthropic.types.beta import BetaManagedAgentsModelConfig, BetaManagedAgentsSession
from anthropic.types.beta.beta_managed_agents_session_agent import BetaManagedAgentsSessionAgent
from anthropic.types.beta.sessions.beta_managed_agents_span_model_request_end_event import (
    BetaManagedAgentsSpanModelRequestEndEvent,
)
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from daimon.core._models import UsageEvent
from daimon.core.defaults.metadata import MA_METADATA_KEY_ACCOUNT, MA_METADATA_KEY_TENANT
from daimon.core.usage_sweep import sweep_headless_usage
from daimon.testing.factories import make_platform_principal
from daimon.testing.ma import (
    EMPTY_SESSION_STATS,
    EMPTY_SESSION_USAGE,
    MARouter,
    build_fake_anthropic,
    list_response,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 6, 24, 12, 0, 0, tzinfo=UTC)


def _session_dict(
    *,
    session_id: str,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    model: str = "claude-sonnet-4-6",
) -> dict[str, Any]:
    """A headless MA session tagged the way create_session tags it."""
    s = BetaManagedAgentsSession(
        id=session_id,
        agent=BetaManagedAgentsSessionAgent(
            id="agent_headless1",
            description=None,
            mcp_servers=[],
            model=BetaManagedAgentsModelConfig(id=model),
            name="headless-agent",
            skills=[],
            system=None,
            tools=[],
            type="agent",
            version=1,
        ),
        archived_at=None,
        created_at=NOW,
        environment_id="env_headless1",
        metadata={
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_ACCOUNT: str(account_id),
        },
        resources=[],
        stats=EMPTY_SESSION_STATS,
        status="idle",
        title=None,
        type="session",
        updated_at=NOW,
        usage=EMPTY_SESSION_USAGE,
        vault_ids=[],
    )
    return s.model_dump(mode="json")


def _model_request_end_dict(
    *, event_id: str, input_tokens: int, output_tokens: int
) -> dict[str, Any]:
    event = BetaManagedAgentsSpanModelRequestEndEvent(
        id=event_id,
        is_error=False,
        model_request_start_id="start_1",
        model_usage=BetaManagedAgentsSpanModelUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
        processed_at=NOW,
        type="span.model_request_end",
    )
    return event.model_dump(mode="json")


async def test_sweep_records_usage_for_headless_session_attributed_to_tenant_and_user(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A start_turn-driven session's model_request_end event lands in usage_events
    attributed to its daimon_tenant and the owning account's platform_user_id."""
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="discord-user-42"
    )

    router = MARouter()
    router.add(
        "GET",
        r"/v1/sessions",
        lambda req, m: list_response(
            [
                _session_dict(
                    session_id="sesn_headless",
                    tenant_id=principal.tenant_id,
                    account_id=principal.account_id,
                )
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/sessions/[^/]+/events",
        lambda req, m: list_response(
            [_model_request_end_dict(event_id="evt_1", input_tokens=100, output_tokens=50)]
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    await sweep_headless_usage(client, db_session_factory, markup=Decimal("1.0"))

    rows = (await db_session.execute(select(UsageEvent))).scalars().all()
    assert len(rows) == 1, "sweep should record exactly one usage row for the session's event"
    row = rows[0]
    assert row.tenant_id == principal.tenant_id, "row must be attributed to the session's tenant"
    assert row.platform_user_id == "discord-user-42", (
        "platform_user_id must resolve from the session's daimon_account"
    )
    assert row.managed_session_id == "sesn_headless", "managed_session_id is the swept session id"
    assert row.event_id == "evt_1", "event_id is the span.model_request_end event id"
    assert row.input_tokens == 100, "tokens sourced from event.model_usage"
    assert row.output_tokens == 50, "tokens sourced from event.model_usage"


async def test_sweep_idempotent_across_runs_no_double_count(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Running the sweep twice over the same session records the event once."""
    principal = await make_platform_principal(
        db_session, platform="discord", external_id="discord-user-99"
    )

    router = MARouter()
    router.add(
        "GET",
        r"/v1/sessions",
        lambda req, m: list_response(
            [
                _session_dict(
                    session_id="sesn_replay",
                    tenant_id=principal.tenant_id,
                    account_id=principal.account_id,
                )
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/sessions/[^/]+/events",
        lambda req, m: list_response(
            [_model_request_end_dict(event_id="evt_replay", input_tokens=10, output_tokens=5)]
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    await sweep_headless_usage(client, db_session_factory, markup=Decimal("1.0"))
    await sweep_headless_usage(client, db_session_factory, markup=Decimal("1.0"))

    count = (
        await db_session.execute(
            select(func.count())
            .select_from(UsageEvent)
            .where(UsageEvent.managed_session_id == "sesn_replay")
        )
    ).scalar_one()
    assert count == 1, "replaying the sweep must not double-count the same event"


async def test_sweep_skips_session_without_tenant_tag(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An untagged session (no daimon_tenant — e.g. a DM or foreign session) is
    skipped: no usage row, and its events are never even fetched."""
    s = BetaManagedAgentsSession(
        id="sesn_untagged",
        agent=BetaManagedAgentsSessionAgent(
            id="agent_x",
            description=None,
            mcp_servers=[],
            model=BetaManagedAgentsModelConfig(id="claude-sonnet-4-6"),
            name="x",
            skills=[],
            system=None,
            tools=[],
            type="agent",
            version=1,
        ),
        archived_at=None,
        created_at=NOW,
        environment_id="env_x",
        metadata={},
        resources=[],
        stats=EMPTY_SESSION_STATS,
        status="idle",
        title=None,
        type="session",
        updated_at=NOW,
        usage=EMPTY_SESSION_USAGE,
        vault_ids=[],
    )
    router = MARouter()
    router.add("GET", r"/v1/sessions", lambda req, m: list_response([s.model_dump(mode="json")]))

    def _events_must_not_be_called(req: httpx.Request, m: Any) -> httpx.Response:
        raise AssertionError("events must not be fetched for an untagged session")

    router.add("GET", r"/v1/sessions/[^/]+/events", _events_must_not_be_called)
    client = build_fake_anthropic(router.dispatch)

    recorded = await sweep_headless_usage(client, db_session_factory, markup=Decimal("1.0"))

    assert recorded == 0, "untagged session contributes no recorded events"
    count = (await db_session.execute(select(func.count()).select_from(UsageEvent))).scalar_one()
    assert count == 0, "no usage row for an untagged session"


async def test_sweep_skips_session_whose_tenant_is_not_in_db(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A session tagged with a daimon_tenant that has no Tenant row in this DB is
    skipped — no usage row, events never fetched. A shared MA workspace holds
    sessions from other deployments/evals; recording them would trip the
    usage_events tenant_id FK and crash the scheduler tick."""
    foreign_tenant = uuid.uuid4()  # never inserted into tenants
    router = MARouter()
    router.add(
        "GET",
        r"/v1/sessions",
        lambda req, m: list_response(
            [
                _session_dict(
                    session_id="sesn_foreign",
                    tenant_id=foreign_tenant,
                    account_id=uuid.uuid4(),
                )
            ]
        ),
    )

    def _events_must_not_be_called(req: httpx.Request, m: Any) -> httpx.Response:
        raise AssertionError("events must not be fetched for a foreign-tenant session")

    router.add("GET", r"/v1/sessions/[^/]+/events", _events_must_not_be_called)
    client = build_fake_anthropic(router.dispatch)

    recorded = await sweep_headless_usage(client, db_session_factory, markup=Decimal("1.0"))

    assert recorded == 0, "foreign-tenant session contributes no recorded events"
    count = (await db_session.execute(select(func.count()).select_from(UsageEvent))).scalar_one()
    assert count == 0, "no usage row for a tenant this deployment does not own"


async def test_sweep_records_with_null_platform_user_when_account_has_no_discord_principal(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An account with no discord principal still bills the tenant: the usage row
    is written with platform_user_id=None (the ledger debit keys on tenant_id)."""
    from daimon.testing.factories import make_account

    account = await make_account(db_session)  # account + tenant, no discord principal

    router = MARouter()
    router.add(
        "GET",
        r"/v1/sessions",
        lambda req, m: list_response(
            [
                _session_dict(
                    session_id="sesn_no_principal",
                    tenant_id=account.tenant_id,
                    account_id=account.id,
                )
            ]
        ),
    )
    router.add(
        "GET",
        r"/v1/sessions/[^/]+/events",
        lambda req, m: list_response(
            [_model_request_end_dict(event_id="evt_np", input_tokens=7, output_tokens=3)]
        ),
    )
    client = build_fake_anthropic(router.dispatch)

    await sweep_headless_usage(client, db_session_factory, markup=Decimal("1.0"))

    rows = (await db_session.execute(select(UsageEvent))).scalars().all()
    assert len(rows) == 1, "usage is recorded even without a resolvable platform user"
    assert rows[0].platform_user_id is None, "platform_user_id is None when no discord principal"
    assert rows[0].tenant_id == account.tenant_id, "tenant attribution still correct"
