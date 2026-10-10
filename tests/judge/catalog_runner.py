"""Offline catalog mapping; no catalog action or provider call is executed."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Literal, cast

import yaml
from mux.contracts.config import BackendConfig, ConfigRevision, resolve_default
from mux.contracts.ids import ChannelRef, Provider
from pydantic import BaseModel, ConfigDict, ValidationInfo, model_validator

Classification = Literal[
    "runnable_headless", "needs_adapter_surface", "needs_unsupported_capability"
]
FROZEN_TARGET_SHA256 = "fc14eab684b6aa257ca5c01ab113ef154c9847938d263f2739480299c43cc2f4"
PROVIDERS: tuple[Provider, ...] = ("anthropic", "openai", "gemini")
SELECTIONS: dict[Provider, tuple[str, str]] = {
    "anthropic": ("anthropic.managed_agents", "claude-haiku-5-5"),
    "openai": ("openai.persistent_workspace", "gpt-6-luna"),
    "gemini": ("gemini.inline_reuse", "gemini-3.8-flash"),
}
# These are semantic host observations, not screenshots or a platform delivery certificate.
HOST_ASSERTS = {
    "text_present",
    "text_absent",
    "done_within_s",
    "no_silent_drop",
    "reply_within_s",
    "log_present",
    "log_absent",
    "card_finalized",
    "attachments",
}
SURFACE_ASSERTS = {
    "in_thread",
    "no_channel_post",
    "same_thread",
    "reaction_present",
    "reaction_absent",
    "no_blank_message",
    "message_count",
    "answers_total",
    "threads_created",
    "thread_name",
    "component_present",
    "component_absent",
    "card_text_now",
    "card_edits_min",
    "progress_seen",
    "progress_text_seen",
    "fences_balanced",
    "chunks_gap_max_s",
    "footer_on_last_message",
    "footer_cost_matches_ledger",
    "channel_text_present",
    "channel_text_absent",
    "answer_length_chars",
    "answer_part_gap_s",
}
STEP_PARAMS = {
    "new_channel": {"do", "ref", "guild"},
    "mention": {"do", "text", "file", "as", "channel"},
    "thread_reply": {"do", "text", "file", "as", "channel", "mention", "reply_to"},
    "channel_message": {"do", "text", "channel", "as"},
    "dm": {"do", "text"},
    "burst": {"do", "texts", "interval_s", "channel", "as"},
    "wait": {"do", "s"},
    "wait_done": {"do", "timeout_s"},
}
ASSERT_PARAMS = {
    "text_present": {"kind", "turn", "pattern"},
    "text_absent": {"kind", "turn", "pattern"},
    "done_within_s": {"kind", "turn", "max"},
    "no_silent_drop": {"kind", "turn", "max"},
    "reply_within_s": {"kind", "turn", "max"},
    "log_present": {"kind", "turn", "event", "fields"},
    "log_absent": {"kind", "turn", "event", "fields"},
    "card_finalized": {"kind", "turn"},
    "attachments": {"kind", "turn", "min", "max", "name_pattern", "unique"},
}
GLOBAL_PATTERNS = (
    r"\(empty response\)",
    r"Discord Error \(\d+\)|Unknown Message|Invalid input:|Unexpected error:|API Error \(\d+\)|Store error|Spec validation failed",
    r"access_token=",
)
PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*(?:\.[a-z0-9_]+|:[A-Za-z0-9_]+)*)\}")


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Gap(Record):
    classification: Literal["needs_adapter_surface", "needs_unsupported_capability"]
    location: str
    reason: str


class CatalogEntry(Record):
    id: str
    filename: str
    sha256: str
    scenario: dict[str, Any]
    load_error: str | None = None


class ChannelBinding(Record):
    ref: str
    revision: ConfigRevision
    agent_model: str


class Invocation(Record):
    location: str
    operation: Literal["new_channel", "host_turn", "context", "wait", "wait_done", "unbound"]
    source: dict[str, Any]
    turn: int | None = None
    channel: str | None = None
    thread: str | None = None
    text: str | None = None
    fixture: str | None = None
    fixture_sha256: str | None = None


class ScenarioPlan(Record):
    scenario: CatalogEntry
    backend: Provider
    scored: bool = False
    classification: Classification
    gaps: tuple[Gap, ...]
    bindings: tuple[ChannelBinding, ...]
    invocations: tuple[Invocation, ...]
    assertions: tuple[dict[str, Any], ...]
    global_assertions: tuple[dict[str, Any], ...] = ()
    # Classification is portability; execution also needs these bindings.
    execution_gaps: tuple[str, ...]
    evidence_status: Literal["pending"] = "pending"

    @model_validator(mode="after")
    def selected_binding(self) -> ScenarioPlan:
        profile, model = SELECTIONS[self.backend]
        for binding in self.bindings:
            if (
                binding.revision.backend != self.backend
                or binding.revision.profile != profile
                or binding.agent_model != model
                or binding.revision.model != (None if self.backend == "anthropic" else model)
            ):
                raise ValueError("channel binding differs from the explicit backend selection")
        return self


class CatalogMatrix(Record):
    version: Literal[1] = 1
    integration_sha: str
    schema_sha256: str
    proposed_kinds_sha256: str
    target_sha256: str
    target_ids: tuple[str, ...]
    scored_denominator: Literal[159] = 159
    plans: tuple[ScenarioPlan, ...]

    @model_validator(mode="after")
    def frozen_denominator(self, info: ValidationInfo) -> CatalogMatrix:
        expected = cast(dict[str, Any], info.context or {}).get(
            "expected_target_sha256", FROZEN_TARGET_SHA256
        )
        canonical = sha(("\n".join(self.target_ids) + "\n").encode())
        if canonical != self.target_sha256 or self.target_sha256 != expected:
            raise ValueError("matrix target IDs differ from the frozen target digest")
        targets = set(self.target_ids)
        if len(self.target_ids) != 53 or len(targets) != 53:
            raise ValueError("matrix must retain exactly 53 distinct target IDs")
        cells = {(p.scenario.id, p.backend) for p in self.plans}
        ids = {p.scenario.id for p in self.plans}
        if len(cells) != len(self.plans) or cells != {
            (id_, backend) for id_ in ids for backend in PROVIDERS
        }:
            raise ValueError("each scenario needs exactly one cell per backend")
        if targets - ids or any(p.scored != (p.scenario.id in targets) for p in self.plans):
            raise ValueError("scored cells must match the frozen target IDs")
        return self


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_catalog(root: Path) -> tuple[CatalogEntry, ...]:
    """One invalid/unknown scenario cannot prevent loading its peers."""
    entries: list[CatalogEntry] = []
    ids: set[str] = set()
    for path in sorted((root / "scenarios").glob("*.yaml")):
        raw = path.read_bytes()
        error = None
        try:
            loaded: object = yaml.safe_load(raw)
            if not isinstance(loaded, dict):
                raise ValueError("missing scenario identity")
            document = cast(dict[str, Any], loaded)
            if not all(
                isinstance(key, str) for key in cast(dict[object, Any], loaded)
            ) or not isinstance(document.get("id"), str):
                raise ValueError("invalid scenario keys or identity")
            id_ = cast(str, document["id"])
            if id_ != path.stem or id_ in ids:
                raise ValueError("scenario identity differs from filename or is repeated")
            if document.get("set") not in ("A", "B") or document.get("surface") not in (
                "discord",
                "slack",
                "teams",
                "headless",
            ):
                raise ValueError("invalid scenario set or surface")
            for section in ("setup", "steps", "assert", "teardown", "human"):
                value = document.get(section, [])
                if not isinstance(value, list):
                    raise ValueError("invalid scenario section")
                for item in cast(list[object], value):
                    if not isinstance(item, dict) or not all(
                        isinstance(key, str) for key in cast(dict[object, object], item)
                    ):
                        raise ValueError("invalid scenario section keys")
            # Ensures replay serialization is possible, including unknown extension fields.
            json.dumps(document, allow_nan=False)
        except (yaml.YAMLError, ValueError, TypeError, UnicodeError):
            id_, document = path.stem, {}
            error = "invalid catalog document; fix its schema before execution"
        ids.add(id_)
        entries.append(
            CatalogEntry(
                id=id_, filename=path.name, sha256=sha(raw), scenario=document, load_error=error
            )
        )
    if not entries:
        raise ValueError("catalog has no YAML scenarios")
    return tuple(entries)


def _gap(kind: str, location: str) -> Gap:
    surface = kind in SURFACE_ASSERTS
    return Gap(
        classification="needs_adapter_surface" if surface else "needs_unsupported_capability",
        location=location,
        reason=f"{kind}: platform observation required"
        if surface
        else f"{kind}: no headless binding implemented",
    )


def _plan_scenario(
    entry: CatalogEntry, root: Path, backend: Provider, *, run_id: str
) -> ScenarioPlan:
    """Map source steps without dropping setup, teardown or any assertion."""
    if not re.fullmatch(r"[a-z0-9-]{1,48}", run_id):
        raise ValueError("run_id must be a bounded fixture namespace")
    profile, model = SELECTIONS[backend]
    config = BackendConfig(
        backend=backend, profile=profile, model=None if backend == "anthropic" else model
    )
    gaps: list[Gap] = []
    operations: list[Invocation] = []
    bindings: dict[str, ChannelBinding] = {}
    threads: dict[str, str] = {}
    first_channel: str | None = None
    last_channel: str | None = None
    turn = 0
    raw = entry.scenario
    if entry.load_error:
        gaps.append(_gap("invalid_document", "scenario"))
    if raw.get("set") == "B" or raw.get("human"):
        gaps.append(
            Gap(
                classification="needs_adapter_surface",
                location="human",
                reason="human checklist needs the original adapter surface",
            )
        )

    for section in ("setup", "steps", "teardown"):
        for index, step in enumerate(raw.get(section, [])):
            location = f"{section}[{index}]"
            kind = step.get("do", "missing_do")
            if kind in STEP_PARAMS and set(step) - STEP_PARAMS[kind]:
                gaps.append(_gap("unknown_step_parameters", location))
            if kind == "new_channel":
                ref = step.get(
                    "ref", "default" if first_channel is None else f"channel_{len(bindings) + 1}"
                )
                if (
                    not isinstance(ref, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,48}", ref)
                    or ref in bindings
                ):
                    gaps.append(_gap("invalid_or_duplicate_channel", location))
                    operations.append(
                        Invocation(location=location, operation="unbound", source=step)
                    )
                    continue
                channel = ChannelRef(
                    tenant_id=f"qa-{run_id}",
                    platform=raw.get("surface", "headless"),
                    channel_id=f"qa-{run_id}-{backend}-{sha(entry.id.encode())[:12]}-{ref}",
                )
                revision = ConfigRevision.create(channel, 1, resolve_default(config))
                bindings[ref] = ChannelBinding(ref=ref, revision=revision, agent_model=model)
                first_channel = first_channel or ref
                operations.append(
                    Invocation(location=location, operation="new_channel", source=step, channel=ref)
                )
                continue
            if kind in ("mention", "thread_reply", "channel_message", "dm", "burst"):
                texts: Any = step.get("texts") if kind == "burst" else [step.get("text")]
                if (
                    not isinstance(texts, list)
                    or not texts
                    or not all(isinstance(t, str) for t in cast(list[object], texts))
                ):
                    gaps.append(_gap("invalid_trigger_text", location))
                    operations.append(
                        Invocation(location=location, operation="unbound", source=step)
                    )
                    continue
                channel_ref = step.get(
                    "channel", last_channel if kind == "thread_reply" else first_channel
                )
                last_channel = channel_ref if isinstance(channel_ref, str) else None
                if channel_ref not in bindings:
                    gaps.append(_gap("unbound_channel", location))
                if (
                    kind in ("dm", "burst")
                    or kind == "thread_reply"
                    and (step.get("reply_to") or step.get("mention") is not True)
                ):
                    gaps.append(
                        Gap(
                            classification="needs_adapter_surface",
                            location=location,
                            reason=f"{kind}: message routing/concurrency requires an adapter fixture",
                        )
                    )
                for offset, text in enumerate(cast(list[str], texts)):
                    turn += 1
                    where = f"{location}.texts[{offset}]" if kind == "burst" else location
                    context = (
                        kind == "channel_message"
                        or kind == "thread_reply"
                        and step.get("mention") is not True
                    )
                    thread = threads.get(channel_ref)
                    if kind in ("mention", "burst"):
                        thread = f"turn{turn}"
                        threads[channel_ref] = thread
                    elif not context and thread is None:
                        gaps.append(_gap("unbound_thread", where))
                    fixture, fixture_hash = None, None
                    if "file" in step:
                        try:
                            fixture_path = (root / step["file"]).resolve()
                            if not fixture_path.is_relative_to(root.resolve() / "fixtures"):
                                raise ValueError("fixture outside catalog fixtures")
                            fixture_hash = sha(fixture_path.read_bytes())
                            fixture = str(fixture_path.relative_to(root.resolve()))
                        except (OSError, TypeError, ValueError):
                            gaps.append(_gap("unavailable_or_unsafe_fixture", where))
                    # Keep placeholders untouched; executor must resolve only explicit fixture values.
                    operations.append(
                        Invocation(
                            location=where,
                            operation="context" if context else "host_turn",
                            source=step,
                            turn=turn,
                            channel=channel_ref if isinstance(channel_ref, str) else None,
                            thread=thread,
                            text=text,
                            fixture=fixture,
                            fixture_sha256=fixture_hash,
                        )
                    )
                continue
            if kind in ("wait", "wait_done"):
                operations.append(
                    Invocation(
                        location=location,
                        operation=kind,
                        source=step,
                        turn=turn or None,
                        channel=last_channel or first_channel,
                    )
                )
            else:
                gaps.append(_gap(str(kind), location))
                operations.append(Invocation(location=location, operation="unbound", source=step))

    assertions = tuple(raw.get("assert", []))
    if not assertions and not raw.get("human"):
        gaps.append(_gap("missing_assertions", "assert"))
    for index, assertion in enumerate(assertions):
        kind = assertion.get("kind", "missing_kind")
        if kind in ASSERT_PARAMS and set(assertion) - ASSERT_PARAMS[kind]:
            gaps.append(_gap("unknown_assertion_parameters", f"assert[{index}]"))
        if kind in ("text_present", "text_absent"):
            re.compile(assertion["pattern"])
        if kind not in HOST_ASSERTS:
            gaps.append(_gap(str(kind), f"assert[{index}]"))
        target = assertion.get("turn", 0)
        if not isinstance(target, int) or isinstance(target, bool) or target < 0 or target > turn:
            gaps.append(_gap("invalid_assertion_turn", f"assert[{index}]"))
    classification: Classification = "runnable_headless"
    if any(g.classification == "needs_unsupported_capability" for g in gaps):
        classification = "needs_unsupported_capability"
    elif gaps:
        classification = "needs_adapter_surface"
    execution_gaps = ["headless host fixture and normalized outcome oracle not yet bound"]
    if backend != "anthropic":
        execution_gaps.append(
            f"{profile}: integration channel admission awaits G; no Anthropic fallback"
        )
    return ScenarioPlan(
        scenario=entry,
        backend=backend,
        classification=classification,
        gaps=tuple(gaps),
        bindings=tuple(bindings.values()),
        invocations=tuple(operations),
        assertions=assertions,
        global_assertions=tuple(
            {"kind": "text_absent", "turn": number, "pattern": pattern}
            for number in range(1, turn + 1)
            for pattern in GLOBAL_PATTERNS
        )
        if raw.get("set") == "A" and raw.get("surface") == "discord"
        else (),
        execution_gaps=tuple(execution_gaps),
    )


def plan_scenario(
    entry: CatalogEntry, root: Path, backend: Provider, *, run_id: str
) -> ScenarioPlan:
    if not re.fullmatch(r"[a-z0-9-]{1,48}", run_id):
        raise ValueError("run_id must be a bounded fixture namespace")
    try:
        return _plan_scenario(entry, root, backend, run_id=run_id)
    except (TypeError, ValueError, KeyError, re.error):
        # Malformed extension parameters affect this scenario, not the whole matrix.
        return ScenarioPlan(
            scenario=entry,
            backend=backend,
            classification="needs_unsupported_capability",
            gaps=(_gap("invalid_step_or_assertion_parameters", "scenario"),),
            bindings=(),
            invocations=tuple(
                Invocation(location=f"{section}[{index}]", operation="unbound", source=step)
                for section in ("setup", "steps", "teardown")
                for index, step in enumerate(entry.scenario.get(section, []))
            ),
            assertions=tuple(entry.scenario.get("assert", [])),
            execution_gaps=("invalid scenario mapping; no host invocation permitted",),
        )


def expand_text(text: str, values: dict[str, str]) -> str:
    """One pass; no environment reads, recursive expansion or shell interpretation."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise ValueError(f"unbound catalog placeholder: {name}")
        return values[name]

    return PLACEHOLDER.sub(replace, text)


def build_matrix(
    root: Path,
    *,
    integration_sha: str,
    run_id: str,
    expected_target_sha256: str = FROZEN_TARGET_SHA256,
) -> CatalogMatrix:
    if not re.fullmatch(r"[a-f0-9]{40}", integration_sha):
        raise ValueError("integration_sha must name an exact head")
    entries = load_catalog(root)
    target_bytes = (root / "TARGET-53.txt").read_bytes()
    if sha(target_bytes) != expected_target_sha256:
        raise ValueError("TARGET-53.txt differs from the frozen target digest")
    targets = tuple(line.strip() for line in target_bytes.decode().splitlines() if line.strip())
    if len(targets) != 53 or len(set(targets)) != 53:
        raise ValueError("TARGET-53.txt must name exactly 53 distinct scenarios")
    if set(targets) - {entry.id for entry in entries}:
        raise ValueError("frozen target scenario missing from catalog")
    return CatalogMatrix.model_validate(
        dict(
            integration_sha=integration_sha,
            schema_sha256=sha((root / "SCHEMA.md").read_bytes()),
            proposed_kinds_sha256=sha((root / "PROPOSED-KINDS.md").read_bytes()),
            target_sha256=sha(target_bytes),
            target_ids=targets,
            plans=tuple(
                plan_scenario(e, root, backend, run_id=run_id).model_copy(
                    update={"scored": e.id in targets}
                )
                for e in entries
                for backend in PROVIDERS
            ),
        ),
        context={"expected_target_sha256": expected_target_sha256},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalog", type=Path)
    parser.add_argument("--integration-sha", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    matrix = build_matrix(
        args.catalog.resolve(), integration_sha=args.integration_sha, run_id=args.run_id
    )
    args.output.write_text(matrix.model_dump_json(indent=2) + "\n")
    print(
        f"Mapped {matrix.scored_denominator} scored cells + "
        f"{sum(not p.scored for p in matrix.plans)} unscored extra cells; every execution remains PENDING."
    )


if __name__ == "__main__":
    main()
