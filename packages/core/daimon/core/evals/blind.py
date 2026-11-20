"""Hiding which arm and model produced an answer: for blind reviewers and for judges."""

from __future__ import annotations

import re
import string
from collections.abc import Iterable, Mapping, Sequence
from hashlib import sha256
from typing import Final

from daimon.core.evals.format import JsonObject, Run, RunItem, to_document
from daimon.core.model_reactions import MODEL_MARKS
from pydantic import JsonValue

MASK: Final = "[model]"
_KNOWN: Final = re.compile(
    r"\b(?:claude|gpt|gemini|codex|opus|sonnet|haiku|o[1-9])(?:[-_.:/][A-Za-z0-9.]+)*\b",
    re.IGNORECASE,
)


def mask_text(text: str, names: Iterable[str] = ()) -> str:
    """`text` without model names (known families and versioned ids) or the given names."""
    for name in sorted({n for n in names if n}, key=len, reverse=True):
        text = re.sub(re.escape(name), MASK, text, flags=re.IGNORECASE)
    text = _KNOWN.sub(MASK, text)
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


def neutral_labels(
    arm_ids: Sequence[str], salt: str, preset: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Labels A, B, C... for arms: the run's own when it has them, else in an order the salt
    shuffles, so a label says nothing about the arm."""
    if preset and all(a in preset for a in arm_ids):
        return {a: preset[a] for a in arm_ids}
    ranked = sorted(arm_ids, key=lambda a: sha256(f"{salt}|{a}".encode()).hexdigest())
    return {arm: string.ascii_uppercase[i % 26] * (i // 26 + 1) for i, arm in enumerate(ranked)}


def handle(salt: str, item_id: str) -> str:
    """An opaque stand-in for an item id, stable for one salt."""
    return "h-" + sha256(f"{salt}|{item_id}".encode()).hexdigest()[:12]


def blind_item_view(item: RunItem, *, label: str, token: str, names: Iterable[str]) -> JsonObject:
    """An item as a blind reader sees it: the arm as its label, an opaque id, no usage or
    enclosure, and no model names or model reactions anywhere in it."""
    update: dict[str, object] = {
        "id": token, "run_id": "run", "arm_id": label, "usage": None, "enclosure": None,
        "ext": None,
    }  # fmt: skip
    masked = mask_document(to_document(item.model_copy(update=update)), names)
    assert isinstance(masked, dict)
    return masked
