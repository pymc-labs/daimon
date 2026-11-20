"""Hiding which arm and model produced an answer: one rule and one set of labels for every
blind view (runs, items, compare, review) and for judges."""

from __future__ import annotations

import hashlib
import hmac
import random
import re
from collections import defaultdict
from collections.abc import Collection, Iterable, Sequence
from typing import Final

from daimon.core.evals.format import (
    Anchor,
    BlobRef,
    Case,
    ChatEvent,
    ChatMessage,
    EventStream,
    FormatModel,
    ItemCaseRef,
    JsonObject,
    Output,
    Reference,
    Run,
    RunItem,
    TranscriptEvent,
    TranscriptResult,
    WorldCall,
    to_document,
)
from daimon.core.model_reactions import MODEL_MARKS
from daimon.core.stores.evals import ItemIndexRow
from pydantic import JsonValue

MASK: Final = "[model]"
_BRANDS: Final = "claude|gpt|gemini|deepseek|qwen|mistral|mixtral"
"""Names that only ever mean a model."""
_WORDS: Final = "opus|sonnet|haiku|grok|codex|llama"
"""Everyday words that name a model only with a version after them."""
_KNOWN: Final = re.compile(
    rf"\b(?:{_BRANDS})(?:\d[A-Za-z0-9.]*)?(?:[-_.:/][A-Za-z0-9.]+)*\b"
    rf"|\b(?:{_WORDS})[-_ ]?\d[A-Za-z0-9.]*(?:[-_.][A-Za-z0-9.]+)*\b",
    re.IGNORECASE,
)
_O_SERIES: Final = re.compile(r"\bo[1-9](?:-[a-z]+)*\b")
"""Lowercase only, so "O2" stays."""
ID_SEP: Final = "~"
"""Separates the parts of an opaque item id: run handle, view, item handle."""
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


def keyed(secret: str, *parts: str) -> str:
    """An HMAC of `parts` under the workspace's secret: stable, and not recomputable from
    anything a reader can see, so no blind handle, label or shuffle can be worked back."""
    data = "\x1f".join(parts).encode()
    return hmac.new(secret.encode(), data, hashlib.sha256).hexdigest()


def handle(secret: str, *parts: str) -> str:
    """An opaque stand-in for `parts`, stable for one secret."""
    return "h-" + keyed(secret, *parts)[:16]


def roll(secret: str, *parts: str) -> random.Random:
    """A shuffle only the secret's holder can reproduce."""
    return random.Random(int(keyed(secret, *parts), 16))


def arm_labels(run: Run, secret: str) -> dict[str, str]:
    """The neutral name of each arm in every blind view of the run: the run's own labels when
    it has them for every arm, else "Arm 1".."Arm N" in an order the secret shuffles."""
    ids = [a.id for a in run.arms]
    preset = run.blind_labels
    if preset and all(a in preset for a in ids):
        return {a: preset[a] for a in ids}
    ranked = sorted(ids, key=lambda a: handle(secret, "arm", run.id, a))
    return {a: f"Arm {n}" for n, a in enumerate(ranked, start=1)}


def blind_pool(index: Sequence[ItemIndexRow], arms: Collection[str]) -> list[ItemIndexRow]:
    """The items a blind reader may see: finished, and of a case and repeat every arm in
    `arms` finished. Errors, skips and repeats only some arms have tell live arms from
    recorded ones, so none of them is served."""
    groups: dict[tuple[str, int], list[ItemIndexRow]] = defaultdict(list)
    for i in index:
        if i.status == "done" and i.arm_key in arms:
            groups[(i.case_key, i.repeat)].append(i)
    return [i for g in groups.values() if {i.arm_key for i in g} == set(arms) for i in g]


def run_handle(secret: str, run_key: str) -> str:
    """A run's stand-in in opaque ids: run ids are chosen by whoever made the run, and one
    that names an arm must not travel in an id."""
    return keyed(secret, "run", run_key)[:12]


def opaque_id(secret: str, run_key: str, scope: str, item_key: str) -> str:
    """The id a blind view gives an item: the run's handle, the view (`scope`) and a keyed
    handle of the item, so the same item has a different id in each view and none names a
    case or an arm."""
    tail = handle(secret, "item", run_key, scope, item_key)[2:]
    return f"{run_handle(secret, run_key)}{ID_SEP}{scope}{ID_SEP}{tail}"


class CaseView(FormatModel):
    """The part of a case a reviewer needs; never its world, provenance or expectations."""

    id: str
    title: str
    platform: str
    history: list[ChatMessage]
    turn: list[ChatMessage]
    reference: Reference | None
    participants: dict[str, str]
    """Author id to display name, as the case's surface names them (the bot included)."""


def case_view(case: Case, names: Iterable[str] | None = None) -> CaseView:
    """`case` cut down to what a reviewer needs; with `names`, model names are masked too."""
    view = CaseView(
        id=case.id, title=case.title, platform=case.surface.platform, history=case.history,
        turn=case.turn.messages, reference=case.expect.reference,
        participants={p.id: p.display_name for p in [*case.surface.participants, case.surface.bot]},
    )  # fmt: skip
    if names is None:
        return view
    masked = mask_document(view.model_dump(mode="json", by_alias=True), names)
    return CaseView.model_validate(masked)


def _numbered(prefix: str, ids: Iterable[str | None]) -> dict[str, str]:
    """`prefix1`, `prefix2`... for ids in order of first appearance."""
    found: dict[str, str] = {}
    for i in ids:
        if i is not None and i not in found:
            found[i] = f"{prefix}{len(found) + 1}"
    return found


def blind_refs(item: RunItem) -> dict[str, dict[str, str]]:
    """The neutral names a blind view gives an item's messages, tool calls and outputs, by
    the `Anchor.target` they answer to. Provider ids and platform ids carry real times and
    tell live arms from recorded ones; places are all that is left."""
    events = item.transcript.events if item.transcript and item.transcript.events else []
    chat = item.chat or []
    messages = _numbered("m", [i for e in chat for i in (e.message_id, e.reply_to)])
    return {
        "message": messages,
        "tool_call": _numbered("c", [e.call_id for e in events]),
        "output": _numbered("out", [o.id for o in item.outputs or []]),
    }


def blind_ref(item: RunItem, anchor: Anchor) -> Anchor:
    """`anchor` with its `ref` named as a blind view names it."""
    names = blind_refs(item).get(anchor.target, {})
    return (
        anchor
        if anchor.ref is None
        else anchor.model_copy(update={"ref": names.get(anchor.ref, anchor.ref)})
    )


def real_ref(item: RunItem, anchor: Anchor) -> Anchor:
    """`anchor` as a blind reader made it, with its `ref` back to the item's own."""
    names = {v: k for k, v in blind_refs(item).get(anchor.target, {}).items()}
    return (
        anchor
        if anchor.ref is None
        else anchor.model_copy(update={"ref": names.get(anchor.ref, anchor.ref)})
    )


def blind_blobs(item: RunItem) -> dict[str, BlobRef]:
    """An item's output files by the name a blind view gives them: `out1`, `out1.r1` (a render),
    `out1.t` (its text). A blind view carries these in place of content hashes."""
    found: dict[str, BlobRef] = {}
    for n, out in enumerate(item.outputs or [], start=1):
        found[f"out{n}"] = out.blob
        for k, render in enumerate(out.renders or [], start=1):
            found[f"out{n}.r{k}"] = render
        if out.text is not None:
            found[f"out{n}.t"] = out.text
    return found


def _world_call(call: WorldCall) -> WorldCall:
    return WorldCall(
        seq=call.seq, surface=call.surface, service=call.service, server=call.server,
        tool=call.tool, request=call.request, response=call.response,
        origin=_NEUTRAL_ORIGIN, effects=call.effects,
    )  # fmt: skip


def _result(result: TranscriptResult | None) -> TranscriptResult | None:
    if result is None:
        return None
    return TranscriptResult(
        content=result.content, is_error=result.is_error, world_call=result.world_call
    )


def _event(e: TranscriptEvent, calls: dict[str, str]) -> TranscriptEvent:
    return e.model_copy(
        update={
            "t_ms": None,
            "model": None,
            "usage": None,
            "result": _result(e.result),
            "call_id": calls.get(e.call_id or ""),
        }
    )


def _chat(e: ChatEvent, messages: dict[str, str], outputs: dict[str, str]) -> ChatEvent:
    return e.model_copy(
        update={
            "t_ms": None,
            "platform_payloads": None,
            "message_id": messages.get(e.message_id or ""),
            "reply_to": messages.get(e.reply_to or ""),
            "files": [outputs.get(f, "file") for f in e.files] if e.files is not None else None,
        }
    )


def _output(out: Output, n: int, item_id: str) -> Output:
    """An output as a blind reader sees it: a neutral id and file names that open through
    the blind item (`blind_blobs`), and no source or path."""

    def ref(name: str, blob: BlobRef) -> BlobRef:
        return BlobRef(
            sha256=f"{item_id}{ID_SEP}{name}", size=blob.size, media_type=blob.media_type,
            name=blob.name,
        )  # fmt: skip

    return Output(
        id=f"out{n}", name=out.name, media_type=out.media_type, blob=ref(f"out{n}", out.blob),
        renders=[ref(f"out{n}.r{k}", r) for k, r in enumerate(out.renders or [], 1)] or None,
        text=ref(f"out{n}.t", out.text) if out.text else None,
    )  # fmt: skip


def blind_item_view(item: RunItem, *, item_id: str, label: str, names: Iterable[str]) -> JsonObject:
    """An item as a blind reader sees it, rebuilt from an allowlist: what the agent said and
    did under `label` and an opaque id. No usage, model, error, enclosure, case revision,
    stored transcript blob, content hash, path, platform payload, absolute time, repeat index
    or world origin; message, call and output ids by place; no model names or reactions."""
    refs = blind_refs(item)
    events = item.transcript.events if item.transcript else None
    blind = RunItem(
        id=item_id, run_id="run", case=ItemCaseRef(id=item.case.id, revision=0, hash=""),
        arm_id=label, repeat=0, status="done",
        transcript=(
            EventStream(events=[_event(e, refs["tool_call"]) for e in events])
            if events is not None else None
        ),
        chat=(
            [_chat(e, refs["message"], refs["output"]) for e in item.chat]
            if item.chat is not None else None
        ),
        outputs=(
            [_output(o, n, item_id) for n, o in enumerate(item.outputs, 1)]
            if item.outputs is not None else None
        ),
        world_calls=[_world_call(w) for w in item.world_calls] if item.world_calls else None,
    )  # fmt: skip
    masked = mask_document(to_document(blind), names)
    assert isinstance(masked, dict)
    return masked
