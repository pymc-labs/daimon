"""Pre-connection admission and runtime negatives for the public D11 seam."""

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest
from daimon.testing.d11_binding import D11TurnBinding
from daimon.testing.product_profiles import DEFAULT_PRODUCT_PROFILE, OPT_IN_PRODUCT_PROFILE
from mux.contracts.ids import ChannelRef, ModelRef, ResourceRef, Scope, ThreadRef
from mux.contracts.resources import ProviderBinding
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .test_footer_accounting import ROOT, TENANT, normalize


def admitted(
    provider="openai", url="postgresql+asyncpg://daimon:daimon@localhost/daimon_test_nc_N5"
):
    engine = create_async_engine(url)

    @event.listens_for(engine.sync_engine, "do_connect")
    def trap(*args):
        raise AssertionError("connection attempted before admission")

    native = ResourceRef(
        id="session",
        kind="session",
        provider=provider,
        account_scope_id="qa",
        tenant_id=str(TENANT),
        account_id="account",
    )
    scope = Scope(
        tenant_id=str(TENANT),
        account_id="account",
        principal_id="user",
        authorization_id="qa-owned",
    )
    binding = ProviderBinding(
        id="binding",
        provider=provider,
        profile=f"{provider}.offline",
        thread=ThreadRef(
            channel=ChannelRef(tenant_id=str(TENANT), platform="discord", channel_id="101"),
            thread_id="102",
        ),
        native_refs={"session": native.id},
        generation=1,
        config_revision=1,
    )
    return dict(
        sessions=async_sessionmaker(engine),
        scope=scope,
        binding=binding,
        native_session=native,
        root_turn_id=ROOT,
        capture_id="actual-posts",
        profile=DEFAULT_PRODUCT_PROFILE,
        admitted_model=ModelRef(provider=provider, id="model"),
        price=None,
        markup=Decimal(2),
        platform_user_id="user",
    )


@pytest.mark.parametrize("database", ["daimon", "daimon_test", "postgres", "daimon_test_nc_"])
def test_shared_database_refused_without_connecting(database, monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(ValueError):
        D11TurnBinding(**admitted(url=f"postgresql+asyncpg://daimon:daimon@localhost/{database}"))


def test_foreign_routed_bind_refused_without_connecting():
    options = admitted()
    foreign = create_async_engine("postgresql+asyncpg://daimon:daimon@localhost/daimon")
    options["sessions"].configure(binds={object: foreign})
    with pytest.raises(ValueError):
        D11TurnBinding(**options)


@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_foreign_usage_and_changed_routing_refused_before_connection(provider):
    options = admitted(provider)
    binding = D11TurnBinding(**options)
    billing = binding.billing()
    assert billing.native_footer_cost is None
    original = normalize(provider, options["native_session"], options["admitted_model"])
    for updates in (
        {"turn_id": "foreign-root"},
        {"session": options["native_session"].model_copy(update={"id": "foreign"})},
        {"model": ModelRef(provider=provider, id="foreign-model")},
    ):
        with pytest.raises(ValueError, match="foreign native usage"):
            await billing.record(observation=original.model_copy(update=updates))
    foreign = create_async_engine("postgresql+asyncpg://daimon:daimon@localhost/daimon")
    options["sessions"].configure(binds={object: foreign})
    with pytest.raises(ValueError):
        await billing.record(observation=original)


def test_profile_and_attempt_cannot_be_reused():
    binding = D11TurnBinding(**admitted())
    binding.billing()
    with pytest.raises(ValueError, match="reused"):
        binding.billing()
    with pytest.raises(FrozenInstanceError):
        binding.profile = OPT_IN_PRODUCT_PROFILE


def test_opt_in_explicit_context_and_unknown_policy():
    binding = D11TurnBinding(**{**admitted(), "profile": OPT_IN_PRODUCT_PROFILE})
    billed = binding.billing()
    assert billed.native_footer_cost is not None
    assert billed.native_footer_cost.result.status == "unknown"
    with pytest.raises(ValueError, match="inapplicable infrastructure"):
        replace(binding, infrastructure_policy="not_applicable", infrastructure_usd=Decimal(1))
    with pytest.raises(ValueError, match="unknown infrastructure"):
        replace(binding, infrastructure_policy="guess")


@pytest.mark.parametrize("problem", ["capture", "session", "root", "history", "survivors", "text"])
async def test_forged_surface_refused_before_accounting_read(problem, monkeypatch):
    from daimon.core.turn.state import TextBlock, TurnState
    from daimon.core.usage_totals import ProviderUsageTotals
    from daimon.testing.discord_surface import DiscordSurfaceHarness
    from daimon.testing.footer_accounting import FooterAccountingHook
    from daimon.testing.outcome_oracle import RunEvidence, TurnEvidence

    options = admitted()
    harness = DiscordSurfaceHarness(
        evidence_id="actual-posts",
        session_id="session",
        root_turn_id=ROOT,
        clock=lambda: 1.0,
        model_id="model",
    )
    state = TurnState(
        content=[TextBlock(kind="text", text="FOOTER-OK")],
        usage_totals=ProviderUsageTotals().add_observation(
            normalize("openai", options["native_session"], options["admitted_model"])
        ),
    )

    async def post(lifecycle):
        await lifecycle.post_initial()
        await lifecycle.on_terminal_success(state)

    capture = await harness.run(post)
    if problem in ("capture", "session", "root"):
        field = {"capture": "evidence_id", "session": "session_id", "root": "root_turn_id"}[problem]
        capture = capture.model_copy(update={field: "foreign"})
    elif problem == "history":
        capture = capture.model_copy(update={"events": capture.events[1:]})
    elif problem == "survivors":
        capture = capture.model_copy(update={"messages": ()})
    else:
        # Forge both accepted-history and final projection while retaining the
        # real raw wire payload; neither can manufacture visible dollars.
        final = capture.messages[-1]
        forged = final.model_copy(update={"content": "FOOTER-OK 1s $1.00"})
        capture = capture.model_copy(
            update={
                "messages": (*capture.messages[:-1], forged),
                "events": tuple(
                    e.model_copy(update={"message": forged})
                    if e.message.message_id == final.message_id
                    else e
                    for e in capture.events
                ),
            }
        )

    async def no_read(*args, **kwargs):
        raise AssertionError("accounting read attempted for forged surface")

    monkeypatch.setattr(FooterAccountingHook, "evaluate_turn", no_read)
    binding = D11TurnBinding(**options)
    binding.billing()
    evidence = RunEvidence(
        scenario_id="QA-D11-FOOTER-COST-MATCHES-LEDGER",
        backend="openai",
        turns=(
            TurnEvidence(
                turn=1, slot_id="slot", session_id="session", root_turn_id=ROOT, started_s=1.0
            ),
        ),
    )
    with pytest.raises(ValueError):
        await binding.evaluate(
            evidence=evidence, capture=capture, receipts=(), inventory=None, assertions=()
        )
