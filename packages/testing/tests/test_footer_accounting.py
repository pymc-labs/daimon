"""Actual adapter footer / independent Postgres accounting joins, offline only."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import pytest
import pytest_asyncio
from daimon.core import accounting_outbox
from daimon.core.pricing import MODEL_PRICING, ProviderPrice
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.turn.state import TextBlock, TurnState, UsageTotals
from daimon.core.usage_totals import ProviderUsageTotals
from daimon.testing.db import build_test_engine, require_owned_sessionmaker
from daimon.testing.discord_surface import DiscordSurfaceCapture, DiscordSurfaceHarness
from daimon.testing.factories import make_tenant
from daimon.testing.footer_accounting import (
    FooterAccountingHook,
    FooterAccountingReport,
    UsagePriceReceipt,
)
from daimon.testing.outcome_oracle import TerminalEvidence
from daimon.testing.turn_accounting import RevisionRateAttribution, TurnUsageInventory
from mux.contracts.ids import ChannelRef, ModelRef, Provider, ResourceRef, Scope, ThreadRef
from mux.contracts.resources import ProviderBinding
from mux.contracts.usage import UsageObservation
from mux.drivers.anthropic.usage import observation_from_event
from mux.drivers.gemini.usage import observation_from_interaction
from mux.drivers.openai.usage import observation as openai_observation
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

NOW = datetime(2026, 10, 10, tzinfo=UTC)
TENANT = uuid.UUID(int=505)
ROOT = "root"
MARKUP = Decimal("2")


@dataclass(frozen=True)
class Account:
    hook: FooterAccountingHook
    observation: UsageObservation
    price: ProviderPrice

    @property
    def receipt(self) -> UsagePriceReceipt:
        return UsagePriceReceipt(self.observation, self.price, Decimal(0), "offline:no-container")

    async def record(self, usage: UsageObservation | None = None, **kwargs: Any) -> bool:
        options: dict[str, Any] = {"markup": MARKUP, **kwargs}
        async with self.hook.sessions() as session, session.begin():
            return await accounting_outbox.record_observation_usage(
                session,
                binding_id=self.hook.binding.id,
                observation=usage or self.observation,
                tenant_id=TENANT,
                platform_user_id="user",
                channel_id="101",
                pricing=MODEL_PRICING[self.observation.model.id]
                if self.observation.session.provider == "anthropic" and self.observation.model
                else None,
                provider_price=self.price
                if self.observation.session.provider != "anthropic"
                else None,
                infrastructure_usd=Decimal(0),
                billing_grain=self.observation.grain,
                **options,
            )

    async def capture(
        self,
        *,
        output: int = 1000,
        incoming: int = 1000,
        notify: bool = False,
        agent_name: str = "Daimon",
    ) -> DiscordSurfaceCapture:
        h = DiscordSurfaceHarness(
            evidence_id="actual:discord:turn1",
            session_id="session",
            root_turn_id=ROOT,
            agent_name=agent_name,
            model_id=self.price.model,
            markup=MARKUP,
            notify_on_completion=notify,
        )
        totals: UsageTotals = (
            UsageTotals(input_tokens=incoming, output_tokens=output)
            if self.observation.session.provider == "anthropic"
            else ProviderUsageTotals().add_observation(self.observation)
        )
        state = TurnState(
            content=[TextBlock(kind="text", text="FOOTER-OK")],
            usage_totals=totals,
        )

        async def post(lc: Any) -> None:
            await lc.post_initial()
            await lc.on_terminal_success(state)

        return await h.run(post)

    async def evaluate(
        self, capture: DiscordSurfaceCapture, **kwargs: Any
    ) -> FooterAccountingReport:
        options: dict[str, Any] = {
            "terminal": terminal(),
            "receipts": (self.receipt,),
            "markup": MARKUP,
            **kwargs,
        }
        return await self.hook.evaluate(capture=capture, **options)


def terminal(**kwargs: Any) -> TerminalEvidence:
    values: dict[str, Any] = {
        "evidence_id": "native:terminal",
        "session_id": "session",
        "root_turn_id": ROOT,
        "authority": "record",
        "outcome": "completed",
        "observed_s": 1.0,
        **kwargs,
    }
    return TerminalEvidence(**values)


def normalize(
    provider: Provider,
    native: ResourceRef,
    model: ModelRef,
    *,
    revision: int = 1,
    output: int | None = 1000,
    incoming: int = 1000,
) -> UsageObservation:
    if provider == "anthropic":
        observation = observation_from_event(
            {
                "id": "span",
                "type": "span.model_request_end",
                "model_usage": {
                    "input_tokens": incoming,
                    "output_tokens": output,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            },
            native,
            observed_at=NOW,
            turn_id=ROOT,
            model_id=model.id,
        )
    elif provider == "openai":
        observation = openai_observation(
            {
                "id": ROOT,
                "usage": {
                    "input_tokens": incoming,
                    "output_tokens": output,
                    "input_tokens_details": {"cached_tokens": 0},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            },
            native,
            revision=revision,
        ).model_copy(update={"model": model, "observed_at": NOW})
    else:
        observation = observation_from_interaction(
            {
                "id": ROOT,
                "updated": f"native-version-{revision}",
                "usageMetadata": {
                    "promptTokenCount": incoming,
                    "cachedContentTokenCount": 0,
                    "candidatesTokenCount": None if output is None else output - 100,
                    "thoughtsTokenCount": 100,
                },
            },
            native,
            revision=revision,
            observed_at=NOW,
            root_turn=ROOT,
            model=model,
        )
    return observation


def admit_footer_database(url: str, schema: str) -> None:
    probe = build_test_engine(url, schema)
    try:
        try:
            require_owned_sessionmaker(probe)
        except ValueError:
            if (
                probe.url.get_backend_name() == "postgresql"
                and probe.url.host in ("localhost", "127.0.0.1")
                and not probe.url.query
                and probe.url.database == "daimon_test"
                and not (
                    os.environ.get("CI") == "true" and os.environ.get("GITHUB_ACTIONS") == "true"
                )
            ):
                pytest.skip(
                    "Accounting hooks intentionally refuse plain local daimon_test; "
                    "use an owned lane DB"
                )
            raise
    finally:
        probe.sync_engine.dispose()


@pytest.fixture
def footer_engine(db_preflight, request: pytest.FixtureRequest, db_schema: str) -> AsyncEngine:
    url = os.environ.get("DAIMON_DATABASE__TEST_URL")
    if not url:
        raise ValueError("Footer fixtures require a configured owned test database")
    admit_footer_database(url, db_schema)
    engine = cast(AsyncEngine, request.getfixturevalue("db_engine"))
    require_owned_sessionmaker(engine)
    request.getfixturevalue("db_clean")
    return engine


@pytest_asyncio.fixture(params=["anthropic", "openai", "gemini"])
async def account(
    db_preflight, request: pytest.FixtureRequest, footer_engine: AsyncEngine
) -> AsyncIterator[Account]:
    provider: Provider = request.param
    db_engine = footer_engine
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        await make_tenant(session, id=TENANT)
    native = ResourceRef(
        id="session",
        kind="session",
        provider=provider,
        account_scope_id="workspace",
        tenant_id=str(TENANT),
        account_id="account",
    )
    model = ModelRef(
        provider=provider, id="claude-haiku-4-5" if provider == "anthropic" else "model"
    )
    binding = ProviderBinding(
        id="binding",
        provider=provider,
        profile=f"{provider}.offline",
        thread=ThreadRef(
            channel=ChannelRef(tenant_id=str(TENANT), platform="discord", channel_id="101"),
            thread_id="102",
        ),
        native_refs={"session": "session"},
        generation=1,
        config_revision=1,
    )
    await PostgresStateStore(sessions).put_binding(binding, expected_generation=0)
    observation = normalize(provider, native, model)
    price = ProviderPrice(
        provider=provider,
        model=model.id,
        checked_on=NOW.date(),
        input=Decimal(1),
        output=Decimal(5),
        cache_read=Decimal("0.1"),
        cache_write=Decimal("1.25") if provider == "anthropic" else None,
    )
    hook = FooterAccountingHook(
        sessions,
        Scope(
            tenant_id=str(TENANT),
            account_id="account",
            principal_id="user",
            authorization_id="qa:owned",
        ),
        binding,
        native,
        ROOT,
    )
    yield Account(hook, observation, price)


async def test_actual_posted_footer_and_independent_accounting_do_not_certify_live(
    account: Account,
) -> None:
    assert await account.record()
    before = await account.hook.snapshot()
    capture = await account.capture()
    report = await account.evaluate(capture)
    assert report.ledger_usd == report.controlled_expected_usd == Decimal("0.012000")
    assert report.source == "controlled_offline_host_arithmetic"
    assert report.native_revisions == (
        (account.observation.id, 1, account.observation.native_revision),
    )
    assert await account.hook.snapshot() == before  # read-only, not a second recorder
    if account.observation.session.provider == "anthropic":
        assert "$0.012 used" in capture.messages[-1].payload_json
        assert [item.code for item in report.checks] == ["TURN_TOTAL_COVERAGE_UNBOUND"] * 2
    else:
        assert " used" not in capture.messages[-1].payload_json
        assert [(item.status, item.code) for item in report.checks] == [
            ("PENDING", "POSTED_COST_MISSING"),
            ("PASS", "CONTROLLED_HOST_ARITHMETIC_MATCH"),
        ]


async def test_real_recorder_wrong_markup_fails_independent_usage_join(account: Account) -> None:
    assert await account.record(markup=Decimal("3"))
    report = await account.evaluate(await account.capture())
    assert all(
        item.code == "LEDGER_NATIVE_USAGE_MISMATCH" and item.status == "FAIL"
        for item in report.checks
    )


async def test_actual_anthropic_footer_mismatch_is_observed_not_reconstructed(
    account: Account,
) -> None:
    if account.observation.session.provider != "anthropic":
        pytest.skip("Only Anthropic has a posted display tariff; alternate footers omit cost")
    assert await account.record()
    report = await account.evaluate(await account.capture(output=2000))
    assert report.checks[0].status == "FAIL"
    assert report.checks[0].code == "POSTED_COST_LEDGER_MISMATCH"
    assert report.checks[1].status == "PENDING"


@pytest.mark.parametrize(
    "gap", ["price", "unverified", "infrastructure", "infra_source", "future_price"]
)
async def test_unknown_price_or_container_policy_stays_pending(account: Account, gap: str) -> None:
    assert await account.record()
    receipt = account.receipt
    if gap == "price":
        receipt = replace(receipt, price=None)
    elif gap == "unverified":
        receipt = replace(receipt, price=replace(account.price, verified=False))
    elif gap == "infrastructure":
        receipt = replace(receipt, infrastructure_usd=None)
    elif gap == "infra_source":
        receipt = replace(receipt, infrastructure_evidence_id=None)
    else:
        receipt = replace(
            receipt, price=replace(account.price, checked_on=datetime(2027, 1, 1).date())
        )
    report = await account.evaluate(await account.capture(), receipts=(receipt,))
    assert all(item.status == "PENDING" for item in report.checks)
    assert report.controlled_expected_usd is None


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("session_id", "other", "POSTED_ROOT_IDENTITY_MISMATCH"),
        ("root_turn_id", "other", "POSTED_ROOT_IDENTITY_MISMATCH"),
        ("post_capture_complete", False, "POST_CAPTURE_INCOMPLETE"),
        ("text_capture_complete", False, "POST_CAPTURE_INCOMPLETE"),
    ],
)
async def test_capture_identity_and_completeness(
    account: Account, field: str, value: object, code: str
) -> None:
    assert await account.record()
    capture = await account.capture()
    report = await account.evaluate(capture.model_copy(update={field: value}))
    assert all(item.code == code for item in report.checks)


async def test_preview_terminal_cannot_seal_accounting(account: Account) -> None:
    assert await account.record()
    report = await account.evaluate(await account.capture(), terminal=terminal(authority="preview"))
    assert all(item.status == "PENDING" for item in report.checks)


@pytest.mark.parametrize("change", ["counts", "revision", "native_version", "meter"])
async def test_native_receipts_must_match_durable_revision(account: Account, change: str) -> None:
    assert await account.record()
    update: Mapping[str, object] = {
        "counts": {"output_tokens": 999},
        "revision": {"revision": 2},
        "native_version": {"native_revision": "different"},
        "meter": {"native_meter": {"different": True}},
    }[change]
    receipt = replace(account.receipt, observation=account.observation.model_copy(update=update))
    report = await account.evaluate(await account.capture(), receipts=(receipt,))
    assert all(item.code == "NATIVE_DURABLE_REVISION_MISMATCH" for item in report.checks)


async def test_empty_or_duplicate_native_receipts_do_not_pass(account: Account) -> None:
    assert await account.record()
    capture = await account.capture()
    assert (await account.evaluate(capture, receipts=())).checks[0].code == "NATIVE_USAGE_MISSING"
    assert (await account.evaluate(capture, receipts=(account.receipt,) * 2)).checks[
        0
    ].code == "DUPLICATE_NATIVE_RECEIPT"


async def test_pending_latest_revision_cannot_use_old_debit(account: Account) -> None:
    if account.hook.native_session.provider == "anthropic":
        pytest.skip(
            "Native Anthropic spans are immutable; pending correction proof is neutral-only"
        )
    assert await account.record()
    corrected = normalize(
        account.hook.native_session.provider,
        account.hook.native_session,
        ModelRef(provider=account.hook.native_session.provider, id=account.price.model),
        revision=2,
        output=1100,
    )
    row = await PostgresStateStore(account.hook.sessions).record_usage("binding", corrected)
    assert row is not None
    receipt = replace(account.receipt, observation=corrected)
    report = await account.evaluate(await account.capture(), receipts=(receipt,))
    assert all(item.code == "OUTBOX_NOT_SETTLED" for item in report.checks)


async def test_signed_corrections_sum_actual_ledger_rows_once(account: Account) -> None:
    if account.observation.session.provider == "anthropic":
        pytest.skip("Native Anthropic spans are immutable; correction proof is neutral-only")
    assert await account.record()
    model = ModelRef(provider=account.hook.native_session.provider, id=account.price.model)
    corrected = normalize(
        account.hook.native_session.provider,
        account.hook.native_session,
        model,
        revision=2,
        output=1200,
    )
    assert await account.record(corrected)
    final = normalize(
        account.hook.native_session.provider,
        account.hook.native_session,
        model,
        revision=3,
        output=1100,
    )
    assert await account.record(final)
    assert not await account.record(final)
    report = await account.evaluate(
        await account.capture(), receipts=(replace(account.receipt, observation=final),)
    )
    assert report.ledger_usd == report.controlled_expected_usd == Decimal("0.013000")
    assert report.checks[1].status == "PASS"
    snapshot = await account.hook.snapshot()
    assert len(snapshot.ledger) == 3
    assert any(item.delta_usd > 0 for item in snapshot.ledger)


async def test_unattributed_or_missing_native_work_is_pending(account: Account) -> None:
    assert await account.record()
    other = account.observation.model_copy(update={"id": "extra", "turn_id": None})
    assert await PostgresStateStore(account.hook.sessions).record_usage("binding", other)
    report = await account.evaluate(await account.capture())
    assert all(item.code == "ROOT_USAGE_COVERAGE_INCOMPLETE" for item in report.checks)


async def test_query_performs_no_writes(
    db_preflight, account: Account, db_engine: AsyncEngine
) -> None:
    assert await account.record()
    writes: list[str] = []

    def observe(
        connection: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
    ) -> None:
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", observe)
    try:
        await account.evaluate(await account.capture())
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", observe)
    assert writes == []


@pytest.mark.parametrize("routed", [False, True])
async def test_shared_or_foreign_bind_refused_before_connect(
    account: Account, routed: bool
) -> None:
    foreign = create_async_engine("postgresql+asyncpg://localhost/daimon_test")
    attempts: list[object] = []

    def refuse(*args: Any, **kwargs: Any) -> None:
        attempts.append(foreign)
        raise AssertionError("guard connected")

    event.listen(foreign.sync_engine, "do_connect", refuse)
    sessions = async_sessionmaker(
        account.hook.sessions.kw["bind"] if routed else foreign,
        binds={object: foreign} if routed else None,
    )
    try:
        with pytest.raises(ValueError, match="owned local"):
            replace(account.hook, sessions=sessions)
    finally:
        await foreign.dispose()
    assert attempts == []


async def test_partial_native_counts_are_not_filled_from_previous_totals(account: Account) -> None:
    partial = normalize(
        account.hook.native_session.provider,
        account.hook.native_session,
        ModelRef(provider=account.hook.native_session.provider, id=account.price.model),
        output=None,
    )
    await account.record(partial)
    report = await account.evaluate(
        await account.capture(), receipts=(replace(account.receipt, observation=partial),)
    )
    assert all(item.code == "NATIVE_TOTALS_INCOMPLETE" for item in report.checks)


async def test_summary_moves_to_real_notification_answer(account: Account) -> None:
    assert await account.record()
    capture = await account.capture(notify=True)
    assert capture.events[-1].operation == "delete"
    assert capture.messages[0].message_id != capture.events[0].message.message_id
    report = await account.evaluate(capture)
    assert report.ledger_usd == Decimal("0.012000")
    assert report.checks[0].code == (
        "TURN_TOTAL_COVERAGE_UNBOUND"
        if account.hook.native_session.provider == "anthropic"
        else "POSTED_COST_MISSING"
    )


async def test_reconfigured_factory_is_refused_before_query(account: Account) -> None:
    foreign = create_async_engine("postgresql+asyncpg://localhost/daimon_test")
    attempts: list[object] = []

    def refuse(*args: Any, **kwargs: Any) -> None:
        attempts.append(foreign)
        raise AssertionError("guard connected")

    event.listen(foreign.sync_engine, "do_connect", refuse)
    previous = account.hook.sessions.kw["bind"]
    account.hook.sessions.configure(bind=foreign)
    try:
        with pytest.raises(ValueError, match="owned local"):
            await account.hook.snapshot()
    finally:
        account.hook.sessions.configure(bind=previous)
        await foreign.dispose()
    assert attempts == []


async def test_agent_name_cannot_supply_missing_used_field(account: Account) -> None:
    assert await account.record()
    capture = await account.capture(agent_name="1s\u2003\u2003$0.012 used")
    report = await account.evaluate(capture)
    assert report.checks[0].code == (
        "TURN_TOTAL_COVERAGE_UNBOUND"
        if account.hook.native_session.provider == "anthropic"
        else "POSTED_COST_MISSING"
    )


async def test_less_than_footer_is_a_strict_bound_not_point_estimate(account: Account) -> None:
    if account.hook.native_session.provider != "anthropic":
        pytest.skip("Only the actual Anthropic adapter currently posts a priced footer")
    tiny = normalize(
        "anthropic",
        account.hook.native_session,
        ModelRef(provider="anthropic", id=account.price.model),
        incoming=0,
        output=1,
    )
    assert await account.record(tiny)
    capture = await account.capture(incoming=0, output=1)
    assert "<$0.001 used" in capture.messages[-1].payload_json
    report = await account.evaluate(capture, receipts=(replace(account.receipt, observation=tiny),))
    assert report.ledger_usd == Decimal("0.000010")
    assert report.checks[0].code == "TURN_TOTAL_COVERAGE_UNBOUND"


async def test_separate_owned_routed_engine_cannot_fake_one_consistent_snapshot(
    account: Account,
) -> None:
    other = create_async_engine("postgresql+asyncpg://localhost/daimon_test_nc_N5")
    attempts: list[object] = []

    def refuse(*args: Any, **kwargs: Any) -> None:
        attempts.append(other)
        raise AssertionError("routing check connected")

    event.listen(other.sync_engine, "do_connect", refuse)
    sessions = async_sessionmaker(account.hook.sessions.kw["bind"], binds={object: other})
    try:
        with pytest.raises(ValueError, match="one consistent bind"):
            replace(account.hook, sessions=sessions)
    finally:
        await other.dispose()
    assert attempts == []


@pytest.mark.parametrize("gap", ["model", "aggregate", "delegated", "wrong_price_model"])
async def test_unbound_optional_attribution_or_coverage_stays_pending_without_new_rows(
    account: Account,
    gap: str,
) -> None:
    assert await account.record()
    before = await account.hook.snapshot()
    receipt = account.receipt
    if gap == "model":
        receipt = replace(
            receipt, observation=account.observation.model_copy(update={"model": None})
        )
        code = "NATIVE_MODEL_ATTRIBUTION_UNBOUND"
    elif gap == "wrong_price_model":
        receipt = replace(receipt, price=replace(account.price, model="unknown-tariff-model"))
        code = "PRICE_IDENTITY_UNVERIFIED"
    else:
        update = {"covers": ("missing",)} if gap == "aggregate" else {"thread_id": "child"}
        receipt = replace(receipt, observation=account.observation.model_copy(update=update))
        code = "AGGREGATE_COVERAGE_CUT"
    report = await account.evaluate(await account.capture(), receipts=(receipt,))
    assert all(item.status == "PENDING" and item.code == code for item in report.checks)
    assert await account.hook.snapshot() == before  # no manufactured aggregate rows


def turn_rate(account: Account, usage: UsageObservation) -> RevisionRateAttribution:
    return RevisionRateAttribution(
        observation_id=usage.id,
        revision=usage.revision,
        price=account.price,
        rate_evidence_id="control:independent-dated-price",
        infrastructure_policy="not_applicable",
        infrastructure_usd=Decimal(0),
        infrastructure_evidence_id="control:independent-no-container-policy",
    )


def turn_inventory(account: Account, *values: UsageObservation) -> TurnUsageInventory:
    # Component control supplied from the native normalizer, never from DB rows.
    # This explicit envelope is not catalog execution or a live walk seal.
    return TurnUsageInventory(
        binding_id=account.hook.binding.id,
        session=account.hook.native_session,
        root_turn_id=ROOT,
        revisions=tuple((value.id, value.revision) for value in values),
        evidence_id="control:independent-terminal-inventory",
        kind="sealed_request_walk"
        if account.hook.native_session.provider == "anthropic"
        else "terminal_native_inventory",
    )


async def evaluate_turn_control(
    account: Account, capture: DiscordSurfaceCapture | None = None, **changes: Any
) -> FooterAccountingReport:
    options: dict[str, Any] = {
        "capture": capture if capture is not None else await account.capture(),
        "terminal": terminal(),
        "receipts": (account.observation,),
        "attributions": (turn_rate(account, account.observation),),
        "inventory": turn_inventory(account, account.observation),
        "markup": MARKUP,
        **changes,
    }
    return await account.hook.evaluate_turn(**options)


def require_checks(report: FooterAccountingReport, *expected: tuple[str, str]) -> None:
    # Check semantic invariants even in the optimized proof run.
    actual = tuple((item.status, item.code) for item in report.checks)
    if actual != expected:
        raise RuntimeError(f"unexpected checks: {actual!r} != {expected!r}")


async def test_turn_reader_resolves_complete_posted_cost_with_independent_seal(
    account: Account,
) -> None:
    await account.record()
    before = await account.hook.snapshot()
    report = await evaluate_turn_control(account)
    footer = (
        ("PASS", "CONTROLLED_POSTED_COST_MATCH")
        if account.hook.native_session.provider == "anthropic"
        else ("FAIL", "POSTED_COST_MISSING")
    )
    require_checks(report, footer, ("PASS", "CONTROLLED_HOST_ARITHMETIC_MATCH"))
    if (
        report.ledger_usd != Decimal("0.012000")
        or report.controlled_expected_usd != Decimal("0.012000")
        or report.source != "controlled_offline_host_arithmetic"
        or await account.hook.snapshot() != before
    ):
        raise RuntimeError("read-only complete arithmetic changed facts or lost precision")
    if "control:independent-terminal-inventory" not in report.checks[0].evidence_ids:
        raise RuntimeError("independent seal not retained")


@pytest.mark.parametrize(
    "gap", ["inventory", "rate", "price", "rate_evidence", "infrastructure", "time", "counts"]
)
async def test_turn_reader_unknowns_do_not_borrow_known_ledger(account: Account, gap: str) -> None:
    await account.record()
    changes: dict[str, Any] = {}
    rate = turn_rate(account, account.observation)
    if gap == "inventory":
        changes["inventory"] = None
    elif gap == "rate":
        changes["attributions"] = ()
    elif gap == "price":
        changes["attributions"] = (replace(rate, price=None),)
    elif gap == "rate_evidence":
        changes["attributions"] = (replace(rate, rate_evidence_id=None),)
    elif gap == "infrastructure":
        changes["attributions"] = (
            replace(rate, infrastructure_policy=None, infrastructure_usd=None),
        )
    elif gap == "time":
        changes["attributions"] = (replace(rate, infrastructure_policy="measured"),)
    else:
        partial = normalize(
            account.hook.native_session.provider,
            account.hook.native_session,
            ModelRef(provider=account.hook.native_session.provider, id=account.price.model),
            revision=2,
            output=None,
        ).model_copy(update={"revision": 2})
        await account.record(partial)
        changes.update(
            receipts=(partial,),
            inventory=turn_inventory(account, partial),
            attributions=(turn_rate(account, partial),),
        )
    report = await evaluate_turn_control(account, **changes)
    if any(item.status != "PENDING" for item in report.checks):
        raise RuntimeError(f"unknown {gap} passed: {report.checks}")
    if report.controlled_expected_usd is not None:
        raise RuntimeError("unknown native total replaced by the known debit")


@pytest.mark.parametrize(
    "change", ["native_revision", "tokens", "identity", "missing", "duplicate"]
)
async def test_turn_reader_compares_independent_native_receipt(
    account: Account, change: str
) -> None:
    await account.record()
    receipts = (account.observation,)
    if change == "native_revision":
        receipts = (account.observation.model_copy(update={"native_revision": "foreign"}),)
    elif change == "tokens":
        receipts = (account.observation.model_copy(update={"output_tokens": 2000}),)
    elif change == "identity":
        receipts = (account.observation.model_copy(update={"turn_id": "foreign"}),)
    elif change == "missing":
        receipts = ()
    else:
        receipts *= 2
    report = await evaluate_turn_control(account, receipts=receipts)
    status = "PENDING" if change == "missing" else "FAIL"
    if any(item.status != status for item in report.checks):
        raise RuntimeError("independent native mismatch was accepted")


async def test_turn_reader_signed_corrections_do_not_add_cumulative_history(
    account: Account,
) -> None:
    values = tuple(
        normalize(
            account.hook.native_session.provider,
            account.hook.native_session,
            ModelRef(provider=account.hook.native_session.provider, id=account.price.model),
            revision=index,
            output=output,
        ).model_copy(update={"revision": index})
        for index, output in enumerate((1000, 2000, 500), 1)
    )
    for value in values:
        await account.record(value)
    report = await evaluate_turn_control(
        account,
        await account.capture(output=500),
        receipts=(values[-1],),
        attributions=tuple(turn_rate(account, value) for value in values),
        inventory=turn_inventory(account, values[-1]),
    )
    expected = Decimal("0.007000")
    if report.ledger_usd != expected or report.controlled_expected_usd != expected:
        raise RuntimeError("historical cumulative revisions were counted as new work")
    footer = (
        ("PASS", "CONTROLLED_POSTED_COST_MATCH")
        if account.hook.native_session.provider == "anthropic"
        else ("FAIL", "POSTED_COST_MISSING")
    )
    require_checks(report, footer, ("PASS", "CONTROLLED_HOST_ARITHMETIC_MATCH"))


async def test_turn_reader_wrong_markup_is_a_real_settled_ledger_failure(account: Account) -> None:
    await account.record(markup=Decimal(3))
    report = await evaluate_turn_control(account)
    require_checks(report, *([("FAIL", "LEDGER_NATIVE_USAGE_MISMATCH")] * 2))


async def test_turn_reader_unsettled_revision_cannot_use_old_debit(account: Account) -> None:
    await account.record()
    latest = account.observation.model_copy(update={"revision": 2, "output_tokens": 2000})
    await PostgresStateStore(account.hook.sessions).record_usage("binding", latest)
    report = await evaluate_turn_control(
        account,
        receipts=(latest,),
        inventory=turn_inventory(account, latest),
        attributions=(turn_rate(account, latest),),
    )
    if any(item.status != "PENDING" for item in report.checks):
        raise RuntimeError("unsettled latest revision passed with the historical debit")


async def test_turn_reader_foreign_inventory_cannot_certify_root(account: Account) -> None:
    await account.record()
    with pytest.raises(ValueError, match="another binding/session/root"):
        await evaluate_turn_control(
            account,
            inventory=replace(turn_inventory(account, account.observation), root_turn_id="foreign"),
        )


async def test_turn_reader_actual_footer_mismatch_is_not_ledger_reconstruction(
    account: Account,
) -> None:
    await account.record()
    report = await evaluate_turn_control(account, await account.capture(output=2000))
    expected = (
        ("FAIL", "POSTED_COST_LEDGER_MISMATCH")
        if account.hook.native_session.provider == "anthropic"
        else ("FAIL", "POSTED_COST_MISSING")
    )
    require_checks(report, expected, ("PASS", "CONTROLLED_HOST_ARITHMETIC_MATCH"))


@pytest.mark.parametrize("gap", ["terminal", "preview", "partial", "foreign", "inventory_omission"])
async def test_turn_reader_requires_actual_complete_post_root(account: Account, gap: str) -> None:
    await account.record()
    changes: dict[str, Any] = {}
    if gap == "terminal":
        changes["terminal"] = None
    elif gap == "preview":
        changes["terminal"] = terminal(authority="preview")
    elif gap == "partial":
        changes["capture"] = (await account.capture()).model_copy(
            update={"post_capture_complete": False}
        )
    elif gap == "foreign":
        changes["capture"] = (await account.capture()).model_copy(
            update={"root_turn_id": "foreign"}
        )
    else:
        changes["inventory"] = replace(
            turn_inventory(account, account.observation), revisions=(("missing", 1),)
        )
    report = await evaluate_turn_control(account, **changes)
    if any(item.status != ("FAIL" if gap == "foreign" else "PENDING") for item in report.checks):
        raise RuntimeError("incomplete or foreign post/native root passed")


async def test_turn_reader_seals_disjoint_native_requests_without_aggregate_rows(
    account: Account,
) -> None:
    if account.hook.native_session.provider != "anthropic":
        pytest.skip("Independent request-walk test is Anthropic-specific")
    child = observation_from_event(
        {
            "id": "second-native-span",
            "type": "span.model_request_end",
            "model_usage": {
                "input_tokens": 1000,
                "output_tokens": 1000,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        },
        account.hook.native_session,
        observed_at=NOW,
        turn_id=ROOT,
        model_id=account.price.model,
    )
    await account.record()
    await account.record(child)
    before = await account.hook.snapshot()
    report = await evaluate_turn_control(
        account,
        await account.capture(incoming=2000, output=2000),
        receipts=(account.observation, child),
        inventory=turn_inventory(account, account.observation, child),
        attributions=(turn_rate(account, account.observation), turn_rate(account, child)),
    )
    require_checks(
        report,
        ("PASS", "CONTROLLED_POSTED_COST_MATCH"),
        ("PASS", "CONTROLLED_HOST_ARITHMETIC_MATCH"),
    )
    if (
        report.ledger_usd != Decimal("0.024000")
        or report.controlled_expected_usd != Decimal("0.024000")
        or len(before.usage) != 2
        or await account.hook.snapshot() != before
    ):
        raise RuntimeError("native request-walk join mutated rows or manufactured a total")
    for item in before.usage:
        if item.observation.grain != "model_request" or item.observation.covers:
            raise RuntimeError("synthetic aggregate replaced native request spans")


async def test_turn_reader_historical_null_root_is_bound_only_by_independent_seal(
    account: Account,
) -> None:
    historical = account.observation.model_copy(update={"turn_id": None})
    await account.record(historical)
    before = await account.hook.snapshot()
    report = await evaluate_turn_control(
        account,
        receipts=(historical,),
        inventory=turn_inventory(account, historical),
        attributions=(turn_rate(account, historical),),
    )
    footer = (
        ("PASS", "CONTROLLED_POSTED_COST_MATCH")
        if account.hook.native_session.provider == "anthropic"
        else ("FAIL", "POSTED_COST_MISSING")
    )
    require_checks(report, footer, ("PASS", "CONTROLLED_HOST_ARITHMETIC_MATCH"))
    if await account.hook.snapshot() != before or before.usage[0].observation.turn_id is not None:
        raise RuntimeError("inventory seal rewrote historical attribution")
    unsealed = await evaluate_turn_control(
        account,
        receipts=(historical,),
        attributions=(turn_rate(account, historical),),
        inventory=None,
    )
    require_checks(unsealed, *([("PENDING", "ROOT_USAGE_ATTRIBUTION_UNBOUND")] * 2))


async def test_turn_reader_reconfigured_foreign_bind_refused_without_connection(
    account: Account,
) -> None:
    capture = await account.capture()
    foreign = create_async_engine("postgresql+asyncpg://localhost/daimon_test")
    attempts: list[object] = []

    def refuse(*args: Any, **kwargs: Any) -> None:
        attempts.append(foreign)
        raise RuntimeError("foreign connection attempted")

    event.listen(foreign.sync_engine, "do_connect", refuse)
    original = account.hook.sessions.kw["bind"]
    account.hook.sessions.kw["binds"] = {object: foreign}
    try:
        with pytest.raises(ValueError, match="owned local"):
            await evaluate_turn_control(account, capture)
    finally:
        account.hook.sessions.kw.pop("binds")
        account.hook.sessions.configure(bind=original)
        await foreign.dispose()
    if attempts:
        raise RuntimeError("secondary foreign bind was connected before admission")


async def test_turn_reader_absent_durable_usage_is_unknown_not_complete(account: Account) -> None:
    report = await evaluate_turn_control(account)
    require_checks(report, *([("PENDING", "ROOT_USAGE_ATTRIBUTION_UNBOUND")] * 2))
    if report.ledger_usd is not None or report.controlled_expected_usd is not None:
        raise RuntimeError("missing durable usage filled with invented zero cost")
