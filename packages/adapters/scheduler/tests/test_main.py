"""Tests for the scheduler entrypoint wiring.

The scheduler-adapter cap-and-meter wiring replaces the
``_StubCaps`` and ``_stub_usage_record`` placeholders with real
calls into ``daimon.core.billing.is_over_cap`` and
``daimon.core.usage_recording.record_turn_usage``. These tests exercise
that wiring at the unit level — they do not boot the full ``run()``
lifecycle (covered by ``tests/integration/test_routines_end_to_end.py``).
"""

from __future__ import annotations

import functools
import os
import unittest.mock
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.scheduler.main import (
    _build_fire,  # pyright: ignore[reportPrivateUsage]  # test seam for balance gate + debit binding
    _CapsAdapter,  # pyright: ignore[reportPrivateUsage]  # named test seam for cap wiring
    _settle_promo_credit,  # pyright: ignore[reportPrivateUsage]  # test seam for the promo settlement wrapper
    _sweep_retired_turn_card_intents,  # pyright: ignore[reportPrivateUsage]  # test seam for the card-intent sweep wrapper
    _sweep_slack_event_dedup,  # pyright: ignore[reportPrivateUsage]  # test seam for the slack_event_dedup sweep wrapper
    _sweep_wizard_sessions,  # pyright: ignore[reportPrivateUsage]  # test seam for the wizard sweep wrapper
    _validate_mcp_settings,  # pyright: ignore[reportPrivateUsage]  # boot-validation seam
)
from daimon.core.billing import BillingConfig
from daimon.core.config import Settings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.pricing import MODEL_PRICING, ModelRates
from daimon.core.promo_codes import build_promo_code_terms
from daimon.core.scheduler import run_one_tick
from daimon.core.scope import ChannelScopeRef, DeploymentDefault, TenantScopeRef
from daimon.core.stores import promo_codes as promo_store
from daimon.core.stores import tenant_ledger, tenant_user_caps, usage_events
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.routines import create_routine, get_routine
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.usage_recording import record_turn_usage
from daimon.testing import ma_model_usage
from daimon.testing.factories import make_channel_budget, make_ledger_entry, make_tenant
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


@pytest.fixture
def db_session_factory(
    db_engine: AsyncEngine,
    db_session: AsyncSession,
) -> async_sessionmaker[AsyncSession]:
    """Concurrent fire finalizers need independent connections, as in production.

    db_session triggers schema cleanup; these tests commit setup before dispatch.
    """
    return async_sessionmaker(bind=db_engine, expire_on_commit=False)


_TEST_BILLING = BillingConfig(
    secret_key=SecretStr("sk_test"),
    webhook_secret=SecretStr("whsec_test"),
    prices={10: "p10", 25: "p25", 50: "p50", 100: "p100"},
    success_url="http://test/success",
    cancel_url="http://test/cancel",
)


async def test_caps_adapter_returns_true_when_user_over_cap(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """_CapsAdapter delegates to billing.is_over_cap and reflects DB state."""
    tenant = await make_tenant(db_session)
    await tenant_user_caps.set_default(db_session, tenant_id=tenant.id, amount=Decimal("0.01"))
    await usage_events.record(
        db_session,
        tenant_id=tenant.id,
        platform_user_id="u1",
        managed_session_id="prev_sess",
        model="claude-opus-4-7",
        model_usage=ma_model_usage(input_tokens=10_000_000, output_tokens=10_000_000),
        event_id="prev_evt",
    )
    await db_session.commit()

    adapter = _CapsAdapter(db_session_factory, billing_config=_TEST_BILLING)
    over = await adapter.is_over_cap(tenant.id, "u1")
    assert over is True, "user with usage above cap must be over_cap"

    other = await adapter.is_over_cap(tenant.id, "u2")
    assert other is False, "uncharged user under same cap must be under"


async def test_caps_adapter_returns_false_when_no_cap_row(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    adapter = _CapsAdapter(db_session_factory, billing_config=_TEST_BILLING)
    over = await adapter.is_over_cap(tenant.id, "u1")
    assert over is False, "no cap row -> uncapped"


async def test_fire_skips_on_over_cap(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """End-to-end at run_one_tick level: real _CapsAdapter sees an over-cap
    user; the routine is skipped and last_error is 'cap_exceeded'."""
    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)

    tenant = await make_tenant(db_session)
    row = await create_routine(
        db_session,
        created_by_user_id="u1",
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="trigger",
        next_fire_at=now - timedelta(minutes=1),
        tenant_id=tenant.id,
    )
    await tenant_user_caps.set_default(db_session, tenant_id=tenant.id, amount=Decimal("0.01"))
    await usage_events.record(
        db_session,
        tenant_id=tenant.id,
        platform_user_id="u1",
        managed_session_id="prev_sess",
        model="claude-opus-4-7",
        model_usage=ma_model_usage(input_tokens=10_000_000, output_tokens=10_000_000),
        event_id="prev_evt",
    )
    await db_session.commit()

    fired: list[uuid.UUID] = []

    async def fake_fire(r: RoutineRow) -> None:
        fired.append(r.id)

    caps = _CapsAdapter(db_session_factory, billing_config=_TEST_BILLING)

    await run_one_tick(
        now=now,
        sm=db_session_factory,
        caps=caps,
        fire=fake_fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=10,
        dispatch_timeout_s=5.0,
        wait_for_completion=True,
    )

    assert fired == [], "over-cap routine must not fire"
    async with db_session_factory() as s:
        fetched = await get_routine(s, row.id, tenant_id=tenant.id)
    assert fetched is not None
    assert fetched.last_error == "cap_exceeded", (
        f"over-cap routine must record 'cap_exceeded'; got {fetched.last_error!r}"
    )


async def test_run_one_tick_resolves_tenant_per_row(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """run_one_tick passes each row's own tenant_id to fire — not a shared singleton."""
    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)

    tenant_a = await make_tenant(db_session)
    tenant_b = await make_tenant(db_session)

    await create_routine(
        db_session,
        created_by_user_id="ua",
        agent_id="agent_a",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="trigger-a",
        next_fire_at=now - timedelta(minutes=1),
        tenant_id=tenant_a.id,
    )
    await create_routine(
        db_session,
        created_by_user_id="ub",
        agent_id="agent_b",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="trigger-b",
        next_fire_at=now - timedelta(minutes=1),
        tenant_id=tenant_b.id,
    )
    await db_session.commit()

    captured_tenant_ids: list[uuid.UUID] = []

    async def recording_fire(r: RoutineRow) -> None:
        captured_tenant_ids.append(r.tenant_id)

    caps = _CapsAdapter(db_session_factory, billing_config=None)

    await run_one_tick(
        now=now,
        sm=db_session_factory,
        caps=caps,
        fire=recording_fire,
        max_age=timedelta(minutes=15),
        max_concurrent_fires=10,
        dispatch_timeout_s=5.0,
        wait_for_completion=True,
    )

    assert set(captured_tenant_ids) == {
        tenant_a.id,
        tenant_b.id,
    }, "each routine must fire under its own tenant_id, not a shared boot singleton"


def _isolate_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip DAIMON_* env vars + repo .env so tests see exactly what they construct."""
    for name in list(os.environ):
        if name.startswith("DAIMON_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DAIMON_DATABASE__URL", "postgresql+asyncpg://u:p@h:5432/d")
    monkeypatch.setenv("DAIMON_ANTHROPIC__API_KEY", "sk-test")


def test_validate_mcp_settings_raises_when_jwt_secret_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boot must fail fast when DAIMON_MCP__JWT_SECRET is unset — routine fires
    cannot bind the daimon-mcp vault without the signing secret."""
    _isolate_settings_env(monkeypatch)
    monkeypatch.setenv("DAIMON_MCP__PUBLIC_URL", "https://mcp.example.com/mcp")
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]

    with pytest.raises(RuntimeError, match="DAIMON_MCP__JWT_SECRET"):
        _validate_mcp_settings(settings)


def test_validate_mcp_settings_raises_when_public_url_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boot must fail fast when DAIMON_MCP__PUBLIC_URL is unset — without it
    ensure_mcp_vault silently skips and the per-fire vault path never runs."""
    _isolate_settings_env(monkeypatch)
    monkeypatch.setenv("DAIMON_MCP__JWT_SECRET", "a" * 32)
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]

    with pytest.raises(RuntimeError, match="DAIMON_MCP__PUBLIC_URL"):
        _validate_mcp_settings(settings)


def test_validate_mcp_settings_returns_none_when_both_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both fields populated -> no raise, returns None."""
    _isolate_settings_env(monkeypatch)
    monkeypatch.setenv("DAIMON_MCP__JWT_SECRET", "a" * 32)
    monkeypatch.setenv("DAIMON_MCP__PUBLIC_URL", "https://mcp.example.com/mcp")
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]

    result = _validate_mcp_settings(settings)
    assert result is None, "_validate_mcp_settings must return None on success"


def _make_test_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Build a minimal Settings for _build_fire tests (no real .env read)."""
    _isolate_settings_env(monkeypatch)
    monkeypatch.setenv("DAIMON_MCP__JWT_SECRET", "a" * 32)
    monkeypatch.setenv("DAIMON_MCP__PUBLIC_URL", "https://mcp.example.com/mcp")
    return Settings(_env_file=None)  # pyright: ignore[reportCallIssue]


async def test_fire_records_error_and_skips_run_turn_when_created_by_user_id_is_none(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_fire records 'routine has no created_by_user_id' and does NOT call run_turn
    when the RoutineRow has created_by_user_id=None (main.py:152-160 bail branch)."""
    now = datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)

    tenant = await make_tenant(db_session)
    row = await create_routine(
        db_session,
        created_by_user_id=None,
        agent_id="agent_no_user",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="trigger",
        next_fire_at=now - timedelta(minutes=1),
        tenant_id=tenant.id,
    )
    await db_session.commit()

    settings = _make_test_settings(monkeypatch)
    fake_client = AsyncAnthropic(api_key="sk-test", base_url="http://localhost:99999")

    run_turn_called = False

    async def fake_run_turn(**kwargs: object) -> object:
        nonlocal run_turn_called
        run_turn_called = True
        return ""

    fire = await _build_fire(
        client=fake_client,
        sm=db_session_factory,
        settings=settings,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )

    with unittest.mock.patch("daimon.adapters.scheduler.main.run_turn", side_effect=fake_run_turn):
        await fire(row)

    assert not run_turn_called, "run_turn must NOT be called when created_by_user_id is None"

    async with db_session_factory() as s:
        fetched = await get_routine(s, row.id, tenant_id=tenant.id)
    assert fetched is not None, "routine row must still exist after bail"
    assert fetched.last_error == "routine has no created_by_user_id", (
        f"routine must record 'routine has no created_by_user_id'; got {fetched.last_error!r}"
    )

    await fake_client.close()


async def test_fire_resolves_principal_using_tenant_platform_not_hardcoded_discord(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A routine on a Slack tenant fires against a slack PlatformPrincipal.

    The fire path resolves created_by_user_id -> account via the routine's tenant
    platform. If it were still hardcoded to discord, a slack-created routine would
    mint a discord principal for the slack user id (wrong account/vault)."""
    now = datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)

    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_FIRE_SLACK")
    row = await create_routine(
        db_session,
        created_by_user_id="U_SLACK_FIRE",
        agent_id="agent_slack",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="trigger",
        next_fire_at=now - timedelta(minutes=1),
        tenant_id=tenant.id,
    )
    await db_session.commit()

    settings = _make_test_settings(monkeypatch)
    fake_client = AsyncAnthropic(api_key="sk-test", base_url="http://localhost:99999")

    async def fake_run_turn(**kwargs: object) -> object:
        return ""

    fire = await _build_fire(
        client=fake_client,
        sm=db_session_factory,
        settings=settings,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )

    with unittest.mock.patch("daimon.adapters.scheduler.main.run_turn", side_effect=fake_run_turn):
        await fire(row)

    async with db_session_factory() as s:
        slack_principal = await find_platform_principal(
            s, tenant_id=tenant.id, platform="slack", external_id="U_SLACK_FIRE"
        )
        discord_principal = await find_platform_principal(
            s, tenant_id=tenant.id, platform="discord", external_id="U_SLACK_FIRE"
        )

    assert slack_principal is not None and discord_principal is None, (
        "fire must resolve the principal on the tenant's platform (slack), "
        f"never hardcoded discord; got slack={slack_principal!r} discord={discord_principal!r}"
    )

    await fake_client.close()


async def test_fire_rejects_routine_when_tenant_balance_depleted(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_fire records balance_depleted and does not invoke run_turn when the
    tenant ledger balance is <= 0 (admission gate, Stripe-independent)."""
    now = datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)

    tenant = await make_tenant(db_session)
    # Seed a depleted ledger — insert a zero-balance entry so balance is 0.
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("0"),
        reason="trial_credit",
        idempotency_key=f"trial:{tenant.id}",
    )
    row = await create_routine(
        db_session,
        created_by_user_id="u1",
        agent_id="agent_x",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="trigger",
        next_fire_at=now - timedelta(minutes=1),
        tenant_id=tenant.id,
    )
    await db_session.commit()

    settings = _make_test_settings(monkeypatch)
    fake_client = AsyncAnthropic(api_key="sk-test", base_url="http://localhost:99999")

    run_turn_called = False

    async def fake_run_turn(**kwargs: object) -> object:
        nonlocal run_turn_called
        run_turn_called = True
        return ""

    fire = await _build_fire(
        client=fake_client,
        sm=db_session_factory,
        settings=settings,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )

    with unittest.mock.patch("daimon.adapters.scheduler.main.run_turn", side_effect=fake_run_turn):
        await fire(row)

    assert not run_turn_called, (
        "run_turn must NOT be called when tenant balance is depleted (balance_depleted gate)"
    )

    async with db_session_factory() as s:
        fetched = await get_routine(s, row.id, tenant_id=tenant.id)
    assert fetched is not None, "routine row must still exist after rejection"
    assert fetched.last_error == "balance_depleted", (
        f"routine must record 'balance_depleted' after balance gate rejection; "
        f"got {fetched.last_error!r}"
    )

    await fake_client.close()


@pytest.mark.parametrize("limit", [Decimal("0"), Decimal("5")], ids=["over", "under"])
async def test_fire_gates_on_and_attributes_to_the_routine_channel(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    limit: Decimal,
) -> None:
    """A routine's channel budget is checked after the balance gate, and a fire
    it admits bills that channel through both the live recorder and the stamp."""
    now = datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)
    tenant = await make_tenant(db_session)
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("10"))
    await make_channel_budget(db_session, tenant=tenant, channel_id="chan-9", limit_usd=limit)
    row = await create_routine(
        db_session,
        created_by_user_id="u3",
        agent_id="agent_z",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="trigger",
        next_fire_at=now - timedelta(minutes=1),
        tenant_id=tenant.id,
        channel_id="chan-9",
    )
    await db_session.commit()
    fake_client = AsyncAnthropic(api_key="sk-test", base_url="http://localhost:99999")
    calls: list[dict[str, object]] = []

    async def capturing_run_turn(**kwargs: object) -> str:
        calls.append(kwargs)
        return "tail"

    async def fake_resolve(*args: object, **kwargs: object) -> str:
        return "agent_z"

    fire = await _build_fire(
        client=fake_client,
        sm=db_session_factory,
        settings=_make_test_settings(monkeypatch),
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )
    with (
        unittest.mock.patch(
            "daimon.adapters.scheduler.main.run_turn", side_effect=capturing_run_turn
        ),
        unittest.mock.patch("daimon.adapters.scheduler.main.resolve_agent", fake_resolve),
        unittest.mock.patch("daimon.adapters.scheduler.main.resolve_environment", fake_resolve),
    ):
        await fire(row)
    await fake_client.close()

    async with db_session_factory() as s:
        fetched = await get_routine(s, row.id, tenant_id=tenant.id)
    assert fetched is not None
    if limit == 0:
        assert calls == [], "an exhausted channel budget must refuse the fire"
        assert fetched.last_error == "channel_budget_exceeded"
        return
    assert fetched.last_error is None
    assert calls[0]["budget_channel_id"] == "chan-9"
    factory = calls[0]["usage_record_factory"]
    assert callable(factory)
    partial = factory("sess_abc", "claude-opus-4-7")
    assert isinstance(partial, functools.partial)
    assert partial.keywords["channel_id"] == "chan-9"


async def test_fire_checks_the_agent_pin_before_the_channel_budget(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fire both pinned out and over its channel budget records the pin refusal:
    the budget gate runs last, after the pin is checked on the resolved agent."""
    from daimon.core.access_policy import TenantAccessPolicy
    from daimon.core.stores.access_policy import set_access_policy
    from daimon.testing import MARouter, build_fake_anthropic, ma_agent

    now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)
    tenant = await make_tenant(db_session)
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(agent_channel_pins={"Acme Display": ("111000111",)}),
    )
    await make_ledger_entry(db_session, tenant=tenant, delta_usd=Decimal("10"))
    await make_channel_budget(
        db_session, tenant=tenant, channel_id="999000999", limit_usd=Decimal("0")
    )
    row = await create_routine(
        db_session,
        created_by_user_id="U_PIN",
        agent_id="agent_dest",
        agent_name="acme-config",
        cron_expr="0 9 * * 1",
        timezone_="UTC",
        trigger_message="report",
        next_fire_at=now - timedelta(minutes=1),
        tenant_id=tenant.id,
        destination_kind="channel",
        destination_id="999000999",
        channel_id="999000999",
    )
    await db_session.commit()
    router = MARouter()
    router.add_agent(
        ma_agent(
            id="agent_dest",
            name="Acme Display",
            tenant_id=tenant.id,
            metadata={"daimon_name": "acme-config"},
        )
    )
    client = build_fake_anthropic(router.dispatch)
    fire = await _build_fire(
        client=client,
        sm=db_session_factory,
        settings=_make_test_settings(monkeypatch),
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )
    ran: list[object] = []

    async def fake_run_turn(**kwargs: object) -> object:
        ran.append(kwargs)
        return "done"

    async def fake_resolve(*args: object, **kwargs: object) -> str:
        return "agent_dest"

    with (
        unittest.mock.patch("daimon.adapters.scheduler.main.run_turn", side_effect=fake_run_turn),
        unittest.mock.patch(
            "daimon.adapters.scheduler.main.resolve_agent", side_effect=fake_resolve
        ),
        unittest.mock.patch(
            "daimon.adapters.scheduler.main.resolve_environment", side_effect=fake_resolve
        ),
    ):
        await fire(row)
    await client.close()
    async with db_session_factory() as s:
        after = await get_routine(s, row.id, tenant_id=tenant.id)
    assert after is not None
    assert after.last_error == "agent_pinned_elsewhere", (
        "the pin refusal must win over the channel budget refusal"
    )
    assert ran == [], "a refused fire must not run a turn"


async def test_fire_balance_gate_passes_threads_tenant_id_markup_pricing_into_usage_record_factory(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """usage_record_factory partial binds tenant_id, markup, and pricing so a
    successful scheduled turn writes a transactional ledger debit."""
    now = datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)

    tenant = await make_tenant(db_session)
    # Positive balance so the gate passes.
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("10.00"),
        reason="trial_credit",
        idempotency_key=f"trial:{tenant.id}",
    )
    row = await create_routine(
        db_session,
        created_by_user_id="u2",
        agent_id="agent_y",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="trigger",
        next_fire_at=now - timedelta(minutes=1),
        tenant_id=tenant.id,
    )
    await db_session.commit()

    settings = _make_test_settings(monkeypatch)
    fake_client = AsyncAnthropic(api_key="sk-test", base_url="http://localhost:99999")

    # Capture the usage_record_factory callable that _fire passes to run_turn.
    captured_factory: list[Callable[[str, str], object]] = []

    async def capturing_run_turn(
        *,
        usage_record_factory: Callable[[str, str], object],
        **kwargs: object,
    ) -> str:
        captured_factory.append(usage_record_factory)
        return "fake tail"

    fire = await _build_fire(
        client=fake_client,
        sm=db_session_factory,
        settings=settings,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )

    with unittest.mock.patch(
        "daimon.adapters.scheduler.main.run_turn", side_effect=capturing_run_turn
    ):
        # resolve_agent / resolve_environment will fail (fake client/no MA), so
        # we patch those too to keep the test focused on the debit-binding shape.
        async def fake_resolve_agent(*args: object, **kwargs: object) -> str:
            return "agent_y"

        async def fake_resolve_environment(*args: object, **kwargs: object) -> str:
            return "env_default"

        with (
            unittest.mock.patch(
                "daimon.adapters.scheduler.main.resolve_agent", side_effect=fake_resolve_agent
            ),
            unittest.mock.patch(
                "daimon.adapters.scheduler.main.resolve_environment",
                side_effect=fake_resolve_environment,
            ),
            unittest.mock.patch("daimon.adapters.scheduler.main.record_result"),
        ):
            await fire(row)

    assert len(captured_factory) == 1, (
        "run_turn must be called exactly once when balance is positive"
    )
    factory = captured_factory[0]

    # Call the factory with a representative model_id and inspect the partial.
    model_id = "claude-opus-4-7"
    partial = factory("sess_abc", model_id)
    assert isinstance(partial, functools.partial), (
        "usage_record_factory must return a functools.partial"
    )
    kw = partial.keywords
    assert kw.get("tenant_id") == tenant.id, (
        f"partial must bind tenant_id={tenant.id!r}; got {kw.get('tenant_id')!r}"
    )
    assert kw.get("markup") == settings.billing.markup, (
        f"partial must bind markup={settings.billing.markup!r}; got {kw.get('markup')!r}"
    )
    expected_pricing: ModelRates | None = MODEL_PRICING.get(model_id)
    assert kw.get("pricing") == expected_pricing, (
        f"partial must bind pricing=MODEL_PRICING.get({model_id!r}); got {kw.get('pricing')!r}"
    )
    assert partial.func is record_turn_usage, "partial must wrap record_turn_usage"

    await fake_client.close()


# ---------------------------------------------------------------------------
# RED tests — DeploymentDefault injection in scheduler
#
# This test imports DeploymentDefault which does not yet exist (Plan 03).
# It is RED until Plans 03 and 08 (scheduler main.py update) land.
# ---------------------------------------------------------------------------


async def test_fire_uses_deployment_default(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_fire with RoutineRow.agent_name=None resolves to DeploymentDefault.agent_name (R8).

    Verifies that the scheduler fire closure uses the injected DeploymentDefault
    instead of hardcoded 'daimon'/'default' string literals.
    RED until Plan 03 (DeploymentDefault) + Plan 08 (_build_fire signature update) land.
    """
    from daimon.core.scope import DeploymentDefault

    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)

    tenant = await make_tenant(db_session)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("10.00"),
        reason="trial_credit",
        idempotency_key=f"trial:{tenant.id}",
    )
    row = await create_routine(
        db_session,
        created_by_user_id="u1",
        agent_id="agent_x",
        agent_name="",  # intentionally empty — falsy, must resolve via deployment default
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="trigger",
        next_fire_at=now - timedelta(minutes=1),
        tenant_id=tenant.id,
    )
    await db_session.commit()

    deployment_default = DeploymentDefault(agent_name="x", environment_name="y")
    settings = _make_test_settings(monkeypatch)
    fake_client = AsyncAnthropic(api_key="sk-test", base_url="http://localhost:99999")

    captured_agent: list[str] = []
    captured_env: list[str] = []

    async def fake_resolve_agent(*args: object, daimon_tag: str, **kwargs: object) -> str:
        captured_agent.append(daimon_tag)
        return "resolved-agent"

    async def fake_resolve_environment(*args: object, daimon_tag: str, **kwargs: object) -> str:
        captured_env.append(daimon_tag)
        return "resolved-env"

    # After Plan 08, _build_fire accepts deployment_default= keyword arg.
    # RED until then: this call will fail with an unexpected kwarg.
    fire = await _build_fire(
        client=fake_client,
        sm=db_session_factory,
        settings=settings,
        deployment_default=deployment_default,
        resolver_cache=new_resolver_cache(),
    )

    with (
        unittest.mock.patch(
            "daimon.adapters.scheduler.main.resolve_agent", side_effect=fake_resolve_agent
        ),
        unittest.mock.patch(
            "daimon.adapters.scheduler.main.resolve_environment",
            side_effect=fake_resolve_environment,
        ),
        unittest.mock.patch("daimon.adapters.scheduler.main.run_turn", return_value="tail"),
        unittest.mock.patch("daimon.adapters.scheduler.main.record_result"),
    ):
        await fire(row)

    assert len(captured_agent) == 1, "resolve_agent must be called exactly once"
    assert captured_agent[0] == "x", (
        f"scheduler fire must pass deployment_default.agent_name='x' to resolve_agent; "
        f"got {captured_agent[0]!r}"
    )
    assert len(captured_env) == 1, "resolve_environment must be called exactly once"
    assert captured_env[0] == "y", (
        "with no channel or tenant environment, scheduler fire must pass "
        f"deployment_default.environment_name='y'; got {captured_env[0]!r}"
    )

    await fake_client.close()


@pytest.mark.parametrize(
    ("channel_env", "tenant_env", "expected"),
    [("gpu", "shared", "gpu"), (None, "shared", "shared")],
    ids=["channel", "tenant-default"],
)
async def test_fire_runs_in_the_routine_channels_environment(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    channel_env: str | None,
    tenant_env: str | None,
    expected: str,
) -> None:
    """A routine runs where a turn in its channel would: channel, then tenant default."""
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    tenant = await make_tenant(db_session)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("10.00"),
        reason="trial_credit",
        idempotency_key=f"trial:{tenant.id}",
    )
    if channel_env is not None:
        await set_fields(
            db_session,
            scope=ChannelScopeRef(tenant_id=tenant.id, channel_id="C_ENV"),
            tenant_id=tenant.id,
            environment_name=channel_env,
        )
    if tenant_env is not None:
        await set_fields(
            db_session,
            scope=TenantScopeRef(tenant_id=tenant.id),
            tenant_id=tenant.id,
            environment_name=tenant_env,
        )
    row = await create_routine(
        db_session,
        created_by_user_id="u1",
        agent_id="agent_x",
        agent_name="x",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="trigger",
        next_fire_at=now - timedelta(minutes=1),
        tenant_id=tenant.id,
        channel_id="C_ENV",
    )
    await db_session.commit()
    fake_client = AsyncAnthropic(api_key="sk-test", base_url="http://localhost:99999")
    captured_env: list[str] = []

    async def fake_resolve_environment(*args: object, daimon_tag: str, **kwargs: object) -> str:
        captured_env.append(daimon_tag)
        return "resolved-env"

    fire = await _build_fire(
        client=fake_client,
        sm=db_session_factory,
        settings=_make_test_settings(monkeypatch),
        deployment_default=DeploymentDefault(agent_name="x", environment_name="y"),
        resolver_cache=new_resolver_cache(),
    )
    with (
        unittest.mock.patch(
            "daimon.adapters.scheduler.main.resolve_agent", return_value="resolved-agent"
        ),
        unittest.mock.patch(
            "daimon.adapters.scheduler.main.resolve_environment",
            side_effect=fake_resolve_environment,
        ),
        unittest.mock.patch("daimon.adapters.scheduler.main.run_turn", return_value="tail"),
        unittest.mock.patch("daimon.adapters.scheduler.main.record_result"),
    ):
        await fire(row)

    assert captured_env == [expected], f"the routine must run in {expected!r}"
    await fake_client.close()


async def test_fire_closure_threads_public_url_from_settings(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_fire's apply_callable closure must thread public_url=str(settings.mcp.public_url)
    to reconcile_tenant_defaults — NOT None. Contrast with CLI/Discord callers which
    pass public_url=None. This ensures self-healed scheduler agents come up with
    daimon-mcp attached.

    Strategy: patch resolve_agent so it calls its apply_callable kwarg, which lets
    us observe what reconcile_tenant_defaults receives — specifically public_url.
    """
    now = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)

    tenant = await make_tenant(db_session)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("10.00"),
        reason="trial_credit",
        idempotency_key=f"trial:{tenant.id}",
    )
    row = await create_routine(
        db_session,
        created_by_user_id="u_pub",
        agent_id="agent_pub",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="trigger",
        next_fire_at=now - timedelta(minutes=1),
        tenant_id=tenant.id,
    )
    await db_session.commit()

    settings = _make_test_settings(monkeypatch)
    fake_client = AsyncAnthropic(api_key="sk-test", base_url="http://localhost:99999")

    # Capture what reconcile_tenant_defaults receives as public_url.
    captured_public_urls: list[str | None] = []

    async def fake_reconcile(
        client: object,
        session_factory: object,
        defaults_root: object,
        *,
        tenant_id: uuid.UUID,
        public_url: str | None = None,
    ) -> object:
        captured_public_urls.append(public_url)
        return object()

    # Patch resolve_agent so it invokes apply_callable (the closure under test)
    # then returns a stub id. This drives the reconcile_tenant_defaults call path
    # without making real Anthropic API calls.
    async def fake_resolve_agent(
        *args: object, apply_callable: Callable[[], object], **kwargs: object
    ) -> str:
        await apply_callable()  # type: ignore[misc]  # invoke closure to observe args
        return "ag_stub"

    async def fake_resolve_environment(*args: object, **kwargs: object) -> str:
        return "env_stub"

    fire = await _build_fire(
        client=fake_client,
        sm=db_session_factory,
        settings=settings,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )

    with (
        unittest.mock.patch(
            "daimon.adapters.scheduler.main.reconcile_tenant_defaults",
            side_effect=fake_reconcile,
        ),
        unittest.mock.patch(
            "daimon.adapters.scheduler.main.resolve_agent",
            side_effect=fake_resolve_agent,
        ),
        unittest.mock.patch(
            "daimon.adapters.scheduler.main.resolve_environment",
            side_effect=fake_resolve_environment,
        ),
        unittest.mock.patch("daimon.adapters.scheduler.main.run_turn", return_value="tail"),
        unittest.mock.patch("daimon.adapters.scheduler.main.record_result"),
    ):
        await fire(row)

    assert len(captured_public_urls) >= 1, (
        "reconcile_tenant_defaults must be called at least once via the apply_callable path"
    )
    expected_url = str(settings.mcp.public_url)
    for url in captured_public_urls:
        assert url == expected_url, (
            f"scheduler closure must thread public_url=str(settings.mcp.public_url)={expected_url!r}; "
            f"got {url!r}"
        )

    await fake_client.close()


async def test_sweep_wizard_sessions_swallows_sqlalchemy_error(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A SQLAlchemyError from the core sweep must not propagate — the wrapper's
    named boundary catch exists so a DB hiccup cannot kill the scheduler loop."""
    with unittest.mock.patch(
        "daimon.adapters.scheduler.main.sweep_expired_wizard_sessions",
        side_effect=SQLAlchemyError("boom"),
    ):
        await _sweep_wizard_sessions(db_session_factory)  # must not raise


async def test_sweep_wizard_sessions_forwards_sessionmaker_and_returns_cleanly(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Happy path: the wrapper forwards the injected sessionmaker to the core
    sweep and returns without raising."""
    captured_sm: list[async_sessionmaker[AsyncSession]] = []

    async def fake_sweep(
        sm: async_sessionmaker[AsyncSession], *, now: datetime, limit: int = 500
    ) -> int:
        captured_sm.append(sm)
        return 0

    with unittest.mock.patch(
        "daimon.adapters.scheduler.main.sweep_expired_wizard_sessions",
        side_effect=fake_sweep,
    ):
        await _sweep_wizard_sessions(db_session_factory)

    assert captured_sm == [db_session_factory], (
        "wrapper must forward the injected sessionmaker to the core sweep unchanged"
    )


async def test_sweep_slack_event_dedup_swallows_sqlalchemy_error(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A SQLAlchemyError from the core sweep must not propagate — the wrapper's
    named boundary catch exists so a DB hiccup cannot kill the scheduler loop."""
    with unittest.mock.patch(
        "daimon.adapters.scheduler.main.sweep_expired_slack_event_dedup",
        side_effect=SQLAlchemyError("boom"),
    ):
        await _sweep_slack_event_dedup(db_session_factory)  # must not raise


async def test_sweep_slack_event_dedup_forwards_sessionmaker_and_returns_cleanly(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Happy path: the wrapper forwards the injected sessionmaker to the core
    sweep and passes a timezone-aware `now`."""
    captured_sm: list[async_sessionmaker[AsyncSession]] = []
    captured_now: list[datetime] = []

    async def fake_sweep(
        sm: async_sessionmaker[AsyncSession], *, now: datetime, limit: int = 500
    ) -> int:
        captured_sm.append(sm)
        captured_now.append(now)
        return 0

    with unittest.mock.patch(
        "daimon.adapters.scheduler.main.sweep_expired_slack_event_dedup",
        side_effect=fake_sweep,
    ):
        await _sweep_slack_event_dedup(db_session_factory)

    assert captured_sm == [db_session_factory], (
        "wrapper must forward the injected sessionmaker to the core sweep unchanged"
    )
    assert captured_now[0].tzinfo is not None, (
        "wrapper must pass a timezone-aware `now` to the core sweep"
    )


async def test_sweep_retired_turn_card_intents_swallows_sqlalchemy_error(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    with unittest.mock.patch(
        "daimon.adapters.scheduler.main.sweep_retired_turn_card_intents",
        side_effect=SQLAlchemyError("boom"),
    ):
        await _sweep_retired_turn_card_intents(db_session_factory)


async def test_sweep_retired_turn_card_intents_forwards_sessionmaker_and_now(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    captured: list[tuple[async_sessionmaker[AsyncSession], datetime]] = []

    async def fake_sweep(sm: async_sessionmaker[AsyncSession], *, now: datetime) -> int:
        captured.append((sm, now))
        return 0

    with unittest.mock.patch(
        "daimon.adapters.scheduler.main.sweep_retired_turn_card_intents",
        side_effect=fake_sweep,
    ):
        await _sweep_retired_turn_card_intents(db_session_factory)

    assert captured[0][0] is db_session_factory
    assert captured[0][1].tzinfo is not None


@pytest.mark.parametrize(
    ("policy_sql", "expected_error"),
    [
        ('{"invoker_user_ids": ["staff"]}', "invoker_not_allowed"),
        ("null", "access_policy_unreadable"),
        ('{"agent_channel_pins": {"daimon": ["rx-chan"]}}', "agent_pinned_elsewhere"),
    ],
    ids=["creator-not-allowlisted", "unreadable-policy", "agent-pinned-elsewhere"],
)
async def test_fire_skips_routine_the_invoker_policy_refuses(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    policy_sql: str,
    expected_error: str,
) -> None:
    """SYS-047: a routine fires as its creator, so taking the creator off the
    allowlist stops it; a policy that can't be read stops it too."""
    now = datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)
    tenant = await make_tenant(db_session)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("10"),
        reason="trial_credit",
        idempotency_key=f"trial:{tenant.id}",
    )
    await db_session.execute(
        text(
            "INSERT INTO tenant_access_policies (tenant_id, policy) VALUES (:t, CAST(:p AS jsonb))"
        ),
        {"t": tenant.id, "p": policy_sql},
    )
    row = await create_routine(
        db_session,
        created_by_user_id="u1",
        agent_id="agent_x",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="trigger",
        next_fire_at=now - timedelta(minutes=1),
        tenant_id=tenant.id,
    )
    await db_session.commit()

    fake_client = AsyncAnthropic(api_key="sk-test", base_url="http://localhost:99999")
    fire = await _build_fire(
        client=fake_client,
        sm=db_session_factory,
        settings=_make_test_settings(monkeypatch),
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )
    with unittest.mock.patch(
        "daimon.adapters.scheduler.main.run_turn",
        side_effect=AssertionError("a refused routine must not run a turn"),
    ):
        await fire(row)

    async with db_session_factory() as s:
        fetched = await get_routine(s, row.id, tenant_id=tenant.id)
    assert fetched is not None, "routine row must still exist after the skip"
    assert fetched.last_error == expected_error, f"got {fetched.last_error!r}"
    await fake_client.close()


# --- FEAT-085: routine destination and fallback post -----------------------


async def _fire_with_fake_turn(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    *,
    destination: tuple[str, str] | None,
    agent_posts_to: str | None,
    policy: object | None = None,
) -> tuple[RoutineRow, dict[str, object]]:
    from daimon.core.turn.state import ToolUseBlock, TurnState

    now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)
    tenant = await make_tenant(db_session)
    if policy is not None:
        from daimon.core.stores.access_policy import set_access_policy

        await set_access_policy(db_session, tenant_id=tenant.id, policy=policy)  # type: ignore[arg-type]
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("10.00"),
        reason="trial_credit",
        idempotency_key=f"trial:{tenant.id}",
    )
    row = await create_routine(
        db_session,
        created_by_user_id="U_DEST",
        agent_id="agent_dest",
        agent_name="daimon",
        cron_expr="0 9 * * 1",
        timezone_="Europe/Lisbon",
        trigger_message="summarize the week",
        next_fire_at=now - timedelta(minutes=1),
        tenant_id=tenant.id,
        destination_kind=destination[0] if destination else None,
        destination_id=destination[1] if destination else None,
    )
    await db_session.commit()
    seen: dict[str, object] = {}

    async def fake_run_turn(**kwargs: object) -> object:
        seen.update(kwargs)
        blocks: list[ToolUseBlock] = []
        if agent_posts_to is not None:
            blocks.append(
                ToolUseBlock(
                    kind="tool_use",
                    id="tu_post",
                    type="agent.mcp_tool_use",
                    name="send_message",
                    input={"channel_id": agent_posts_to, "content": "done"},
                    mcp_server_name="daimon-mcp",
                    status="complete",
                )
            )
        on_state = kwargs["on_state"]
        assert callable(on_state)
        on_state(TurnState(content=list(blocks)))
        return "Weekly summary: all green."

    fire = await _build_fire(
        client=AsyncAnthropic(api_key="sk-test", base_url="http://localhost:99999"),
        sm=db_session_factory,
        settings=_make_test_settings(monkeypatch),
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )

    async def fake_resolve(*args: object, **kwargs: object) -> str:
        return "agent_dest"

    with (
        unittest.mock.patch("daimon.adapters.scheduler.main.run_turn", side_effect=fake_run_turn),
        unittest.mock.patch(
            "daimon.adapters.scheduler.main.resolve_agent", side_effect=fake_resolve
        ),
        unittest.mock.patch(
            "daimon.adapters.scheduler.main.resolve_environment", side_effect=fake_resolve
        ),
    ):
        await fire(row)
    async with db_session_factory() as s:
        after = await get_routine(s, row.id, tenant_id=tenant.id)
    assert after is not None
    return after, seen


async def test_fire_without_destination_is_unchanged(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    after, seen = await _fire_with_fake_turn(
        db_session, db_session_factory, monkeypatch, destination=None, agent_posts_to=None
    )

    assert seen["trigger_message"] == "summarize the week", "no controls without a destination"
    assert after.last_result_tail == "Weekly summary: all green."
    assert after.delivery_status is None, "nothing queued without a destination"


async def test_fire_with_destination_queues_the_tail_when_the_agent_did_not_post(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    after, seen = await _fire_with_fake_turn(
        db_session,
        db_session_factory,
        monkeypatch,
        destination=("channel", "111222333"),
        agent_posts_to=None,
    )

    trigger = str(seen["trigger_message"])
    assert trigger.startswith("<turn_controls>\n")
    assert '"channel_id": "111222333"' in trigger
    assert '"schedule": "0 9 * * 1"' in trigger
    assert trigger.endswith("</turn_controls>\nsummarize the week")
    assert after.delivery_status == "pending"


async def test_fire_with_destination_skips_the_fallback_when_the_agent_posted(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    after, _seen = await _fire_with_fake_turn(
        db_session,
        db_session_factory,
        monkeypatch,
        destination=("channel", "111222333"),
        agent_posts_to="111222333",
    )

    assert (after.delivery_status, after.delivery_note) == ("skipped", "agent_posted")


async def test_fire_posting_elsewhere_still_queues_the_fallback(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    after, _seen = await _fire_with_fake_turn(
        db_session,
        db_session_factory,
        monkeypatch,
        destination=("channel", "111222333"),
        agent_posts_to="999",
    )

    assert after.delivery_status == "pending"


async def test_fire_does_not_invite_a_post_into_a_destination_protected_since(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Eval regression: the controls must not tell the agent to post into a
    channel the access policy now protects (send_message has no guard yet)."""
    from daimon.core.access_policy import TenantAccessPolicy

    after, seen = await _fire_with_fake_turn(
        db_session,
        db_session_factory,
        monkeypatch,
        destination=("channel", "111222333"),
        agent_posts_to=None,
        policy=TenantAccessPolicy(protected_channel_ids=("111222333",)),
    )

    trigger = str(seen["trigger_message"])
    assert "do not post there" in trigger
    assert "posts the end of your final reply there" not in trigger
    assert after.delivery_status == "pending", "the poster still routes it (DM fallback)"


@pytest.mark.parametrize(
    ("kind", "policy_kwargs"),
    [
        # A thread under a channel protected since: the scheduler cannot see
        # the thread's parent, so it must not invite a direct post.
        ("thread", {"protected_channel_ids": ("444",)}),
        ("thread", {"protected_category_ids": ("77",)}),
        # A channel in a category protected since.
        ("channel", {"protected_category_ids": ("77",)}),
    ],
)
async def test_fire_does_not_invite_a_post_when_placement_cannot_be_checked(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    policy_kwargs: dict[str, tuple[str, ...]],
) -> None:
    """Review regression (round 2): fire-time protection only checked the id,
    so a thread under a protected parent/category was still offered."""
    from daimon.core.access_policy import TenantAccessPolicy

    after, seen = await _fire_with_fake_turn(
        db_session,
        db_session_factory,
        monkeypatch,
        destination=(kind, "111222333"),
        agent_posts_to=None,
        policy=TenantAccessPolicy(**policy_kwargs),
    )

    trigger = str(seen["trigger_message"])
    assert "Do not post to the destination yourself" in trigger
    assert "posts the end of your final reply there" not in trigger
    assert after.delivery_status == "pending", "the poster resolves placement and delivers"


async def test_fire_still_invites_a_post_when_nothing_relevant_is_protected(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from daimon.core.access_policy import TenantAccessPolicy

    _after, seen = await _fire_with_fake_turn(
        db_session,
        db_session_factory,
        monkeypatch,
        destination=("channel", "111222333"),
        agent_posts_to=None,
        policy=TenantAccessPolicy(protected_channel_ids=("999",)),
    )

    assert "posts the end of your final reply there" in str(seen["trigger_message"])


async def test_settle_promo_credit_grants_a_due_timed_code(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The tick step grants timed credit whose window opened since the last tick."""
    start = datetime.now(UTC) - timedelta(minutes=5)
    terms = build_promo_code_terms(
        amount_usd=Decimal("10"),
        timed=True,
        credit_starts_at=start,
        credit_ends_at=start + timedelta(days=1),
    )
    async with db_session_factory.begin() as s:
        tenant = await make_tenant(s)
        code = await promo_store.insert_promo_code(s, code_hash="h", terms=terms)
        assert code is not None, "the promo code should be inserted"
        await promo_store.insert_redemption(
            s,
            promo_code_id=code.id,
            tenant_id=tenant.id,
            account_id=None,
            now=start - timedelta(hours=1),
            granted=False,
        )
    await _settle_promo_credit(db_session_factory)
    async with db_session_factory() as s:
        assert await tenant_ledger.get_balance(s, tenant_id=tenant.id) == Decimal("10"), (
            "the due timed credit should be granted"
        )


async def test_settle_promo_credit_swallows_sqlalchemy_error(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A database error in promo settlement is logged, not raised into the tick."""
    with unittest.mock.patch(
        "daimon.adapters.scheduler.main.settle_promo_credit",
        side_effect=SQLAlchemyError("boom"),
    ):
        await _settle_promo_credit(db_session_factory)  # must not raise


async def test_fire_skips_a_routine_that_would_post_across_an_isolated_channels_line(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Routing may change after a routine is saved: an outside agent's routine
    posting into a channel isolated since then is skipped, not run."""
    from daimon.core.access_policy import TenantAccessPolicy
    from daimon.core.stores.access_policy import set_access_policy
    from daimon.testing import ma_agent

    now = datetime(2026, 5, 30, 12, 0, 0, tzinfo=UTC)
    tenant = await make_tenant(db_session)
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("10"),
        reason="trial_credit",
        idempotency_key=f"trial:{tenant.id}",
    )
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(
            sealed_channel_ids=("room",),
            isolated_channel_ids=("room",),
            agent_channel_pins={"local": ("room",)},
        ),
    )
    row = await create_routine(
        db_session,
        created_by_user_id="u1",
        agent_id="agent_x",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="trigger",
        next_fire_at=now - timedelta(minutes=1),
        tenant_id=tenant.id,
        destination_kind="channel",
        destination_id="room",
        channel_id="room",
    )
    await db_session.commit()

    client = AsyncAnthropic(api_key="sk-test", base_url="http://localhost:99999")
    fire = await _build_fire(
        client=client,
        sm=db_session_factory,
        settings=_make_test_settings(monkeypatch),
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )

    async def fake_resolve(*args: object, **kwargs: object) -> str:
        return "agent_x"

    agent = ma_agent(id="agent_x", name="daimon", tenant_id=tenant.id)
    with (
        unittest.mock.patch(
            "daimon.adapters.scheduler.main.run_turn",
            side_effect=AssertionError("a routine crossing the line must not run a turn"),
        ),
        unittest.mock.patch(
            "daimon.adapters.scheduler.main.resolve_agent", side_effect=fake_resolve
        ),
        unittest.mock.patch(
            "daimon.adapters.scheduler.main.resolve_environment", side_effect=fake_resolve
        ),
        unittest.mock.patch.object(
            client.beta.agents, "retrieve", new=unittest.mock.AsyncMock(return_value=agent)
        ),
    ):
        await fire(row)

    async with db_session_factory() as s:
        fetched = await get_routine(s, row.id, tenant_id=tenant.id)
    assert fetched is not None and fetched.last_error == "channel_isolated"
    await client.close()


@pytest.mark.parametrize(
    ("destination", "pinned_out"),
    [(None, True), ("999000999", True), ("111000111", False)],
    ids=["no-destination", "other-channel", "pinned-channel"],
)
async def test_fire_checks_the_pin_on_the_agent_that_will_run_by_its_display_name(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    destination: str | None,
    pinned_out: bool,
) -> None:
    """The routine saved its config name; the pin is on the agent's display name.
    The fire must check the resolved agent's names, not the saved name alone."""
    from daimon.core.access_policy import TenantAccessPolicy
    from daimon.core.stores.access_policy import set_access_policy
    from daimon.testing import ma_agent

    now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=UTC)
    tenant = await make_tenant(db_session)
    await set_access_policy(
        db_session,
        tenant_id=tenant.id,
        policy=TenantAccessPolicy(agent_channel_pins={"Acme Display": ("111000111",)}),
    )
    await tenant_ledger.insert_entry(
        db_session,
        tenant_id=tenant.id,
        delta_usd=Decimal("10.00"),
        reason="trial_credit",
        idempotency_key=f"trial:{tenant.id}",
    )
    row = await create_routine(
        db_session,
        created_by_user_id="U_PIN",
        agent_id="agent_dest",
        agent_name="acme-config",
        cron_expr="0 9 * * 1",
        timezone_="UTC",
        trigger_message="report",
        next_fire_at=now - timedelta(minutes=1),
        tenant_id=tenant.id,
        destination_kind="channel" if destination else None,
        destination_id=destination,
    )
    await db_session.commit()
    client = AsyncAnthropic(api_key="sk-test", base_url="http://localhost:99999")
    fire = await _build_fire(
        client=client,
        sm=db_session_factory,
        settings=_make_test_settings(monkeypatch),
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )
    ran: list[object] = []

    async def fake_run_turn(**kwargs: object) -> object:
        ran.append(kwargs)
        return "done"

    async def fake_resolve(*args: object, **kwargs: object) -> str:
        return "agent_dest"

    agent = ma_agent(
        id="agent_dest",
        name="Acme Display",
        tenant_id=tenant.id,
        metadata={"daimon_name": "acme-config"},
    )
    with (
        unittest.mock.patch("daimon.adapters.scheduler.main.run_turn", side_effect=fake_run_turn),
        unittest.mock.patch(
            "daimon.adapters.scheduler.main.resolve_agent", side_effect=fake_resolve
        ),
        unittest.mock.patch(
            "daimon.adapters.scheduler.main.resolve_environment", side_effect=fake_resolve
        ),
        unittest.mock.patch.object(
            client.beta.agents, "retrieve", new=unittest.mock.AsyncMock(return_value=agent)
        ),
    ):
        await fire(row)
    async with db_session_factory() as s:
        after = await get_routine(s, row.id, tenant_id=tenant.id)
    assert after is not None
    if pinned_out:
        assert after.last_error == "agent_pinned_elsewhere" and ran == []
    else:
        assert after.last_error is None and len(ran) == 1
    await client.close()
