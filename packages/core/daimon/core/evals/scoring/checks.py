"""Deterministic checks: one `Score` per expectation, free to run on every item.

Calls and effects come from `canonical_calls` (tool aliases applied); chat expectations are
read from the item's chat events and outputs. A question for a judge (`op: llm`) never fails
here: it is listed by `llm_checks` and scored by `judges`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from hashlib import sha256
from typing import Final

from daimon.core.evals.effects import (
    CanonicalCall,
    canonical_calls,
    glob_matches,
    matches,
    pointer_get,
)
from daimon.core.evals.format import (
    Case,
    ChatEvent,
    Check,
    Effect,
    ExpectedCall,
    ExpectedEffect,
    ExpectedOutput,
    Grader,
    MatchMode,
    RunItem,
    Score,
    ScoreValue,
    ToolAlias,
    to_document,
)
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import JsonValue

WorldState = Mapping[tuple[str, str], JsonValue]
"""The item's world after the run: `(service, key)` to the resource's data."""
CODE: Final = Grader(kind="code", version="1")
_SPACE: Final = re.compile(r"\s+")
_ALWAYS_HARD: Final = ("status", "forbidden_calls", "forbidden_effects")
MAX_PATTERN: Final = 500
MAX_SUBJECT: Final = 100_000
"""Characters a pattern is matched against; the rest is not searched."""
_NESTED: Final = re.compile(r"\((?:[^()\\]|\\.)*[+*}](?:[^()\\]|\\.)*\)\s*[+*{]")
_BACKREF: Final = re.compile(r"\\[1-9]|\(\?P=")


def pattern_problem(pattern: str) -> str | None:
    """Why a case's `regex` pattern can't be used, or None. Repeated groups that repeat
    inside and back references are refused: they can take exponential time."""
    if len(pattern) > MAX_PATTERN:
        return f"is longer than {MAX_PATTERN} characters"
    try:
        re.compile(pattern)
    except re.error as exc:
        return f"is not a valid pattern ({exc})"
    if _NESTED.search(pattern) or _BACKREF.search(pattern):
        return "repeats a repeated group or refers back, which can run too long"
    return None


def regex_problems(node: JsonValue) -> list[str]:
    """Problems of every `regex` check in a case document (or part of one)."""
    if isinstance(node, list):
        return [p for v in node for p in regex_problems(v)]
    if not isinstance(node, dict):
        return []
    found = [p for v in node.values() for p in regex_problems(v)]
    pattern = node.get("pattern")
    if node.get("op") == "regex" and isinstance(pattern, str):
        problem = pattern_problem(pattern)
        found += [f"pattern {pattern!r} {problem}"] if problem else []
    return found


def pass_value(passed: bool) -> ScoreValue:
    return ScoreValue.model_validate({"pass": passed})


@dataclass(frozen=True)
class LlmCheck:
    """A `llm` check waiting for a judge: the criterion path, the question and what it is about."""

    criterion: str
    question: str
    subject: JsonValue


def is_hard(case: Case, criterion: str) -> bool:
    """Whether a check failure fails the item under `pass_rule.checks: hard_only`."""
    head, _, rest = criterion.partition("/")
    index = rest.split("/")[0]
    expect = case.expect
    pool: Sequence[ExpectedCall | ExpectedEffect | ExpectedOutput] = ()
    if head in _ALWAYS_HARD or index in ("extra", "order"):
        return True
    if head == "tool_calls" and expect.tool_calls:
        pool = expect.tool_calls.calls
    elif head == "effects" and expect.effects:
        pool = expect.effects.items
    elif head == "outputs":
        pool = expect.outputs or ()
    elif head == "world":
        pool_hard = [w.hard for w in expect.world or []]
        return pool_hard[int(index)] if index.isdigit() and int(index) < len(pool_hard) else True
    elif head == "limits":
        return False
    return pool[int(index)].hard if index.isdigit() and int(index) < len(pool) else True


# ---------- one value against one Check ----------


def _normal(value: JsonValue) -> JsonValue:
    if isinstance(value, str):
        text = _SPACE.sub(" ", value.strip()).casefold()
        try:
            return float(text.replace(",", ""))
        except ValueError:
            return text
    if isinstance(value, bool):
        return value
    return float(value) if isinstance(value, int | float) else value


def _text(value: JsonValue) -> str:
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True)


def check_value(check: Check, found: bool, value: JsonValue) -> bool:
    """Whether a value passes a deterministic check; `llm` passes (a judge answers it)."""
    match check.op:
        case "llm":
            return True
        case "exists":
            return found
        case "not_contains":
            return not found or not _contains(value, check.value)
        case _ if not found:
            return False
        case "exact":
            return value == check.value
        case "normalized":
            return _normal(value) == _normal(check.value)
        case "regex":
            pattern = check.pattern
            if pattern is None or pattern_problem(pattern):
                return False
            return re.search(pattern, _text(value)[:MAX_SUBJECT]) is not None
        case "contains":
            return _contains(value, check.value)
        case "one_of":
            return value in (check.values or [])
        case "range":
            if not isinstance(value, int | float) or isinstance(value, bool):
                return False
            return (check.min is None or value >= check.min) and (
                check.max is None or value <= check.max
            )
        case "schema":
            try:
                return not list(Draft202012Validator(check.schema_ or {}).iter_errors(value))
            except SchemaError:
                return False


def _contains(value: JsonValue, needle: JsonValue) -> bool:
    if isinstance(value, str):
        return isinstance(needle, str) and needle in value
    if isinstance(value, list):
        return needle in value
    return isinstance(value, dict) and isinstance(needle, str) and needle in value


def _args_ok(
    checks: Mapping[str, Check] | None, args: JsonValue, path: str, pending: list[LlmCheck] | None
) -> bool:
    ok = True
    for pointer, check in (checks or {}).items():
        found, value = pointer_get(args, pointer)
        if check.op == "llm" and pending is not None and found:
            pending.append(LlmCheck(f"{path}/args{pointer}", check.criterion or "", value))
        ok = ok and check_value(check, found, value)
    return ok


# ---------- the run of checks over one item ----------


@dataclass
class _Checker:
    case: Case
    item: RunItem
    at: datetime
    scores: list[Score] = field(default_factory=lambda: [])
    pending: list[LlmCheck] = field(default_factory=lambda: [])

    def add(self, criterion: str, passed: bool | None, why: str) -> None:
        key = sha256(f"{self.item.id}|{criterion}".encode()).hexdigest()[:24]
        value = pass_value(passed) if passed is not None else ScoreValue(unknown=True)
        self.scores.append(
            Score(
                id=f"code-{key}", item_id=self.item.id, criterion=criterion, grader=CODE,
                value=value, rationale=why, at=self.at,
            )
        )  # fmt: skip


def _count_ok(count: int, low: int, high: int | None) -> bool:
    return count >= low and (high is None or count <= high)


def _span(low: int, high: int | None) -> str:
    return f"{low}" if low == high else f"{low}-{'any' if high is None else high}"


def _call_checks(
    checker: _Checker,
    head: str,
    mode: MatchMode,
    expected: Sequence[ExpectedCall],
    calls: Sequence[CanonicalCall],
) -> None:
    first_index: list[int] = []
    unmatched: list[str] = []
    for call in calls:
        owners = [
            i for i, e in enumerate(expected)
            if matches(e.match, call) and _args_ok(e.args, call.args, "", None)
        ]  # fmt: skip
        if owners:
            first_index.append(owners[0])
        else:
            unmatched.append(call.tool or call.surface or "call")
    for i, item in enumerate(expected):
        path = f"{head}/{i}"
        hits = [
            c for c in calls
            if matches(item.match, c) and _args_ok(item.args, c.args, path, None)
        ]  # fmt: skip
        if hits:
            _args_ok(item.args, hits[0].args, path, checker.pending)
        low = 0 if mode == "superset" else item.min
        checker.add(
            path, _count_ok(len(hits), low, item.max),
            f"{len(hits)} matching, expected {_span(low, item.max)}",
        )  # fmt: skip
    _mode_checks(checker, head, mode, first_index, unmatched)


def _mode_checks(
    checker: _Checker, head: str, mode: MatchMode, order: Sequence[int], unmatched: Sequence[str]
) -> None:
    if mode != "subset":
        checker.add(
            f"{head}/extra", not unmatched,
            "nothing beyond the expected"
            if not unmatched
            else f"unexpected: {', '.join(unmatched)}",
        )  # fmt: skip
    if mode == "strict":
        ordered = all(a <= b for a, b in zip(order, order[1:], strict=False))
        checker.add(
            f"{head}/order", ordered, "in the expected order" if ordered else "out of order"
        )


def _effect_checks(checker: _Checker, calls: Sequence[CanonicalCall]) -> None:
    expect = checker.case.expect
    effects: list[Effect] = [e for call in calls for e in call.effects]
    if expect.effects:
        spec = expect.effects
        first_index: list[int] = []
        unmatched: list[str] = []
        for effect in effects:
            owners = [
                i for i, e in enumerate(spec.items)
                if _effect_fits(e, effect) and _args_ok(e.args, effect.args, "", None)
            ]  # fmt: skip
            if owners:
                first_index.append(owners[0])
            else:
                unmatched.append(effect.verb)
        for i, item in enumerate(spec.items):
            path = f"effects/{i}"
            hits = [
                e for e in effects
                if _effect_fits(item, e) and _args_ok(item.args, e.args, path, None)
            ]  # fmt: skip
            if hits:
                _args_ok(item.args, hits[0].args, path, checker.pending)
            low = 0 if spec.mode == "superset" else item.min
            checker.add(
                path, _count_ok(len(hits), low, item.max),
                f"{len(hits)} matching, expected {_span(low, item.max)}",
            )  # fmt: skip
        _mode_checks(checker, "effects", spec.mode, first_index, unmatched)
    for i, pattern in enumerate(expect.forbidden_effects or []):
        seen = [e.verb for e in effects if glob_matches(pattern, e.verb)]
        checker.add(f"forbidden_effects/{i}", not seen, f"{pattern}: {len(seen)} made")


def _effect_fits(expected: ExpectedEffect, effect: Effect) -> bool:
    return glob_matches(expected.verb, effect.verb) and (
        expected.target is None or glob_matches(expected.target, effect.target)
    )


# ---------- chat ----------


def bot_messages(case: Case, item: RunItem) -> list[tuple[str, str]]:
    """The bot's messages as the chat ended: (id, text), edits applied, deletes dropped."""
    people = {p.id for p in case.surface.participants}
    messages: dict[str, str] = {}
    for event in item.chat or []:
        if event.actor in people or event.message_id is None:
            continue
        if event.kind in ("message", "edit"):
            messages[event.message_id] = event.text or ""
        elif event.kind == "delete":
            messages.pop(event.message_id, None)
    return list(messages.items())


def bot_events(case: Case, item: RunItem, kinds: Sequence[str]) -> list[ChatEvent]:
    people = {p.id for p in case.surface.participants}
    return [e for e in item.chat or [] if e.kind in kinds and e.actor not in people]


def _pick[T](where: str, found: Sequence[T]) -> list[T]:
    if where == "final":
        return list(found[-1:])
    return list(found[:1]) if where == "first" else list(found)


def _output_hits(
    case: Case, item: RunItem, spec: ExpectedOutput, path: str, checker: _Checker
) -> int:
    kind = spec.kind
    if kind in ("reply", "message"):
        texts = [t for _, t in _pick(spec.where, bot_messages(case, item))]
        if spec.text is not None:
            hits = [t for t in texts if _args_ok({"": spec.text}, t, path, None)]
            if hits:
                _args_ok({"": spec.text}, hits[0], path, checker.pending)
            return len(hits)
        return len(texts)
    if kind == "file":
        files = [
            o for o in item.outputs or []
            if (spec.media_type is None or fnmatchcase(o.media_type, spec.media_type))
            and (spec.name is None or check_value(spec.name, True, o.name))
            and _pages_ok(spec, len(o.renders) if o.renders is not None else None)
        ]  # fmt: skip
        return len(_pick(spec.where, files))
    if kind == "card":
        cards = {
            e.card.id: e.card.kind for e in bot_events(case, item, ("card",)) if e.card is not None
        }
        found = [c for c, k in cards.items() if spec.card_kind in (None, k)]
        return len(_pick(spec.where, found))
    if kind == "reaction":
        found = [
            e for e in bot_events(case, item, ("reaction_add",)) if spec.emoji in (None, e.emoji)
        ]
        return len(_pick(spec.where, found))
    return 0 if bot_messages(case, item) else 1


def _pages_ok(spec: ExpectedOutput, pages: int | None) -> bool:
    if spec.pages is None:
        return True
    low, high = spec.pages.min, spec.pages.max
    return pages is not None and (low is None or pages >= low) and (high is None or pages <= high)


def _output_checks(checker: _Checker) -> None:
    for i, spec in enumerate(checker.case.expect.outputs or []):
        path = f"outputs/{i}"
        count = _output_hits(checker.case, checker.item, spec, path, checker)
        if spec.kind == "no_reply":
            checker.add(path, count == 1, "no reply posted" if count else "a reply was posted")
            continue
        checker.add(
            path, _count_ok(count, spec.min, spec.max),
            f"{count} {spec.kind} matching, expected {_span(spec.min, spec.max)}",
        )  # fmt: skip


# ---------- world and limits ----------


def _world_checks(checker: _Checker, world: WorldState | None) -> None:
    for i, spec in enumerate(checker.case.expect.world or []):
        path = f"world/{i}"
        if world is None:
            checker.add(path, None, "world state not available")
            continue
        data = world.get((spec.service, spec.key))
        if data is None:
            checker.add(path, False, f"{spec.service} {spec.key} not found")
            continue
        found, value = pointer_get(data, spec.path)
        ok = (
            _args_ok({"": spec.check}, value, path, checker.pending)
            if found
            else spec.check.op == "not_contains"
        )
        checker.add(path, ok, f"{spec.service} {spec.key}{spec.path}")


def _duration_s(item: RunItem) -> float | None:
    if item.usage and item.usage.duration_ms is not None:
        return item.usage.duration_ms / 1000
    if item.started_at and item.finished_at:
        return (item.finished_at - item.started_at).total_seconds()
    return None


def _limit_checks(checker: _Checker) -> None:
    limits = checker.case.expect.limits
    if limits is None:
        return
    item = checker.item
    tool_calls = (
        sum(e.kind == "tool_call" for e in item.transcript.events or []) if item.transcript else 0
    )
    if not tool_calls and item.usage and item.usage.tool_calls is not None:
        tool_calls = item.usage.tool_calls
    measured: list[tuple[str, float | None, float | None]] = [
        ("max_tool_calls", limits.max_tool_calls, float(tool_calls)),
        ("max_duration_s", limits.max_duration_s, _duration_s(item)),
        ("max_cost_usd", limits.max_cost_usd, item.usage.cost_usd if item.usage else None),
        ("max_messages", limits.max_messages, float(len(bot_messages(checker.case, item)))),
    ]
    for name, cap, actual in measured:
        if cap is None:
            continue
        if actual is None:
            checker.add(f"limits/{name}", None, "not measured")
        else:
            checker.add(f"limits/{name}", actual <= cap, f"{actual:g} against {cap:g}")


# ---------- entry points ----------


def _run(
    case: Case,
    item: RunItem,
    aliases: Sequence[ToolAlias],
    world: WorldState | None,
    at: datetime | None,
) -> _Checker:
    checker = _Checker(case, item, at or datetime.now(UTC))
    if item.status == "error":
        kind = item.error.kind if item.error else "other"
        checker.add("status", False, f"the item ended in error ({kind})")
        return checker
    if item.status != "done":
        return checker
    if problems := regex_problems(to_document(case.expect)):
        checker.add("expect", None, "; ".join(problems))
        return checker
    calls = canonical_calls(item, aliases)
    expect = case.expect
    if expect.tool_calls:
        _call_checks(checker, "tool_calls", expect.tool_calls.mode, expect.tool_calls.calls, calls)
    for i, pattern in enumerate(expect.forbidden_calls or []):
        seen = [c for c in calls if matches(pattern, c)]
        checker.add(f"forbidden_calls/{i}", not seen, f"{len(seen)} made")
    _effect_checks(checker, calls)
    _output_checks(checker)
    _world_checks(checker, world)
    _limit_checks(checker)
    return checker


def check_item(
    case: Case,
    item: RunItem,
    aliases: Sequence[ToolAlias],
    *,
    world: WorldState | None = None,
    at: datetime | None = None,
) -> list[Score]:
    """Every deterministic check of the case on one item. Items not finished have none; an
    item that ended in error has one failing `status` score. `world` is the item's world
    state after the run, for `expect.world`; without it those checks are unknown."""
    checker = _run(case, item, aliases, world, at)
    checker.scores += [
        Score(
            id=f"code-{sha256(f'{item.id}|{p.criterion}'.encode()).hexdigest()[:24]}",
            item_id=item.id, criterion=p.criterion, grader=CODE, value=ScoreValue(unknown=True),
            rationale="waiting for a judge", at=checker.at,
        )
        for p in checker.pending
    ]  # fmt: skip
    return checker.scores


def llm_checks(
    case: Case, item: RunItem, aliases: Sequence[ToolAlias], *, world: WorldState | None = None
) -> list[LlmCheck]:
    """The `op: llm` checks of a finished item, for judges to answer."""
    if item.status != "done":
        return []
    return _run(case, item, aliases, world, None).pending
