"""Hiding which arm and model produced an answer: one rule and one set of labels for every
blind view (runs, items, compare, review) and for judges."""

from __future__ import annotations

import re
from collections.abc import Iterable
from hashlib import sha256
from typing import Final

from daimon.core.evals.format import (
    EventStream,
    JsonObject,
    Run,
    RunItem,
    TranscriptResult,
    WorldCall,
    to_document,
)
from daimon.core.model_reactions import MODEL_MARKS
from pydantic import JsonValue

MASK: Final = "[model]"
_BRANDS: Final = "claude|gpt|gemini|deepseek|qwen|mistral|mixtral"
"""Names that only ever mean a model."""
_WORDS: Final = "opus|sonnet|haiku|grok|codex|llama"
"""Everyday words that name a model only with a version after them."""
_KNOWN: Final = re.compile(
    rf"\b(?:{_BRANDS})(?:[-_.:/][A-Za-z0-9.]+)*\b"
    rf"|\b(?:{_WORDS})[-_ ]?\d[A-Za-z0-9.]*(?:[-_.][A-Za-z0-9.]+)*\b",
    re.IGNORECASE,
)
_O_SERIES: Final = re.compile(r"\bo[1-9](?:-[a-z]+)*\b")
"""Lowercase only, so "O2" stays."""
_NEUTRAL_ORIGIN: Final = "recorded"
"""Every world answer reads the same in a blind view: live and simulated tell the arm."""


def is_blind(role: str, asked: bool = False) -> bool:
    """The one blinding rule: reviewers always see a run blind; anyone else when they ask."""
    return asked or role == "reviewer"


def mask_text(text: str, names: Iterable[str] = ()) -> str:
    """`text` without model names (known families and versioned ids) or the given names."""
    for name in sorted({n for n in names if n}, key=len, reverse=True):
        pattern = rf"(?<![A-Za-z0-9]){re.escape(name)}(?![A-Za-z0-9])"
        text = re.sub(pattern, MASK, text, flags=re.IGNORECASE)
    text = _O_SERIES.sub(MASK, _KNOWN.sub(MASK, text))
    return "".join(c for c in text if c not in MODEL_MARKS)


def mask_document(node: JsonValue, names: Iterable[str] = ()) -> JsonValue:
    """A copy of a JSON document with `mask_text` applied to every string; list entries that
    are a model or effort mark, or a reaction or chat event carrying one, are dropped."""
    found = frozenset(names)
    if isinstance(node, str):
        return mask_text(node, found)
    if isinstance(node, list):
        return [mask_document(v, found) for v in node if not _is_mark(v)]
    if isinstance(node, dict):
        return {k: mask_document(v, found) for k, v in node.items()}
    return node


def _is_mark(node: JsonValue) -> bool:
    if isinstance(node, dict):
        return node.get("emoji") in MODEL_MARKS
    return isinstance(node, str) and node in MODEL_MARKS


def arm_names(run: Run) -> frozenset[str]:
    """Every string that names an arm or what it runs on, to hide from blind readers."""
    found: set[str] = set()
    for arm in run.arms:
        found |= {arm.id, arm.label}
        if arm.live:
            found |= {arm.live.agent or "", arm.live.model}
        if arm.baseline:
            found.add(arm.baseline.source_system or "")
    found.add(run.origin.deployment or "")
    return frozenset(n for n in found if len(n) >= 3)


def handle(salt: str, text: str) -> str:
    """An opaque stand-in for `text`, stable for one salt."""
    return "h-" + sha256(f"{salt}|{text}".encode()).hexdigest()[:16]


def arm_labels(run: Run) -> dict[str, str]:
    """The neutral name of each arm in every blind view of the run: the run's own labels when
    it has them for every arm, else "Arm 1".."Arm N" in an order the run id shuffles."""
    ids = [a.id for a in run.arms]
    preset = run.blind_labels
    if preset and all(a in preset for a in ids):
        return {a: preset[a] for a in ids}
    ranked = sorted(ids, key=lambda a: handle(run.id, a))
    return {a: f"Arm {n}" for n, a in enumerate(ranked, start=1)}


def _world_call(call: WorldCall) -> WorldCall:
    return WorldCall(
        seq=call.seq, t_ms=call.t_ms, surface=call.surface, service=call.service,
        server=call.server, tool=call.tool, request=call.request, response=call.response,
        origin=_NEUTRAL_ORIGIN, effects=call.effects,
    )  # fmt: skip


def _result(result: TranscriptResult | None) -> TranscriptResult | None:
    if result is None:
        return None
    return TranscriptResult(
        content=result.content, is_error=result.is_error, world_call=result.world_call
    )


def blind_item_view(item: RunItem, *, item_id: str, label: str, names: Iterable[str]) -> JsonObject:
    """An item as a blind reader sees it, rebuilt from an allowlist: what the agent said and
    did under `label` and an opaque id. No usage, model, enclosure, stored transcript blob,
    absolute times, repeat index or world origin, and no model names or reactions."""
    events = item.transcript.events if item.transcript else None
    quiet = [
        e.model_copy(update={"model": None, "usage": None, "result": _result(e.result)})
        for e in events or []
    ]
    blind = RunItem(
        id=item_id, run_id="run", case=item.case, arm_id=label, repeat=0, status=item.status,
        error=item.error, transcript=EventStream(events=quiet) if events is not None else None,
        chat=item.chat, outputs=item.outputs,
        world_calls=[_world_call(w) for w in item.world_calls] if item.world_calls else None,
    )  # fmt: skip
    masked = mask_document(to_document(blind), names)
    assert isinstance(masked, dict)
    return masked
