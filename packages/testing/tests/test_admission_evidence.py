"""Real host gates and Discord refusal dispatch; no provider/model execution."""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import asdict, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import discord
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.discord.bot import DISCORD_REFUSAL_NOUNS, DaimonBot
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.config import McpSettings
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.turn.admission import AdmissionDecision, AdmissionObserver, admit
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.errors import AdmissionDenied, MissingTurnConfigError
from daimon.core.turn.notices import admission_refusal_text
from daimon.core.turn_hold import RequesterOrg
from daimon.testing.admission_evidence import AdmissionEffects, admission_edge
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.discord_surface import OfflineDiscordGateway
from daimon.testing.factories import (
    make_channel_budget,
    make_ledger_entry,
    make_platform_principal,
    make_tenant,
    make_tenant_config,
    make_tenant_user_cap,
)
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.outcome_oracle import LogObservation, VisibleText
from daimon.testing.turn_fakes import deps_for
from mux.contracts.ids import Provider, Scope
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from structlog.testing import capture_logs
from structlog.typing import EventDict


class Clock:
    now = 100.0

    def __call__(self) -> float:
        return self.now


def owned(factory: async_sessionmaker[AsyncSession]) -> None:
    bind = factory.kw["bind"]
    engine = getattr(bind, "engine", bind)
    if engine.url.database == "daimon_test" and not (
        os.environ.get("CI") == "true" and os.environ.get("GITHUB_ACTIONS") == "true"
    ):
        pytest.skip("Admission hooks require an owned lane DB; plain local daimon_test is refused")
    require_owned_sessionmaker(factory)


async def setup(
    session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    root: Path,
    gate: str,
) -> tuple[TurnDeps, Scope, AsyncMock]:
    owned(factory)
    tenant = await make_tenant(session)
    principal = await make_platform_principal(
        session, tenant=tenant, platform="discord", external_id="103"
    )
    await make_tenant_config(
        session, tenant=tenant, agent_name="daimon", environment_name="default"
    )
    if gate != "balance_depleted":
        await make_ledger_entry(session, tenant=tenant, delta_usd=Decimal("10"))
    if gate == "writers_none":
        await set_access_policy(
            session,
            tenant_id=tenant.id,
            policy=TenantAccessPolicy.model_validate({"protected_channel_ids": ("101",)}),
        )
    if gate == "cap_exceeded":
        await make_tenant_user_cap(session, tenant=tenant, amount=Decimal("0"))
    if gate == "channel_budget_exceeded":
        # Controlled host fixture, NOT NEW15's first actual metered native turn.
        await make_channel_budget(session, tenant=tenant, channel_id="101", limit_usd=Decimal("0"))
    await session.commit()
    client = AsyncMock(spec=AsyncAnthropic)
    cache = new_resolver_cache()
    cache[(tenant.id, "agent", "daimon")] = "ag_1"
    cache[(tenant.id, "environment", "default")] = "env_1"
    # Use the production resolver's real warm resource cache. No SDK client is
    # constructed and no beta method is mocked. Default cold-request equivalence
    # is independently covered by the immutable integration goldens.
    cache.agents[(tenant.id, "ag_1")] = ma_agent(id="ag_1", name="daimon", tenant_id=tenant.id)
    cache.environments[(tenant.id, "env_1")] = ma_environment(
        id="env_1", name="default", tenant_id=tenant.id
    )
    deps = TurnDeps(
        anthropic=client,
        sessionmaker=factory,
        deployment_default=DeploymentDefault(),
        resolver_cache=cache,
        defaults_root=root,
        mcp=McpSettings(),
        billing_config=None,
        markup=Decimal("1"),
        fernet=None,
        github_fallback_pat=None,
        github_app_id=None,
        github_app_private_key=None,
        public_url=None,
    )
    scope = Scope(
        tenant_id=str(tenant.id),
        account_id=str(principal.account_id),
        principal_id=str(principal.id),
        authorization_id="owned-fixture-row",
    )
    return deps, scope, client


def native_count(client: AsyncMock) -> int:
    # Independently inspect ALL client calls. Only existing metadata reads are
    # excluded. Any additional operation is counted, not silently classified zero.
    return sum(
        call[0] not in ("beta.agents.retrieve", "beta.environments.retrieve")
        for call in client.mock_calls
    )


def gateway_message(clock: Clock) -> tuple[discord.Client, discord.Message, OfflineDiscordGateway]:
    client = discord.Client(intents=discord.Intents.none())
    state = client._connection  # pyright: ignore[reportPrivateUsage]
    guild = discord.Guild(
        state=state,
        data=cast(Any, {"id": "100", "name": "QA", "roles": [], "emojis": [], "stickers": []}),
    )
    parent = discord.TextChannel(
        state=state,
        guild=guild,
        data=cast(
            Any, {"id": "101", "name": "QA", "type": 0, "position": 0, "permission_overwrites": []}
        ),
    )
    thread = discord.Thread(
        state=state,
        guild=guild,
        data=cast(
            Any,
            {
                "id": "102",
                "parent_id": "101",
                "name": "QA",
                "type": 11,
                "owner_id": "103",
                "message_count": 0,
                "member_count": 0,
                "thread_metadata": {
                    "archived": False,
                    "auto_archive_duration": 1440,
                    "archive_timestamp": "2026-10-10T00:00:00+00:00",
                    "locked": False,
                },
            },
        ),
    )
    state._add_guild(guild)  # pyright: ignore[reportPrivateUsage]
    guild._add_channel(parent)  # pyright: ignore[reportPrivateUsage]
    gateway = OfflineDiscordGateway(evidence_id="attempt", channel_id=102, clock=clock)
    state.http = cast(Any, gateway)
    message = discord.Message(
        state=state,
        channel=thread,
        data=cast(
            Any,
            {
                "id": "1400000000000000000",
                "channel_id": "102",
                "content": "<@999> hello",
                "author": {"id": "103", "username": "caller", "discriminator": "0", "avatar": None},
                "attachments": [],
                "embeds": [],
                "mentions": [],
                "mention_roles": [],
                "timestamp": "2026-10-10T00:00:00+00:00",
                "edited_timestamp": None,
                "type": 0,
                "pinned": False,
                "mention_everyone": False,
                "tts": False,
            },
        ),
    )
    return client, message, gateway


@pytest.mark.parametrize("backend", ["anthropic", "openai", "gemini"])
@pytest.mark.parametrize(
    "gate,event_name,notice",
    [
        ("writers_none", "turn.skipped.writers_none", None),
        # Main a5097ab195a3d94d999f606e216b7d25120abaec: approved credit notice (A/O/G).
        (
            "balance_depleted",
            "turn.skipped.over_balance",
            admission_refusal_text("balance_depleted", DISCORD_REFUSAL_NOUNS),
        ),
        # Main a5097ab195a3d94d999f606e216b7d25120abaec: approved monthly-limit notice (A/O/G).
        (
            "cap_exceeded",
            "turn.skipped.over_cap",
            admission_refusal_text("cap_exceeded", DISCORD_REFUSAL_NOUNS),
        ),
        ("channel_budget_exceeded", "turn.skipped.over_channel_budget", "budget"),
    ],
)
async def test_real_gate_and_adapter_denial_have_no_native_sends_or_provider_root(
    db_preflight,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: Provider,
    gate: str,
    event_name: str,
    notice: str | None,
) -> None:
    deps, scope, native = await setup(db_session, db_session_factory, tmp_path, gate)

    # Pin only discord.py's nonce source for the paired byte comparison. Leave
    # all other modules' randomness, including database schema ownership, alone.
    def nonce(_bits: int) -> int:
        return 123

    monkeypatch.setattr("discord.abc.secrets", SimpleNamespace(randbits=nonce))
    clock = Clock()
    client, message, gateway = gateway_message(clock)
    bot = cast(
        DaimonBot,
        SimpleNamespace(
            user=object(),
            # Merged #839 (7fa0ddf392481c6067f66f0128ba8bb02af3e411): constructor default.
            _ingress_clock=None,
            runtime=SimpleNamespace(
                turn_deps=deps,
                sessionmaker=deps.sessionmaker,
                settings=SimpleNamespace(discord=None),
            ),
            _wait_for_orphan_recovery=AsyncMock(),
            # The default protected-channel signal is a platform reaction, never a native send.
            _signal_protected_channel=AsyncMock(),
        ),
    )

    bot._orchestrate_observed_guarded = MethodType(DaimonBot._orchestrate_observed_guarded, bot)  # pyright: ignore[reportPrivateUsage]

    async def invoke(observer: AdmissionObserver) -> None:
        async def observed(*args: Any, **kwargs: Any) -> object:
            return await admit(*args, **kwargs, observer=observer)

        monkeypatch.setattr("daimon.adapters.discord.bot.admit", observed)
        clock.now += 1.0
        await DaimonBot._orchestrate_observed(bot, message, "100", uuid.UUID(scope.tenant_id))  # pyright: ignore[reportPrivateUsage]
        clock.now += 1.0

    def stamp_log(_logger: object, _method: str, entry: EventDict) -> EventDict:
        entry["captured_s"] = clock()
        return entry

    with capture_logs(processors=[stamp_log]) as logs:

        def effects() -> AdmissionEffects:
            return AdmissionEffects(
                notices=tuple(
                    VisibleText(
                        evidence_id=entry.evidence_id,
                        message_id=entry.message.message_id,
                        text=entry.message.content,
                        order=i,
                        observed_s=entry.message.observed_s,
                    )
                    for i, entry in enumerate(gateway.events)
                ),
                logs=tuple(
                    LogObservation(
                        evidence_id=f"log:{i}",
                        event=entry["event"],
                        fields={
                            key: value
                            for key, value in entry.items()
                            if key not in ("event", "log_level", "captured_s")
                        },
                        order=i,
                        observed_s=entry["captured_s"],
                    )
                    for i, entry in enumerate(logs)
                ),
                complete=True,
            )

        receipt = await admission_edge(
            deps=deps,
            scope=scope,
            backend=backend,
            platform="discord",
            channel_id="101",
            thread_id="102",
            external_user_id="103",
            evidence_id="attempt",
            invoke=invoke,
            effects=effects,
            native_send_count=lambda: native_count(native),
            clock=clock,
        )
    observed_requests = tuple(entry.request_json for entry in gateway.events)
    gateway.events.clear()
    monkeypatch.setattr("daimon.adapters.discord.bot.admit", admit)
    with capture_logs(processors=[stamp_log]) as baseline_logs:
        await DaimonBot._orchestrate_observed(bot, message, "100", uuid.UUID(scope.tenant_id))  # pyright: ignore[reportPrivateUsage]
    assert tuple(entry.request_json for entry in gateway.events) == observed_requests
    assert [
        (
            entry["event"],
            {k: v for k, v in entry.items() if k not in ("event", "log_level", "captured_s")},
        )
        for entry in baseline_logs
    ] == [(entry.event, entry.fields) for entry in receipt.effects.logs]
    await client.close()
    assert receipt.decision is not None and receipt.decision.reason == gate
    assert not receipt.decision.admitted
    assert receipt.native_sends == 0
    assert receipt.pending == ()
    assert event_name in [entry.event for entry in receipt.effects.logs]
    assert receipt.decision_s == 101.0 and receipt.observed_s == 102.0
    assert "session_id" not in asdict(receipt) and "root_turn_id" not in asdict(receipt)
    if notice is None:
        assert receipt.effects.notices == () and receipt.effects.reactions == ()
        assert native.mock_calls == [], "writers-none runs before metadata reads"
    else:
        assert len(receipt.effects.notices) == 1
        if gate in ("balance_depleted", "cap_exceeded"):
            assert receipt.effects.notices[0].text == notice
        else:
            assert notice in receipt.effects.notices[0].text
        assert "an operator" not in receipt.effects.notices[0].text
        assert native.mock_calls == [], "warm metadata cache needs no SDK dispatch"


@pytest.mark.parametrize(
    "gate",
    ["allowed", "writers_none", "balance_depleted", "cap_exceeded", "channel_budget_exceeded"],
)
@pytest.mark.parametrize("installed", [False, True, "raises"])
async def test_observer_preserves_real_acceptance_denial_and_warm_dispatch(
    db_preflight,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate: str,
    installed: bool | str,
) -> None:
    deps, scope, native = await setup(db_session, db_session_factory, tmp_path, gate)
    decisions: list[AdmissionDecision] = []

    if not installed:

        def unused_decision(*args: object, **kwargs: object) -> None:
            pytest.fail("The default path must not construct observer receipts")

        monkeypatch.setattr("daimon.core.turn.admission.AdmissionDecision", unused_decision)

    def observer(decision: AdmissionDecision) -> None:
        decisions.append(decision)
        if installed == "raises":
            raise RuntimeError("observer failure must not change admission")

    kwargs = dict(
        tenant_id=uuid.UUID(scope.tenant_id),
        requester_org=RequesterOrg.HOST,
        platform="discord",
        external_user_id="103",
        channel_id="101",
        now=datetime.now(UTC),
    )
    if gate == "allowed":
        result = await admit(deps, **kwargs, observer=observer if installed else None)  # pyright: ignore[reportArgumentType]
        assert str(result.account_id) == scope.account_id
        assert decisions[0].admitted if installed else decisions == []
    else:
        with pytest.raises(AdmissionDenied) as error:
            await admit(deps, **kwargs, observer=observer if installed else None)  # pyright: ignore[reportArgumentType]
        assert error.value.reason == gate
        assert decisions[0].reason == gate if installed else decisions == []
    assert native_count(native) == 0
    if gate == "writers_none":
        assert native.mock_calls == []
    else:
        assert native.mock_calls == [], "the warm default and observed paths dispatch identically"


@pytest.mark.parametrize(
    "error",
    [
        MissingTurnConfigError(
            missing=("agent",), agent_name_tier=None, environment_name_tier=None
        ),
        RuntimeError("fault"),
        asyncio.CancelledError(),
    ],
)
async def test_non_decision_errors_do_not_create_an_admission_receipt(
    db_preflight,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
) -> None:
    deps, scope, _ = await setup(db_session, db_session_factory, tmp_path, "allowed")
    monkeypatch.setattr("daimon.core.turn.admission.admit_impl", AsyncMock(side_effect=error))
    observer = AsyncMock()
    with pytest.raises(type(error)) as raised:
        await admit(
            deps,
            tenant_id=uuid.UUID(scope.tenant_id),
            requester_org=RequesterOrg.HOST,
            platform="discord",
            external_user_id="103",
            channel_id="101",
            now=datetime.now(UTC),
            observer=observer,
        )
    assert raised.value is error
    observer.assert_not_called()


async def test_foreign_routed_bind_is_refused_before_connection_or_callback() -> None:
    owned_engine = create_async_engine("postgresql+asyncpg://localhost/daimon_test_nc_N5")
    foreign_engine = create_async_engine("postgresql+asyncpg://localhost/operator_test")
    attempts: list[object] = []
    for engine in (owned_engine, foreign_engine):
        event.listen(engine.sync_engine, "do_connect", lambda *args, **kwargs: attempts.append(1))
    deps = replace(
        deps_for(None, async_sessionmaker(owned_engine, binds={AsyncSession: foreign_engine})),
        turn_path=None,
    )
    invoke = AsyncMock()
    try:
        with pytest.raises(ValueError, match="owned local"):
            await admission_edge(
                deps=deps,
                scope=Scope(
                    tenant_id="t", account_id="a", principal_id="p", authorization_id="auth"
                ),
                backend="openai",
                platform="discord",
                channel_id="101",
                external_user_id="103",
                evidence_id="attempt",
                invoke=invoke,
                effects=lambda: AdmissionEffects(),
                native_send_count=lambda: pytest.fail("guard must run first"),
            )
        invoke.assert_not_called()
        assert attempts == []
    finally:
        await owned_engine.dispose()
        await foreign_engine.dispose()


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "partial",
        "positive_send",
        "counter_unknown",
        "counter_bool",
        "counter_negative",
        "counter_backwards",
        "clock_backwards",
        "clock_nan",
        "duplicate",
        "foreign_tenant",
        "foreign_account",
        "foreign_channel",
        "foreign_thread",
        "outside_effect",
        "platform_scope",
        "legacy_scope",
        "invoke_error",
    ],
)
async def test_capture_gaps_and_invalid_evidence_never_become_certification(case: str) -> None:
    engine = create_async_engine("postgresql+asyncpg://localhost/daimon_test_nc_N5")
    tenant, account = uuid.uuid4(), uuid.uuid4()
    deps = replace(deps_for(None, async_sessionmaker(engine)), turn_path=None)
    scope = Scope(
        tenant_id=str(tenant), account_id=str(account), principal_id="p", authorization_id="auth"
    )
    if case == "platform_scope":
        scope = Scope.platform(reason="not a tenant authorization")
    if case == "legacy_scope":
        scope = Scope.legacy_host_authorized(call_site="not a tenant authorization")
    clock = Clock()
    calls = 0

    def count() -> int | None:
        nonlocal calls
        calls += 1
        if case in ("missing", "counter_unknown"):
            return None
        if case == "counter_bool":
            return True
        if case == "counter_negative":
            return -1
        if case == "counter_backwards":
            return 2 - calls
        return int(case == "positive_send" and calls == 2)

    async def invoke(observer: AdmissionObserver) -> None:
        if case == "invoke_error":
            raise RuntimeError("actual host failure")
        clock.now += 1
        if case != "missing":
            decision = AdmissionDecision(
                uuid.uuid4() if case == "foreign_tenant" else tenant,
                "discord",
                "103",
                "wrong" if case == "foreign_channel" else "101",
                "wrong" if case == "foreign_thread" else None,
                uuid.uuid4() if case == "foreign_account" else account,
                "writers_none",
            )
            observer(decision)
            if case == "duplicate":
                observer(decision)
        clock.now = 90 if case == "clock_backwards" else 102
        if case == "clock_nan":
            clock.now = float("nan")

    def effects() -> AdmissionEffects:
        return AdmissionEffects(
            complete=case != "partial",
            notices=(
                VisibleText(
                    evidence_id="post",
                    order=0,
                    observed_s=99.0,
                    message_id="m",
                    text="actual capture outside window",
                ),
            )
            if case == "outside_effect"
            else (),
        )

    async def run() -> object:
        return await admission_edge(
            deps=deps,
            scope=scope,
            backend="gemini",
            platform="discord",
            channel_id="101",
            external_user_id="103",
            evidence_id="attempt",
            invoke=invoke,
            effects=effects,
            native_send_count=count,
            clock=clock,
        )

    try:
        if case == "invoke_error":
            with pytest.raises(RuntimeError, match="actual host failure"):
                await run()
        elif case in ("missing", "partial", "positive_send", "counter_unknown"):
            receipt = await admission_edge(
                deps=deps,
                scope=scope,
                backend="gemini",
                platform="discord",
                channel_id="101",
                external_user_id="103",
                evidence_id="attempt",
                invoke=invoke,
                effects=effects,
                native_send_count=count,
                clock=clock,
            )
            if case == "missing":
                assert receipt.decision is None
                assert "HOST_ADMISSION_NOT_REACHED" in receipt.pending
            if case in ("missing", "counter_unknown"):
                assert receipt.native_sends is None
                assert "NATIVE_SEND_COVERAGE_MISSING" in receipt.pending
            if case == "partial":
                assert "ADAPTER_EFFECT_COVERAGE_MISSING" in receipt.pending
            if case == "positive_send":
                assert receipt.native_sends == 1, (
                    "preserve evidence of a violated denial; never infer zero"
                )
        else:
            with pytest.raises(ValueError):
                await run()
    finally:
        await engine.dispose()
