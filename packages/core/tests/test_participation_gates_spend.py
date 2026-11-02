"""Participation runs the spend gate before it pays for the classifier."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
from daimon.core.config import ThreadParticipationSettings
from daimon.core.participation_gates import ParticipationGates
from daimon.core.stores import spend_allowances
from daimon.core.thread_participation import (
    ClassifierMessage,
    ParticipationMode,
    ResolvedParticipation,
)
from daimon.testing.factories import make_ledger_entry, make_tenant
from mux.testing.isolated_sdk import build_async_anthropic
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def test_an_exhausted_allowance_skips_the_classifier(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("10"))
    await spend_allowances.create_allowance(
        db_session,
        tenant_id=tenant.id,
        subject_kind="person",
        subject_id="caller",
        agent_id=None,
        amount_usd=Decimal(0),
        window="total",
        starts_at=datetime.now(UTC) - timedelta(days=1),
    )
    await db_session.commit()
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    client = build_async_anthropic(
        transport=httpx.MockTransport(handle), api_key="test", base_url="https://api.anthropic.com"
    )
    recent = AsyncMock(return_value=[])
    gates = ParticipationGates(
        platform="discord",
        settings=ThreadParticipationSettings(mode=ParticipationMode.ON),
        sessionmaker=db_session_factory,
        anthropic=client,
        bot_display_name="Daimon",
        billing_config=None,
        markup=Decimal(1),
    )
    try:
        result = await gates.should_respond(
            tenant_id=tenant.id,
            channel_id="channel",
            thread_id="thread",
            caller_id="caller",
            candidates=[ClassifierMessage("person", "organic message", False)],
            recent=recent,
            resolved=ResolvedParticipation(ParticipationMode.ON, "thread"),
        )
    finally:
        await client.close()
    assert result is False, "a person whose allowance is used up is not answered unprompted"
    assert requests == [], "the classifier is not paid for"
    recent.assert_not_awaited()
