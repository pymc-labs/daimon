"""Opt-in A3 offer delivery and the OpenAI D8 catalog observation boundary.

The existing consumer owns native turns, step barriers and source assertions.
This hook observes an offer actually posted by the native credential adapter;
it never invokes that adapter on the model's behalf or synthesizes a host root.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import discord
from daimon.core.github_connect_cards import ConnectCard, discord_embed_payload
from daimon.core.posted_controls import PostedCard
from daimon.core.turn.openai_credential_requests import (
    NativeCredentialOrigin,
    NativeCredentialRequest,
    NativeOfferReceipt,
)
from daimon.testing.db import require_owned_sessionmaker
from daimon.testing.discord_surface import DiscordSurfaceCapture, DiscordSurfaceHarness
from daimon.testing.outcome_oracle import CheckResult, OutcomeReport, TurnEvidence
from daimon.testing.provider_fixtures import BackendFixture, ScenarioFixture
from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if __package__:
    from .discord_catalog_surface import bind_discord_text
    from .platform_catalog_surface import evaluate_platform_catalog
else:
    from discord_catalog_surface import bind_discord_text
    from platform_catalog_surface import evaluate_platform_catalog

if TYPE_CHECKING:
    if __package__:
        from .catalog_runner import ScenarioPlan
        from .headless_executor import CellResult
    else:
        from catalog_runner import ScenarioPlan
        from headless_executor import CellResult

D8 = "QA-D8-GITHUB-CONNECT-OFFERED"
D8_SOURCE = "0d774eaef640bcb864becc1212a1708911ce091b73fc92bf87b22efaa1e8285a"
type OfferProfile = Literal["default", "opt-in-product"]


class OpenAIGitHubOfferDelivery:
    """Owned offline native registry and real discord.py serialization.

    The operator supplies the accepted native origin and root identity. The
    registry contains request identities, never credentials; there is no OAuth,
    submission or legacy MA interaction handler. The button's copy comes from
    the product-built ConnectCard, rather than a hand-written fixture label.
    """

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        origin: NativeCredentialOrigin,
        surface: DiscordSurfaceHarness,
        registry: Path,
        *,
        product_profile: OfferProfile = "default",
    ) -> None:
        self.sessions, self.origin, self.surface, self.registry = (
            sessions,
            origin,
            surface,
            registry,
        )
        self.product_profile = product_profile
        self._receipt: NativeOfferReceipt | None = None
        self._request: NativeCredentialRequest | None = None

    def admit(self) -> None:
        if self.product_profile != "opt-in-product":
            raise ValueError("native GitHub capture requires the opt-in profile")
        require_owned_sessionmaker(self.sessions)
        surface = self.surface.snapshot()
        if (
            self.origin.session.provider != "openai"
            or self.origin.binding.profile != "openai.persistent_workspace"
            or self.origin.session.id != surface.session_id
            or self.origin.binding.thread.thread_id != str(self.surface.thread.id)
        ):
            raise ValueError("native offer capture differs from its owned origin")

    async def register(self, request: NativeCredentialRequest) -> None:
        self.admit()
        if (
            self._request is not None
            or request.origin != self.origin
            or request.thread != self.origin.binding.thread
            or request.kind != "github"
            or request.target != "GitHub"
            or request.ref.id != uuid.UUID(request.ref.id).hex
            or request.ref.kind != "credential_request"
            or request.ref.provider != "openai"
            or request.ref.tenant_id != self.origin.agent.tenant_id
            or request.ref.account_id != self.origin.agent.account_id
            or request.ref.account_scope_id != self.origin.agent.account_scope_id
        ):
            raise ValueError("native GitHub request differs from its owned origin")
        self.registry.mkdir(parents=True, exist_ok=True)
        with (self.registry / request.ref.id).open("xb") as file:
            file.write(TypeAdapter(NativeCredentialRequest).dump_json(request))
        self._request = request

    async def post(
        self, request: NativeCredentialRequest, card: ConnectCard | PostedCard
    ) -> NativeOfferReceipt:
        self.admit()
        if (
            request != self._request
            or self._receipt is not None
            or not isinstance(card, ConnectCard)
        ):
            raise ValueError("native offer lacks one registered product ConnectCard")
        stored = TypeAdapter(NativeCredentialRequest).validate_json(
            (self.registry / request.ref.id).read_bytes()
        )
        if stored != request:
            raise ValueError("native GitHub registry changed before delivery")
        view = discord.ui.View(timeout=None)
        view.add_item(
            discord.ui.Button(
                label=card.title, custom_id=request.custom_id, style=discord.ButtonStyle.primary
            )
        )
        message = await self.surface.thread.send(
            embed=discord.Embed.from_dict(discord_embed_payload(card)),
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.admit()  # A suspended delivery may not redirect subsequent DB reads.
        self._receipt = NativeOfferReceipt(request.ref, request.thread, str(message.id))
        return self._receipt

    def snapshot(self, turn: TurnEvidence) -> DiscordSurfaceCapture | None:
        self.admit()
        if self._receipt is None:
            return None
        surface = self.surface.snapshot()
        if (
            turn.turn != 1
            or turn.session_id != surface.session_id
            or turn.root_turn_id != surface.root_turn_id
            or turn.started_s > surface.started_s
            or self._request is None
            or self._request.origin != self.origin
            or self._receipt.thread != self.origin.binding.thread
            or len(surface.messages) != 1
            or surface.messages[0].message_id != self._receipt.message_id
            or len(surface.events) != 1
            or surface.events[0].operation != "send"
        ):
            raise ValueError("native GitHub offer is foreign to the catalog root")
        return surface


def bind_openai_d8_offer(
    plan: ScenarioPlan,
    result: CellResult,
    delivery: OpenAIGitHubOfferDelivery,
    *,
    product_profile: OfferProfile = "default",
) -> CellResult:
    """Bind only the source's component predicate, preserving every other result.

    Missing native roots or delivery remain PENDING. Setup/admin/routing/tool
    gaps, global checks and existing text coverage are retained verbatim. N9's
    opt-in report owns the final ``PASS under opt-in profile`` label.
    """
    if product_profile != "opt-in-product" or delivery.product_profile != product_profile:
        raise ValueError("D8 binding requires the opt-in profile")
    if (
        plan.backend != "openai"
        or plan.scenario.id != D8
        or plan.scenario.sha256 != D8_SOURCE
        or result.backend != plan.backend
        or result.scenario_id != plan.scenario.id
        or result.source_sha256 != plan.scenario.sha256
    ):
        raise ValueError("native GitHub hook differs from the frozen OpenAI D8 source")
    predicates = [
        (i, predicate)
        for i, predicate in enumerate([*plan.assertions, *plan.global_assertions])
        if predicate.get("kind") == "component_present"
    ]
    if len(predicates) != 1 or predicates[0][1] != {
        "kind": "component_present",
        "turn": 1,
        "label_pattern": "(?i)connect github",
    }:
        raise ValueError("D8 component predicate differs from its frozen source")
    index, predicate = predicates[0]
    if len(result.catalog_outcome.checks) != len(plan.assertions) + len(plan.global_assertions):
        raise ValueError("D8 consumer omitted frozen assertions")
    turn = next((t for t in result.evidence.turns if t.turn == 1), None)
    if turn is None:
        return result
    surface = delivery.snapshot(turn)
    if surface is None:
        return result
    observed = bind_discord_text(
        turn, surface, expected_id=surface.evidence_id, observed_s=surface.observed_s
    )

    messages = list(surface.messages)
    for message in messages:
        payload = cast(dict[str, Any], json.loads(message.payload_json))
        labels = tuple(
            component["label"]
            for row in payload.get("components", [])
            for component in row.get("components", [])
            if isinstance(component.get("label"), str)
        )
        if labels != message.component_labels:
            raise ValueError("GitHub component labels differ from the delivered payload")
    matched = [
        m
        for m in messages
        if any(re.search(predicate["label_pattern"], label) for label in m.component_labels)
    ]
    check = CheckResult(
        kind="component_present",
        turn=1,
        status="PASS" if matched else "FAIL",
        code="NATIVE_GITHUB_OFFER_OBSERVED" if matched else "NATIVE_GITHUB_OFFER_LABEL_MISSING",
        evidence_ids=tuple(f"{surface.evidence_id}:message:{m.message_id}" for m in matched),
    )
    offset = max((text.order for text in turn.texts), default=-1) + 1
    texts = tuple(
        text.model_copy(update={"order": offset + i}) for i, text in enumerate(observed.texts)
    )
    bound = TurnEvidence.model_validate(
        {
            **turn.model_dump(),
            "texts": (*turn.texts, *texts),
            "text_capture_complete": turn.text_capture_complete and observed.text_capture_complete,
        }
    )
    evidence = type(result.evidence).model_validate(
        {
            **result.evidence.model_dump(),
            "turns": tuple(bound if t.turn == 1 else t for t in result.evidence.turns),
        }
    )
    outcome = evaluate_platform_catalog(
        evidence, [*plan.assertions, *plan.global_assertions], result.platform_surfaces
    )
    checks = list(outcome.checks)
    checks[index] = check
    # Revalidate; no model_copy may bypass the consumer's invariants.
    return type(result).model_validate(
        {
            **result.model_dump(exclude_computed_fields=True),
            "evidence": evidence,
            "catalog_outcome": OutcomeReport(
                scenario_id=result.scenario_id, backend=result.backend, checks=tuple(checks)
            ),
            "discord_surfaces": (*result.discord_surfaces, surface),
        }
    )


async def execute_openai_d8_cell(
    plan: ScenarioPlan,
    scenario: ScenarioFixture,
    fixture: BackendFixture,
    *,
    source_root: Path,
    repository_root: Path,
    github_offer: OpenAIGitHubOfferDelivery | None = None,
    product_profile: OfferProfile = "default",
) -> CellResult:
    """N9's consumer first; an explicit offer can only supplement its own root."""
    if __package__:
        from .headless_executor import execute_openai_cell
    else:
        from headless_executor import execute_openai_cell

    if product_profile not in ("default", "opt-in-product"):
        raise ValueError("unknown D8 product profile")
    if github_offer is not None and (
        product_profile != "opt-in-product"
        or github_offer.product_profile != product_profile
        or plan.scenario.id != D8
        or plan.scenario.sha256 != D8_SOURCE
        or plan.backend != "openai"
    ):
        raise ValueError("D8 offer consumer requires its frozen opt-in OpenAI selection")
    result = await execute_openai_cell(
        plan, scenario, fixture, source_root=source_root, repository_root=repository_root
    )
    return (
        result
        if github_offer is None
        else bind_openai_d8_offer(plan, result, github_offer, product_profile=product_profile)
    )
