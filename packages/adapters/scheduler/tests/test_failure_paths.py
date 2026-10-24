"""Scheduler failure boundaries preserve completed replies and record failures."""

import unittest.mock
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from anthropic import APIConnectionError, AsyncAnthropic
from daimon.adapters.scheduler.main import _build_fire  # pyright: ignore[reportPrivateUsage]
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scheduler import run_one_tick
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.routines import create_routine, get_routine, update_routine
from daimon.testing.factories import make_ledger_entry, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_main import _make_test_settings  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "case",
    [
        "channel_files_error",
        "dm_files_error",
        "heal_then_fail",
        "heal_edit_then_fail",
        "dm_fail",
        "dm_busy",
    ],
)
async def test_routine_failure_paths_preserve_the_finished_reply_or_error(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    from daimon.core.config import DirectMessagePolicy
    from daimon.core.direct_messages import DirectMessageAccessRevoked
    from daimon.core.errors import TurnError
    from daimon.core.membership import CachedMembershipResolver, Membership
    from daimon.core.routine_delivery import DeliveryOutcome, poll_deliveries_once
    from daimon.core.stores.direct_messages import set_dm_enabled

    now = datetime.now(UTC)
    private = case.startswith("dm_")
    tenant = await make_tenant(db_session, platform="discord", workspace_id="424242")
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("10"))
    row = await create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="123",
        agent_id="old_agent",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="summary",
        next_fire_at=now - timedelta(seconds=1),
        destination_kind="dm" if private else "channel",
        destination_id="123" if private else "456",
        source_provenance_known=True,
    )
    if private:
        await set_dm_enabled(db_session, tenant_id=tenant.id, enabled=True)
    await db_session.commit()
    settings = _make_test_settings(monkeypatch).model_copy(
        update={"direct_message_policies": {tenant.id: DirectMessagePolicy(mode="members")}}
    )

    async def member(*args: Any) -> Membership:
        return Membership(is_member=True)

    async def turn(**kwargs: Any) -> str:
        if case == "heal_edit_then_fail":
            async with db_session_factory.begin() as session:
                await update_routine(
                    session,
                    row.id,
                    tenant_id=row.tenant_id,
                    trigger_message="new configuration",
                    edited_by="123",
                )
        if case in ("heal_then_fail", "heal_edit_then_fail"):
            raise TurnError(kind="upstream", message="provider failed")
        kwargs["on_session"]("ses_completed")
        return "Finished reply"

    async def dm_turn(*args: Any, **kwargs: Any) -> SimpleNamespace:
        if case == "dm_busy":
            raise DirectMessageAccessRevoked("owner_busy", message="Owner is busy")
        if case == "dm_fail":
            raise TurnError(kind="upstream", message="private provider error")
        return SimpleNamespace(
            text="Finished reply", session_id="ses_completed", producing_agent_id="old_agent"
        )

    class NoCaps:
        async def is_over_cap(self, tenant_id: uuid.UUID, user_id: str) -> bool:
            return False

    async with AsyncAnthropic(api_key="offline") as client:
        fire = await _build_fire(
            client=client,
            sm=db_session_factory,
            settings=settings,
            deployment_default=DeploymentDefault(),
            resolver_cache=new_resolver_cache(),
            membership_resolver=CachedMembershipResolver(member),
        )
        with (
            unittest.mock.patch(
                "daimon.adapters.scheduler.main.resolve_agent",
                return_value="healed_agent" if case.startswith("heal_") else "old_agent",
            ),
            unittest.mock.patch(
                "daimon.adapters.scheduler.main.resolve_environment", return_value="env"
            ),
            unittest.mock.patch("daimon.adapters.scheduler.main.run_turn", side_effect=turn),
            unittest.mock.patch(
                "daimon.adapters.scheduler.main.run_dm_routine", side_effect=dm_turn
            ),
            unittest.mock.patch(
                "daimon.core.routine_files.poll_output_files",
                side_effect=APIConnectionError(
                    request=httpx.Request("GET", "https://api.anthropic.com/v1/files")
                ),
            ) as listing,
        ):
            await run_one_tick(
                now=now,
                sm=db_session_factory,
                caps=NoCaps(),
                fire=fire,
                max_age=timedelta(minutes=15),
                max_concurrent_fires=1,
                dispatch_timeout_s=10,
                wait_for_completion=True,
            )
    async with db_session_factory() as session:
        saved = await get_routine(session, row.id, tenant_id=row.tenant_id)
    assert saved is not None
    if case.endswith("files_error"):
        listing.assert_awaited_once()
        assert saved.last_error is None
        assert saved.last_result_tail == "Finished reply"
        assert saved.delivery_status == "pending"
        posted: list[str | None] = []

        async def post(claimed: RoutineRow) -> DeliveryOutcome:
            posted.append(claimed.delivery_payload)
            return DeliveryOutcome(status="delivered")

        await poll_deliveries_once(
            db_session_factory, platform="discord", post=post, now=datetime.now(UTC)
        )
        assert posted == ["Finished reply\n\nGenerated files could not be attached."]
    elif case == "heal_edit_then_fail":
        assert saved.trigger_message == "new configuration"
        assert saved.last_error is None
        assert saved.delivery_status is None
    elif case == "heal_then_fail":
        assert saved.agent_id == "healed_agent"
        assert saved.last_error == "TurnError: upstream: provider failed"
        assert saved.delivery_status == "pending"
        assert saved.delivery_note == "failure_notice"
    else:
        assert saved.last_error == ("owner_busy" if case == "dm_busy" else "execution_failed")
        assert saved.delivery_status is None
        assert saved.delivery_payload is None
