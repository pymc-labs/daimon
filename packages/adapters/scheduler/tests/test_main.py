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
from anthropic.types.beta.sessions.beta_managed_agents_span_model_usage import (
    BetaManagedAgentsSpanModelUsage,
)
from daimon.adapters.scheduler.main import (
    _build_fire,  # pyright: ignore[reportPrivateUsage]  # test seam for balance gate + debit binding
    _CapsAdapter,  # pyright: ignore[reportPrivateUsage]  # named test seam for cap wiring
    _validate_mcp_settings,  # pyright: ignore[reportPrivateUsage]  # boot-validation seam
)
from daimon.core._models import PlatformPrincipal
from daimon.core.billing import BillingConfig
from daimon.core.config import Settings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.pricing import MODEL_PRICING, ModelRates
from daimon.core.scheduler import run_one_tick
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import tenant_ledger, tenant_user_caps, usage_events
from daimon.core.stores.domain import RoutineRow
from daimon.core.stores.routines import create_routine, get_routine
from daimon.core.usage_recording import record_turn_usage
from daimon.testing.factories import make_tenant
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
        model_usage=BetaManagedAgentsSpanModelUsage(
            input_tokens=10_000_000,
            output_tokens=10_000_000,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
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
        model_usage=BetaManagedAgentsSpanModelUsage(
            input_tokens=10_000_000,
            output_tokens=10_000_000,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
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
        principals = (
            (
                await s.execute(
                    select(PlatformPrincipal).where(PlatformPrincipal.external_id == "U_SLACK_FIRE")
                )
            )
            .scalars()
            .all()
        )

    platforms = {p.platform for p in principals}
    assert platforms == {"slack"}, (
        "fire must resolve the principal on the tenant's platform (slack), "
        f"never hardcoded discord; got platforms={platforms}"
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
    from daimon.core.scope import DeploymentDefault  # noqa: PLC0415

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
        f"scheduler fire must pass deployment_default.environment_name='y' to resolve_environment; "
        f"got {captured_env[0]!r}"
    )

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
