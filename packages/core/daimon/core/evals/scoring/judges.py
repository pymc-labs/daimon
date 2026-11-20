"""Model judges: rubric, pairwise and reference-guided, with panels and metered cost.

A judge never sees which arm or model wrote an answer. Every call is planned first
(`plan_calls`), so the estimate and the run price the same prompts. Scores get stable ids:
judging a run again replaces its earlier verdicts instead of piling up.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from itertools import combinations
from typing import Final, Literal, Protocol

from daimon.core.evals.blind import mask_text
from daimon.core.evals.format import (
    BlobRef,
    Case,
    Estimate,
    EstimateLine,
    Grader,
    ItemPair,
    JudgeConfig,
    ModelChoice,
    RubricItem,
    RubricScale,
    Run,
    RunItem,
    Score,
    ScoreValue,
    ToolAlias,
)
from daimon.core.evals.scoring.checks import bot_events, bot_messages, llm_checks, pass_value
from daimon.core.pricing import ModelRates, side_call_rates
from pydantic import JsonValue, TypeAdapter, ValidationError

MAX_TOKENS: Final = 1024
EXPECTED_OUTPUT_TOKENS: Final = 300
BATCH_DISCOUNT: Final = 0.5
CONCURRENCY: Final = 6
_SECTION_CHARS: Final = 8000
_JSON: Final = re.compile(r"\{.*\}", re.DOTALL)
_OBJECT: Final = TypeAdapter(dict[str, JsonValue])
_SEES_FOR: Final[dict[str, str]] = {"world": "world_calls"}


class JudgeUnpriced(Exception):
    """A judge model has no price today; a judge call never runs unmetered."""


@dataclass(frozen=True)
class Completion:
    text: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    refused: bool = False


class JudgeClient(Protocol):
    async def complete(
        self, model: ModelChoice, system: str, prompt: str, max_tokens: int
    ) -> Completion: ...


class JudgeBatch(Protocol):
    """A provider's batch API: answers in request order, at a discount, later."""

    async def complete_batch(
        self, model: ModelChoice, requests: Sequence[tuple[str, str, int]]
    ) -> list[Completion]: ...


@dataclass(frozen=True)
class JudgeDeps:
    client: JudgeClient
    load_text: Callable[[BlobRef], Awaitable[str | None]]
    """Text of a blob (file text, reference files); None when it is not there."""
    batch: JudgeBatch | None = None
    interactive: bool = False
    """False and `batch` set: calls go through the batch API."""
    now: Callable[[], datetime] = lambda: datetime.now(UTC)


@dataclass
class JudgeMeter:
    """What judging has cost so far; calls stop once `cap_usd` is reached."""

    cap_usd: float | None = None
    spent_usd: float = 0.0
    skipped: int = 0
    by_model: dict[str, float] = field(default_factory=lambda: {})

    @property
    def has_room(self) -> bool:
        return self.cap_usd is None or self.spent_usd < self.cap_usd


@dataclass(frozen=True)
class PlannedCall:
    judge_id: str
    model: ModelChoice
    system: str
    prompt: str
    criterion: str
    kind: Literal["binary", "scale", "preference"]
    item_id: str | None = None
    pair: ItemPair | None = None
    swapped: bool = False
    scale: RubricScale | None = None


# ---------- prompts ----------

_BINARY = (
    "You grade one answer from an AI assistant against one question. Write a short critique "
    "first, then the verdict. Answer unknown when the material does not let you decide. Reply "
    'with one JSON object and nothing else: {"critique": string, "verdict": "yes" | "no" | '
    '"unknown"}.'
)
_SCALE = (
    "You grade one answer from an AI assistant against one question on a scale. Write a short "
    "critique first, then the score. Answer null when the material does not let you decide. "
    'Reply with one JSON object and nothing else: {"critique": string, "score": integer | null}.'
)
_PAIR = (
    "You compare two answers from AI assistants to the same request. Write a short critique "
    "first, then the verdict. Do not favour an answer for its position or its length. Reply "
    'with one JSON object and nothing else: {"critique": string, "verdict": "A" | "B" | "tie"}.'
)


def _clip(text: str) -> str:
    return text if len(text) <= _SECTION_CHARS else text[:_SECTION_CHARS] + "\n[cut]"


def _history(case: Case) -> str:
    names = {p.id: p.display_name for p in case.surface.participants}
    names[case.surface.bot.id] = case.surface.bot.display_name
    lines = [f"{names.get(m.author, m.author)}: {m.text or ''}" for m in case.history]
    lines += [
        f"{names.get(m.author, m.author)} (the request): {m.text or ''}" for m in case.turn.messages
    ]
    return "\n".join(lines)


def _chat(case: Case, item: RunItem) -> str:
    lines: list[str] = []
    for event in bot_events(case, item, ("message", "edit", "card", "reaction_add", "file")):
        if event.kind in ("message", "edit") and event.text:
            lines.append(f"message: {event.text}")
        elif event.kind == "card" and event.card is not None:
            card = event.card
            buttons = ", ".join(b.label for b in card.buttons or [])
            lines.append(f"card ({card.kind}): {card.headline or ''} {card.body or ''} [{buttons}]")
        elif event.kind == "reaction_add":
            lines.append(f"reaction: {event.emoji}")
        elif event.kind == "file":
            lines.append(f"file: {', '.join(event.files or [])}")
    return "\n".join(lines) or "(nothing)"


def _tools(item: RunItem) -> str:
    events = item.transcript.events if item.transcript else None
    if events is None:
        return "(not available)"
    origins = {e.call_id: e.result.origin for e in events if e.kind == "tool_result" and e.result}
    lines: list[str] = []
    for event in events:
        if event.kind == "tool_call" and event.tool is not None:
            note = " [answer was simulated]" if origins.get(event.call_id) == "simulated" else ""
            args = json.dumps(event.tool.input, sort_keys=True, default=str)[:500]
            lines.append(f"{event.tool.name} {args}{note}")
    return "\n".join(lines) or "(none)"


def _world(item: RunItem) -> str:
    lines = [
        f"{w.service or w.surface} {w.tool or ''} -> {w.origin}"
        + (" [simulated]" if w.origin == "simulated" else "")
        for w in item.world_calls or []
    ]
    return "\n".join(lines) or "(none)"


async def _files(deps: JudgeDeps, item: RunItem) -> str:
    parts: list[str] = []
    for output in item.outputs or []:
        text = await deps.load_text(output.text) if output.text else None
        parts.append(f"{output.name} ({output.media_type}):\n{text if text else '(no text)'}")
    return "\n\n".join(parts) or "(none)"


async def render_item(deps: JudgeDeps, case: Case, item: RunItem, sections: Sequence[str]) -> str:
    """What a judge reads of one item, with model names masked."""
    blocks: list[str] = []
    for section in sections:
        match section:
            case "history":
                body = _history(case)
            case "reply":
                messages = bot_messages(case, item)
                body = messages[-1][1] if messages else "(no reply)"
            case "chat":
                body = _chat(case, item)
            case "files":
                body = await _files(deps, item)
            case "tool_calls":
                body = _tools(item)
            case "world_calls":
                body = _world(item)
            case _:
                continue
        blocks.append(f"## {section}\n{_clip(mask_text(body))}")
    return "\n\n".join(blocks)


async def _reference(deps: JudgeDeps, case: Case, baseline: RunItem | None) -> str | None:
    ref = case.expect.reference
    parts: list[str] = []
    if ref is not None:
        parts += [t for t in (ref.text, ref.note) if t]
        for file in ref.files or []:
            text = await deps.load_text(file)
            if text:
                parts.append(f"{file.name or 'file'}:\n{text}")
    if not parts and baseline is not None:
        messages = bot_messages(case, baseline)
        if messages:
            parts.append(messages[-1][1])
    return _clip(mask_text("\n\n".join(parts))) if parts else None


# ---------- planning ----------


def _sections(config: JudgeConfig, applies: Sequence[str] | None) -> list[str]:
    allowed = {_SEES_FOR.get(a, a) for a in applies or []}
    return [
        s for s in config.sees
        if applies is None or s in allowed or s in ("history", "reference")
    ]  # fmt: skip


def _system(base: str, config: JudgeConfig, scale: RubricScale | None = None) -> str:
    text = base
    if scale is not None:
        anchors = "; ".join(f"{k} = {v}" for k, v in (scale.anchors or {}).items())
        text += f" The score runs from {scale.min} to {scale.max}" + (
            f" ({anchors})." if anchors else "."
        )
    return f"{text}\n\n{config.instructions}" if config.instructions else text


def _item_calls(
    config: JudgeConfig, model: ModelChoice, item: RunItem, rubric: RubricItem,
    context: str, reference: str | None,
) -> PlannedCall:  # fmt: skip
    scale = rubric.scale if rubric.kind == "scale" else None
    ref = f"\n\n## reference\n{reference}" if reference else ""
    return PlannedCall(
        judge_id=config.id, model=model,
        system=_system(_SCALE if scale else _BINARY, config, scale),
        prompt=f"{context}{ref}\n\n## question\n{rubric.criterion}", criterion=rubric.id,
        kind="scale" if scale else "binary", item_id=item.id, scale=scale,
    )  # fmt: skip


async def plan_calls(
    deps: JudgeDeps,
    run: Run,
    config: JudgeConfig,
    items: Sequence[RunItem],
    cases: Mapping[str, Case],
    aliases: Sequence[ToolAlias] = (),
) -> list[PlannedCall]:
    """Every call this judge makes on these items, in a stable order."""
    done = [i for i in items if i.status == "done" and i.case.id in cases]
    if config.kind == "pairwise":
        return await _pair_calls(deps, run, config, done, cases)
    kinds = {a.id: a.kind for a in run.arms}
    baselines = {(i.case.id, i.repeat): i for i in done if kinds.get(i.arm_id) == "baseline"}
    calls: list[PlannedCall] = []
    for item in done:
        case = cases[item.case.id]
        reference = None
        if config.kind == "reference" or "reference" in config.sees:
            own = (
                None
                if kinds.get(item.arm_id) == "baseline"
                else baselines.get((case.id, item.repeat))
            )
            reference = await _reference(deps, case, own)
            if config.kind == "reference" and reference is None:
                continue
        for rubric in case.expect.rubric or []:
            if "llm" not in rubric.graders:
                continue
            context = await render_item(deps, case, item, _sections(config, rubric.applies_to))
            calls += [
                _item_calls(config, m, item, rubric, context, reference) for m in config.models
            ]
        asked = llm_checks(case, item, aliases)
        if asked:
            history = await render_item(deps, case, item, ["history"])
            for ask in asked:
                value = mask_text(json.dumps(ask.subject, ensure_ascii=False, default=str))
                rubric = RubricItem(id=ask.criterion, criterion=ask.question)
                body = f"{history}\n\n## value\n{_clip(value)}"
                calls += [_item_calls(config, m, item, rubric, body, None) for m in config.models]
    return calls


async def _pair_calls(
    deps: JudgeDeps, run: Run, config: JudgeConfig, items: Sequence[RunItem],
    cases: Mapping[str, Case],
) -> list[PlannedCall]:  # fmt: skip
    by_key: dict[tuple[str, int, str], RunItem] = {
        (i.case.id, i.repeat, i.arm_id): i for i in items
    }
    calls: list[PlannedCall] = []
    sections = [s for s in config.sees if s != "reference"]
    for arm_a, arm_b in combinations([a.id for a in run.arms], 2):
        for (case_id, repeat, arm), first in sorted(by_key.items()):
            second = by_key.get((case_id, repeat, arm_b))
            if arm != arm_a or second is None:
                continue
            case = cases[case_id]
            texts = {
                i.id: await render_item(deps, case, i, [s for s in sections if s != "history"])
                for i in (first, second)
            }
            history = (
                await render_item(deps, case, first, ["history"]) if "history" in sections else ""
            )
            for swapped in (False, True) if config.swap_positions else (False,):
                shown = (second, first) if swapped else (first, second)
                prompt = (
                    f"{history}\n\n# Answer A\n{texts[shown[0].id]}"
                    f"\n\n# Answer B\n{texts[shown[1].id]}"
                    "\n\n## question\nWhich answer is better for this request?"
                )
                calls += [
                    PlannedCall(
                        judge_id=config.id,
                        model=m,
                        system=_system(_PAIR, config),
                        prompt=prompt,
                        criterion="preference",
                        kind="preference",
                        pair=ItemPair(a=first.id, b=second.id),
                        swapped=swapped,
                    )
                    for m in config.models
                ]
    return calls


# ---------- cost ----------


def rates_for(model: ModelChoice, prompt_tokens: int = 0) -> ModelRates:
    try:
        return side_call_rates(model.model, prompt_tokens)
    except KeyError:
        raise JudgeUnpriced(model.model) from None


def cost_usd(model: ModelChoice, completion: Completion, *, batch: bool = False) -> float:
    prompt = completion.input_tokens + completion.cache_read_tokens
    rates = rates_for(model, prompt)
    cost = (
        completion.input_tokens * rates.input
        + completion.cache_read_tokens * rates.cache_read
        + completion.output_tokens * rates.output
    ) / 1_000_000
    return cost * BATCH_DISCOUNT if batch else cost


def _planned_cost(call: PlannedCall, *, batch: bool) -> float:
    guess = Completion("", (len(call.system) + len(call.prompt)) // 4, EXPECTED_OUTPUT_TOKENS)
    return cost_usd(call.model, guess, batch=batch)


def estimate(
    calls: Sequence[PlannedCall], *, batch: bool = False, at: datetime | None = None
) -> Estimate:
    """What these calls should cost: low is half, high double, the way defaults are bounded."""
    totals: dict[tuple[str, str], list[float]] = {}
    for call in calls:
        entry = totals.setdefault((call.judge_id, call.model.model), [0, 0.0])
        entry[0] += 1
        entry[1] += _planned_cost(call, batch=batch)
    expected = sum(v[1] for v in totals.values())
    lines = [
        EstimateLine(
            category="judge", model=model, items=int(n), expected_usd=round(usd, 6), basis="default"
        )
        for (_, model), (n, usd) in sorted(totals.items())
    ]
    return Estimate(
        low_usd=expected / 2, expected_usd=expected, high_usd=expected * 2,
        at=at or datetime.now(UTC), lines=lines,
    )  # fmt: skip


# ---------- parsing and running ----------


def _verdict(call: PlannedCall, text: str) -> tuple[ScoreValue, str | None]:
    unknown = ScoreValue(unknown=True)
    match = _JSON.search(text)
    try:
        data = _OBJECT.validate_json(match.group(0)) if match else None
    except ValidationError:
        data = None
    if data is None:
        return unknown, "the judge's answer could not be read"
    critique = data.get("critique")
    why = critique if isinstance(critique, str) else None
    if call.kind == "scale":
        value = data.get("score")
        scale = call.scale
        if (
            not isinstance(value, int | float)
            or isinstance(value, bool)
            or scale is None
            or not scale.min <= value <= scale.max
        ):
            return unknown, why
        return ScoreValue(score=float(value)), why
    word = str(data.get("verdict", "")).strip().lower()
    if call.kind == "preference":
        shown = {"a": "a", "b": "b", "tie": "tie"}.get(word)
        if shown is None:
            return unknown, why
        if call.swapped and shown != "tie":
            shown = "b" if shown == "a" else "a"
        return ScoreValue(preference=shown), why  # pyright: ignore[reportArgumentType]
    if word in ("yes", "pass", "true"):
        return pass_value(True), why
    if word in ("no", "fail", "false"):
        return pass_value(False), why
    return unknown, why


def _score(deps: JudgeDeps, call: PlannedCall, completion: Completion, cost: float) -> Score:
    value, why = (
        (ScoreValue(unknown=True), "the judge declined to answer")
        if completion.refused
        else _verdict(call, completion.text)
    )
    subject = call.item_id or (f"{call.pair.a}|{call.pair.b}" if call.pair else "")
    key = "|".join([subject, call.criterion, call.judge_id, call.model.model, str(call.swapped)])
    return Score(
        id=f"llm-{sha256(key.encode()).hexdigest()[:24]}", item_id=call.item_id, pair=call.pair,
        criterion=call.criterion, rationale=why, swapped=call.swapped if call.pair else None,
        grader=Grader(kind="llm", model=call.model.model, judge_id=call.judge_id, version="1"),
        value=value, cost_usd=cost, at=deps.now(),
    )  # fmt: skip


async def judge(
    deps: JudgeDeps,
    run: Run,
    config: JudgeConfig,
    items: Sequence[RunItem],
    cases: Mapping[str, Case],
    *,
    aliases: Sequence[ToolAlias] = (),
    meter: JudgeMeter | None = None,
) -> list[Score]:
    """Run one judge config over finished items; one score per call, panel members apart.

    Raises `JudgeUnpriced` before any call when a panel model has no price. Calls stop when
    `meter.cap_usd` is reached; those left are counted in `meter.skipped`.
    """
    meter = meter or JudgeMeter()
    for model in config.models:
        rates_for(model)
    calls = await plan_calls(deps, run, config, items, cases, aliases)
    if deps.batch is not None and not deps.interactive:
        return await _run_batched(deps, deps.batch, calls, meter)
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(call: PlannedCall) -> Score | None:
        async with gate:
            if not meter.has_room:
                meter.skipped += 1
                return None
            done = await deps.client.complete(call.model, call.system, call.prompt, MAX_TOKENS)
            cost = cost_usd(call.model, done)
            _spend(meter, call, cost)
            return _score(deps, call, done, cost)

    return [s for s in await asyncio.gather(*(one(c) for c in calls)) if s is not None]


def _spend(meter: JudgeMeter, call: PlannedCall, cost: float) -> None:
    meter.spent_usd += cost
    meter.by_model[call.model.model] = meter.by_model.get(call.model.model, 0.0) + cost


async def _run_batched(
    deps: JudgeDeps, batch: JudgeBatch, calls: Sequence[PlannedCall], meter: JudgeMeter
) -> list[Score]:
    """One batch per model; only as many calls as the cap's room pays for at the estimate."""
    chosen: list[PlannedCall] = []
    planned = meter.spent_usd
    for call in calls:
        planned += _planned_cost(call, batch=True)
        if meter.cap_usd is not None and planned > meter.cap_usd:
            meter.skipped += 1
        else:
            chosen.append(call)
    scores: list[Score] = []
    for model in {c.model.model: c.model for c in chosen}.values():
        group = [c for c in chosen if c.model == model]
        answers = await batch.complete_batch(
            model, [(c.system, c.prompt, MAX_TOKENS) for c in group]
        )
        for call, done in zip(group, answers, strict=True):
            cost = cost_usd(model, done, batch=True)
            _spend(meter, call, cost)
            scores.append(_score(deps, call, done, cost))
    return scores


# ---------- panels ----------


def vote(
    values: Sequence[bool | None], aggregate: Literal["majority", "mean", "all"]
) -> bool | None:
    """A panel's verdict on a yes/no question; None (unknown) when it cannot decide."""
    cast = [v for v in values if v is not None]
    if not cast:
        return None
    yes = sum(cast)
    if aggregate == "all":
        return yes == len(cast)
    if yes * 2 == len(cast):
        return True if aggregate == "mean" else None
    return yes * 2 > len(cast)


def pair_preference(scores: Sequence[Score]) -> Literal["a", "b", "tie"] | None:
    """A panel's preference between two items, from every score on that pair.

    Each model's two orders must agree, else it counts as a tie; models then vote.
    """
    per_model: dict[str, list[str]] = {}
    for score in scores:
        if score.value.preference is not None:
            per_model.setdefault(score.grader.model or "", []).append(score.value.preference)
    if not per_model:
        return None
    verdicts = [v[0] if len(set(v)) == 1 else "tie" for v in per_model.values()]
    ranked = Counter(verdicts).most_common()
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return "tie"
    return ranked[0][0]  # pyright: ignore[reportReturnType]
