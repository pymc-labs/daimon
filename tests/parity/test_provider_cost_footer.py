"""Executable record: a GPT or Gemini turn shows the same debit in every chat footer.

Discord, Slack and Teams read one core rule (`provider_turn_debit`), the pricing the
usage outbox settles with, markup included. An unpriced model, a partial reading or
usage not reported yet shows no cost. Web chat renders no per-turn cost.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from daimon.adapters.discord.lifecycle import DiscordTurnLifecycle
from daimon.adapters.slack.lifecycle import SlackTurnLifecycle
from daimon.adapters.teams.lifecycle import TeamsTurnLifecycle
from daimon.core.turn.state import TurnState
from daimon.core.usage_totals import ProviderUsageTotals
from mux.contracts.ids import ResourceRef
from mux.contracts.usage import UsageObservation

# Observation rates per 1M tokens: input 0.10, output 0.50 (gpt-6-luna);
# input 0.30, output 2.50 (gemini-3.5-flash-lite). 100k in and 10k out cost 0.015 and 0.055.
_PRICED = [("openai", "gpt-6-luna", "$0.015"), ("gemini", "gemini-3.5-flash-lite", "$0.055")]


def _totals(provider: str, model: str, *, completeness: str = "measured") -> ProviderUsageTotals:
    return ProviderUsageTotals().add_observation(
        UsageObservation(
            id="usage",
            revision=1,
            session=ResourceRef(
                id="session",
                kind="session",
                provider=provider,
                account_scope_id="account",
            ),
            model={"provider": provider, "id": model},
            grain="model_request",
            basis="increment",
            input_tokens=100_000,
            input_cached_tokens=0,
            output_tokens=10_000,
            completeness=completeness,
            observed_at=datetime.now(UTC),
        )
    )


async def _footer(platform: str, totals: ProviderUsageTotals, markup: Decimal = Decimal(1)) -> str:
    """The text the platform's finished-turn footer carries for the turn's cost."""
    state = TurnState(usage_totals=totals)
    common: dict[str, Any] = {"agent_name": "Agent", "markup": markup, "model_id": "m"}
    if platform == "teams":
        teams = TeamsTurnLifecycle(
            sender=None,
            conversation_id="c",
            service_url=None,
            cancel_key="k",
            **common,
        )
        return await teams._summary(state) or ""
    if platform == "discord":
        lifecycle: DiscordTurnLifecycle | SlackTurnLifecycle = DiscordTurnLifecycle(
            send=None,
            edit=None,
            **common,
        )
    else:
        lifecycle = SlackTurnLifecycle(
            client=None, channel="C", thread_ts="1.0", cancel=asyncio.Event(), author_id="U",
            register=lambda *a: None, deregister=lambda *a: None, **common,
        )  # fmt: skip
    lifecycle._apply_usage(state)
    return lifecycle._state.cost_str or ""


@pytest.mark.parametrize("platform", ["discord", "slack", "teams"])
@pytest.mark.parametrize(("provider", "model", "cost"), _PRICED)
async def test_priced_provider_turn_footer_shows_the_debit_on_every_platform(
    platform: str, provider: str, model: str, cost: str
) -> None:
    footer = await _footer(platform, _totals(provider, model))
    assert cost in footer, f"{platform} footer should show {cost}, got {footer!r}"


@pytest.mark.parametrize("platform", ["discord", "slack", "teams"])
async def test_provider_turn_footer_shows_the_marked_up_debit(platform: str) -> None:
    footer = await _footer(platform, _totals("openai", "gpt-6-luna"), Decimal(2))
    assert "$0.03" in footer, f"{platform} footer should show the marked-up debit, got {footer!r}"


@pytest.mark.parametrize("platform", ["discord", "slack", "teams"])
@pytest.mark.parametrize(
    "totals",
    [
        _totals("openai", "gpt-unpriced"),
        _totals("openai", "gpt-6-luna", completeness="partial"),
        _totals("selfhosted", "gpt-6-luna"),
        ProviderUsageTotals(),
    ],
    ids=["unpriced model", "partial reading", "unknown infrastructure", "no usage"],
)
async def test_provider_turn_footer_omits_a_cost_it_cannot_settle(
    platform: str, totals: ProviderUsageTotals
) -> None:
    footer = await _footer(platform, totals)
    assert "$" not in footer, f"{platform} footer must not guess a cost, got {footer!r}"
