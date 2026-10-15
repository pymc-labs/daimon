"""Actual A3 SDK/MCP/card delivery through N9's frozen D8 consumer.

Join controls below are supplemental observations, not a native D8 execution
certificate: the frozen native recipe and own-fork/admin barriers stay PENDING.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools import openai_credential_requests as native_mcp
from daimon.core.github_connect_cards import ConnectCard
from daimon.core.stores.domain import Role
from daimon.testing import db as database_fixtures
from daimon.testing.discord_surface import DiscordSurfaceHarness
from daimon.testing.outcome_oracle import RunEvidence, TurnEvidence
from daimon.testing.provider_fixtures import load_provider_fixtures
from fastmcp import Context, FastMCP
from mcp.types import TextContent
from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import create_async_engine
from tests.turn import test_openai_credential_requests as a3

from .catalog_runner import ScenarioPlan, load_catalog, plan_scenario
from .headless_executor import CellResult
from .openai_credential_catalog import (
    D8,
    OpenAIGitHubOfferDelivery,
    bind_openai_d8_offer,
    execute_openai_d8_cell,
)

db_preflight = a3.db_preflight
db_clean = database_fixtures.db_clean
db_engine = database_fixtures.db_engine
db_schema = database_fixtures.db_schema
case = a3.case
isolated = a3.isolated
owned_database_preflight = a3.owned_database_preflight
type Case = a3.Case
REPO = Path(__file__).resolve().parents[2]
INDEX = REPO / "packages/testing/fixtures/target53/index.json"
CATALOG = INDEX.parent / "catalog"


async def pending_d8() -> tuple[ScenarioPlan, CellResult]:
    pack = load_provider_fixtures(INDEX, catalog_root=CATALOG)
    scenario, fixture = pack.select(D8, "openai")
    entry = next(e for e in load_catalog(CATALOG) if e.id == D8)
    entry = entry.model_copy(update={"sha256": scenario.source.sha256})
    plan = plan_scenario(entry, CATALOG, "openai", run_id="d8")
    result = await execute_openai_d8_cell(
        plan, scenario, fixture, source_root=CATALOG, repository_root=REPO
    )
    return plan, result


def delivery(case: Case, registry: Path) -> OpenAIGitHubOfferDelivery:
    surface = DiscordSurfaceHarness(
        evidence_id="d8:native:offer",
        session_id=case.origin.session.id,
        root_turn_id="controlled-join-root",
        model_id="gpt-6-luna",
    )
    result = OpenAIGitHubOfferDelivery(
        case.sessions, case.origin, surface, registry, product_profile="opt-in-product"
    )
    case.adapter.delivery = result
    return result


def join_control(result: CellResult, offer: OpenAIGitHubOfferDelivery) -> CellResult:
    surface = offer.surface.snapshot()
    # No terminal, session or tool certificate is fabricated by this control.
    turn = TurnEvidence(
        turn=1,
        slot_id="controlled-offer-join",
        session_id=surface.session_id,
        root_turn_id=surface.root_turn_id,
        started_s=surface.started_s,
    )
    return CellResult.model_validate(
        {
            **result.model_dump(exclude_computed_fields=True),
            "evidence": RunEvidence(scenario_id=D8, backend="openai", turns=(turn,)),
        }
    )


async def test_d8_native_mcp_product_card_reaches_frozen_consumer(
    case: Case, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, raw = await pending_d8()
    offer = delivery(case, tmp_path / "native-registry")
    server = FastMCP("explicit-native-d8")
    identity = AuthIdentity(
        tenant_id=uuid.UUID(case.adapter.host.scope.tenant_id),
        account_id=case.origin.requester_account_id,
        role=Role.ADMIN,
    )

    async def authenticated(ctx: Context) -> AuthIdentity:
        return identity

    async def granted(auth: AuthIdentity, id_: str) -> native_mcp.OpenAICredentialGrant:
        return native_mcp.OpenAICredentialGrant(case.adapter, case.origin)

    monkeypatch.setattr(native_mcp, "_auth", authenticated)
    await native_mcp.register_openai_credential_tools(server, operator_enabled=True, grants=granted)
    before = len(case.wire.requests)
    reply = await server.call_tool(
        "github_connect", {"origin_context_id": str(case.origin.origin_id)}
    )
    content = cast(Sequence[object], reply.content)
    assert isinstance(content[0], TextContent)
    receipt = TypeAdapter(a3.NativeOfferReceipt).validate_json(content[0].text)
    captured = bind_openai_d8_offer(
        plan, join_control(raw, offer), offer, product_profile="opt-in-product"
    )
    assert captured.catalog_outcome.checks[1].status == "PASS"
    assert captured.catalog_outcome.checks[1].code == "NATIVE_GITHUB_OFFER_OBSERVED"
    assert captured.status == "PENDING" and captured.gaps == raw.gaps
    assert len(captured.catalog_outcome.checks) == len(plan.assertions) + len(
        plan.global_assertions
    )
    message = captured.discord_surfaces[-1].messages[0]
    assert receipt.message_id == message.message_id
    assert "Connect GitHub" in message.embed_texts
    assert (
        "Nothing is connected yet.\n\nTap the button and tick the repos credential-team can use."
        in message.embed_texts
    )
    assert message.component_labels == ("Connect GitHub",)
    assert captured.evidence.turns[0].texts[0].message_id == message.message_id
    assert len(case.wire.requests[before:]) == 1 and case.wire.requests[-1].method == "GET"
    assert not captured.evidence.turns[0].terminal_capture_complete
    assert not captured.evidence.turns[0].text_capture_complete


@pytest.mark.parametrize("mutation", ["title", "forbidden_description"])
async def test_d8_product_builder_mutations_fail_source_predicates(
    case: Case, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    plan, raw = await pending_d8()
    offer = delivery(case, tmp_path / "registry")
    build = a3.native_product.build_connect_card

    def mutated(
        *,
        agent_name: str | None,
        identity_enabled: bool,
        avatar_url: str | None,
        public_base_url: str,
        variant: Literal["A", "B", "B_NO_FOOTER"] | None = None,
    ) -> ConnectCard:
        card = build(
            agent_name=agent_name,
            identity_enabled=identity_enabled,
            avatar_url=avatar_url,
            public_base_url=public_base_url,
            variant=variant,
        )
        return (
            replace(card, title="Something else")
            if mutation == "title"
            else replace(card, description="Ask your Daimon operator")
        )

    monkeypatch.setattr(a3.native_product, "build_connect_card", mutated)
    await case.adapter.offer(case.origin, kind="github")
    result = bind_openai_d8_offer(
        plan, join_control(raw, offer), offer, product_profile="opt-in-product"
    )
    assert result.catalog_outcome.checks[1 if mutation == "title" else 0].status == "FAIL"
    assert result.status == "FAIL"


async def test_d8_unbound_source_stays_pending_with_zero_consumer_requests(
    case: Case, tmp_path: Path
) -> None:
    plan, raw = await pending_d8()
    offer = delivery(case, tmp_path / "registry")
    await case.adapter.offer(case.origin, kind="github")
    result = bind_openai_d8_offer(plan, raw, offer, product_profile="opt-in-product")
    assert result == raw and result.status == "PENDING" and result.request_count == 0
    assert not result.evidence.turns and result.gaps
    assert result.catalog_outcome.checks[1].status == "PENDING"


async def test_d8_default_binding_refuses_before_registry_or_provider_io(
    case: Case, tmp_path: Path
) -> None:
    offer = OpenAIGitHubOfferDelivery(
        case.sessions,
        case.origin,
        DiscordSurfaceHarness(
            evidence_id="default",
            session_id=case.origin.session.id,
            root_turn_id="controlled-join-root",
            model_id="gpt-6-luna",
        ),
        tmp_path / "never-created",
    )
    case.adapter.delivery = offer
    before = len(case.wire.requests)
    # Registry admission precedes its own filesystem/platform effects. The A3
    # operator default itself is already covered by the product regressions.
    with pytest.raises(ValueError, match="opt-in profile"):
        offer.admit()
    assert len(case.wire.requests) == before
    assert not (tmp_path / "never-created").exists()
    assert not offer.surface.gateway.events


async def test_d8_missing_offer_remains_pending_without_provider_io(
    case: Case, tmp_path: Path
) -> None:
    plan, raw = await pending_d8()
    offer = delivery(case, tmp_path / "registry")
    result = join_control(raw, offer)
    before = len(case.wire.requests)
    assert bind_openai_d8_offer(plan, result, offer, product_profile="opt-in-product") == result
    assert result.catalog_outcome.checks[1].status == "PENDING"
    assert len(case.wire.requests) == before and not offer.registry.exists()


@pytest.mark.parametrize("routed", [False, True])
async def test_d8_foreign_factory_refuses_after_delivery_before_evidence_read(
    case: Case, tmp_path: Path, routed: bool
) -> None:
    from daimon.core.stores.domain import TurnOriginRow

    plan, raw = await pending_d8()
    offer = delivery(case, tmp_path / "registry")
    await case.adapter.offer(case.origin, kind="github")
    result = join_control(raw, offer)
    foreign = create_async_engine(
        "postgresql+asyncpg://test:test@foreign.invalid/daimon_test_nc_n11"
    )
    original = dict(case.sessions.kw)
    try:
        case.sessions.configure(
            **({"binds": {TurnOriginRow: foreign}} if routed else {"bind": foreign})
        )
        with pytest.raises(ValueError, match="owned local lane engines"):
            bind_openai_d8_offer(plan, result, offer, product_profile="opt-in-product")
    finally:
        case.sessions.kw.clear()
        case.sessions.kw.update(original)
        await foreign.dispose()


@pytest.mark.parametrize("foreign", ["root", "session", "window", "source", "profile", "labels"])
async def test_d8_join_and_payload_substitutions_refuse(
    case: Case, tmp_path: Path, foreign: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan, raw = await pending_d8()
    offer = delivery(case, tmp_path / "registry")
    await case.adapter.offer(case.origin, kind="github")
    result = join_control(raw, offer)
    turn = result.evidence.turns[0]
    if foreign in ("root", "session", "window"):
        changes = (
            {"root_turn_id": "foreign"}
            if foreign == "root"
            else (
                {"session_id": "foreign"}
                if foreign == "session"
                else {"started_s": turn.started_s + 1}
            )
        )
        result = result.model_copy(
            update={
                "evidence": result.evidence.model_copy(
                    update={"turns": (turn.model_copy(update=changes),)}
                )
            }
        )
    elif foreign == "source":
        plan = plan.model_copy(
            update={"scenario": plan.scenario.model_copy(update={"sha256": "0" * 64})}
        )
    elif foreign == "profile":
        offer.product_profile = "default"
    else:
        message = offer.surface.gateway.surviving_messages()[0]
        payload = json.loads(message.payload_json)
        payload["components"][0]["components"][0]["label"] = "forged"
        snapshot = offer.surface.snapshot()
        # Mutate only the serialized payload; it may not be masked by the
        # diagnostic component_labels field supplied by an evidence producer.
        monkeypatch.setattr(
            offer.surface,
            "_receipt",
            snapshot.model_copy(
                update={
                    "messages": (message.model_copy(update={"payload_json": json.dumps(payload)}),)
                }
            ),
        )
    with pytest.raises(ValueError):
        bind_openai_d8_offer(plan, result, offer, product_profile="opt-in-product")
