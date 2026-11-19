"""Executable record: a GPT or Gemini turn shows the same debit in every chat footer.

Discord, Slack and Teams read one core rule (`LATE_TURN_COSTS.turn_debit`), the pricing
the usage outbox settles with, markup included. An unpriced model, a partial reading or
usage not reported yet shows no cost. A GPT turn's usage reported after the turn ended
is edited into the footer it posted. Deliberate gaps: web chat renders no per-turn cost,
and the scheduler's durable re-reads run in another process, with no footer to edit.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import itertools
import textwrap
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from daimon.adapters.discord.lifecycle import DiscordTurnLifecycle
from daimon.adapters.slack.lifecycle import SlackTurnLifecycle
from daimon.adapters.teams.lifecycle import TeamsTurnLifecycle
from daimon.core.pricing import provider_prices
from daimon.core.provider_turn_cost import LATE_TURN_COSTS
from daimon.core.turn import openai_host, openai_late_usage
from daimon.core.turn.state import TextBlock, TurnState
from daimon.core.usage_totals import ProviderUsageTotals
from microsoft_teams.api import MessageActivityInput, SentActivity
from mux.contracts.ids import ResourceRef
from mux.contracts.usage import UsageObservation
from mux.profiles.openai import PERSISTENT_WORKSPACE

# Observation rates per 1M tokens: input 0.10, output 0.50 (gpt-6-luna);
# input 0.30, output 2.50 (gemini-3.5-flash-lite). 100k in and 10k out cost 0.015 and 0.055.
_PRICED = [("openai", "gpt-6-luna", "$0.015"), ("gemini", "gemini-3.5-flash-lite", "$0.055")]
_PLATFORMS = ["discord", "slack", "teams"]
_ROOT = Path(__file__).resolve().parents[2]
_ids = itertools.count()

#: Paths that never edit a late GPT debit into a footer, and why.
DELIBERATE_GAPS = {
    "web chat": "renders no per-turn cost",
    "scheduler durable re-reads": "another process, which holds no footer to edit",
}


def _observation(
    provider: str,
    model: str,
    *,
    completeness: str = "measured",
    revision: int = 1,
    final: bool = False,
    session: str = "session",
) -> UsageObservation:
    measured = completeness != "unknown"
    return UsageObservation(
        id="usage",
        revision=revision,
        session=ResourceRef(
            id=session, kind="session", provider=provider, account_scope_id="account"
        ),
        model={"provider": provider, "id": model},
        turn_id="turn",
        grain="turn",
        basis="cumulative",
        input_tokens=100_000 if measured else None,
        input_cached_tokens=0 if measured else None,
        output_tokens=10_000 if measured else None,
        completeness=completeness,
        observed_at=datetime.now(UTC),
        final=final,
    )


def _totals(provider: str, model: str, *, completeness: str = "measured") -> ProviderUsageTotals:
    return ProviderUsageTotals().add_observation(
        _observation(provider, model, completeness=completeness)
    )


def _common(markup: Decimal = Decimal(1)) -> dict[str, Any]:
    return {"agent_name": "Agent", "markup": markup, "model_id": "m"}


async def _footer(platform: str, totals: ProviderUsageTotals, markup: Decimal = Decimal(1)) -> str:
    """The text the platform's finished-turn footer carries for the turn's cost."""
    state = TurnState(usage_totals=totals)
    if platform == "teams":
        teams = TeamsTurnLifecycle(
            sender=None, conversation_id="c", service_url=None, cancel_key="k", **_common(markup)
        )
        return await teams._summary(state) or ""
    if platform == "discord":
        lifecycle: DiscordTurnLifecycle | SlackTurnLifecycle = DiscordTurnLifecycle(
            send=None, edit=None, **_common(markup)
        )
    else:
        lifecycle = SlackTurnLifecycle(
            client=None, channel="C", thread_ts="1.0", cancel=asyncio.Event(), author_id="U",
            register=lambda *a: None, deregister=lambda *a: None, **_common(markup),
        )  # fmt: skip
    lifecycle._apply_usage(state)
    return lifecycle._state.cost_str or ""


@pytest.mark.parametrize("platform", _PLATFORMS)
@pytest.mark.parametrize(("provider", "model", "cost"), _PRICED)
async def test_priced_provider_turn_footer_shows_the_debit_on_every_platform(
    platform: str, provider: str, model: str, cost: str
) -> None:
    footer = await _footer(platform, _totals(provider, model))
    assert cost in footer, f"{platform} footer should show {cost}, got {footer!r}"


@pytest.mark.parametrize("platform", _PLATFORMS)
async def test_provider_turn_footer_shows_the_marked_up_debit(platform: str) -> None:
    footer = await _footer(platform, _totals("openai", "gpt-6-luna"), Decimal(2))
    assert "$0.03" in footer, f"{platform} footer should show the marked-up debit, got {footer!r}"


@pytest.mark.parametrize("platform", _PLATFORMS)
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


class _Slack:
    """Records Slack posts and updates; each post gets the next ts."""

    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.writes.append(kwargs)
        return {"ts": f"1.{len(self.writes)}"}

    async def chat_update(self, **kwargs: Any) -> dict[str, str]:
        self.writes.append(kwargs)
        return {"ts": kwargs["ts"]}


class _Teams:
    async def send(
        self, conversation_id: str, activity: MessageActivityInput, *, service_url: str | None
    ) -> SentActivity:
        self.writes.append(activity.model_dump_json())
        return SentActivity(id=activity.id or f"m-{len(self.writes)}", activity_params=activity)

    def __init__(self) -> None:
        self.writes: list[str] = []


async def _finish_turn(platform: str, totals: ProviderUsageTotals) -> Callable[[], list[str]]:
    """Run a finished GPT turn through the platform; reads every write it made, as text."""
    state = TurnState(content=[TextBlock(kind="text", text="The answer.")], usage_totals=totals)
    if platform == "discord":
        writes: list[Any] = []

        async def send(**kwargs: Any) -> SimpleNamespace:
            writes.append(kwargs)
            return SimpleNamespace(id=next(_ids))

        async def edit(message: object, **kwargs: Any) -> None:
            writes.append(kwargs)

        discord = DiscordTurnLifecycle(send=send, edit=edit, in_dm=False, **_common())
        await discord.on_terminal_success(state)
        return lambda: [
            " ".join(str(embed.footer.text) for embed in write.get("embeds") or [])
            for write in writes
        ]
    if platform == "slack":
        client = _Slack()
        slack = SlackTurnLifecycle(
            client=client, channel="C", thread_ts="1.0", cancel=asyncio.Event(), author_id="U",
            register=lambda *a: None, deregister=lambda *a: None, **_common(),
        )  # fmt: skip
        await slack.on_terminal_success(state)
        return lambda: [str(write["blocks"]) for write in client.writes]
    sender = _Teams()
    teams = TeamsTurnLifecycle(
        sender=sender, conversation_id="c", service_url=None, cancel_key="k", **_common()
    )
    await teams.on_terminal_success(state)
    return lambda: list(sender.writes)


def _settle(observation: UsageObservation) -> None:
    """What the GPT recorder hands over once a revision of the turn's usage settled."""
    LATE_TURN_COSTS.settle(
        observation,
        price=provider_prices(PERSISTENT_WORKSPACE.profile_id).get(("openai", "gpt-6-luna")),
        infrastructure_usd=Decimal(0),
        markup=Decimal(1),
    )


@pytest.mark.parametrize("platform", _PLATFORMS)
async def test_late_gpt_usage_is_edited_into_the_footer_on_every_platform(platform: str) -> None:
    """Usage unknown at the end shows no cost; its measured revision edits the debit in."""
    session = f"late-{platform}"
    unknown = _observation("openai", "gpt-6-luna", completeness="unknown", session=session)
    writes = await _finish_turn(platform, ProviderUsageTotals().add_observation(unknown))
    ended = writes()
    assert ended and not any("$0.015" in write for write in ended), f"{platform}: {ended}"
    _settle(_observation("openai", "gpt-6-luna", revision=2, final=True, session=session))
    await LATE_TURN_COSTS.wait()
    late = writes()[len(ended) :]
    assert any("$0.015" in write for write in late), f"{platform} never edited it in: {late}"


@pytest.mark.parametrize("platform", _PLATFORMS)
async def test_usage_settled_before_the_footer_renders_is_shown_at_once(platform: str) -> None:
    """A re-read can beat the terminal render: the footer then takes that debit."""
    session = f"early-{platform}"
    unknown = _observation("openai", "gpt-6-luna", completeness="unknown", session=session)
    _settle(_observation("openai", "gpt-6-luna", revision=2, final=True, session=session))
    shown = (await _finish_turn(platform, ProviderUsageTotals().add_observation(unknown)))()
    assert any("$0.015" in write for write in shown), f"{platform} footer missed it: {shown}"


def _calls(source: str, name: str) -> bool:
    return any(
        isinstance(node, ast.Attribute) and node.attr == name
        for node in ast.walk(ast.parse(textwrap.dedent(source)))
    )


def test_every_chat_footer_takes_the_late_debit_and_only_in_process_rereads_settle_it() -> None:
    """Found from the code: each lifecycle pricing a turn reads the one core rule."""
    readers = {
        path.parts[path.parts.index("adapters") + 1]
        for path in (_ROOT / "packages").rglob("*.py")
        if "tests" not in path.parts and _calls(path.read_text(), "turn_debit")
    }
    assert readers == set(_PLATFORMS), f"footers reading the late debit: {readers}"
    assert _calls(inspect.getsource(openai_host), "settle"), "the GPT recorder must settle it"
    durable = inspect.getsource(openai_late_usage._reread)
    assert not _calls(durable, "settle"), DELIBERATE_GAPS["scheduler durable re-reads"]
