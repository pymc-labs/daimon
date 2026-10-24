"""Authored Managed Agents SDK inputs for TARGET-53, never outcome evidence."""

from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Literal

import httpx
from anthropic import AsyncAnthropic
from anthropic.types.beta.sessions import BetaManagedAgentsStreamSessionEvents
from daimon.testing.provider_fixtures import (
    TARGET_SHA256,
    FixtureGap,
    FixturePack,
    NativeTurn,
    load_provider_fixtures,
)
from daimon.testing.provider_replay import (
    Object,
    Record,
    SourcePin,
    WireFrame,
    WireReplay,
    WireReply,
)
from mux.testing.drivers.anthropic.ma_models import ma_session
from mux.testing.isolated_sdk import build_async_anthropic
from pydantic import Field, TypeAdapter, model_validator

_EVENT = TypeAdapter[BetaManagedAgentsStreamSessionEvents](BetaManagedAgentsStreamSessionEvents)
_OBJECT = TypeAdapter[Object](Object)
MODEL = "claude-haiku-5-5"
SDK_VERSION = "0.117.0"
HEADERS = (("anthropic-beta", "managed-agents-2026-04-01"), ("anthropic-version", "2023-06-01"))
QUERY = (("beta", "true"),)
ARTIFACT_GAP = "Authored command/result is not remote file creation, download or platform delivery."


def _refuse_ambient_anthropic() -> None:
    # Inspect names only: never retrieve, report or mutate operator credentials.
    if any(name.startswith("ANTHROPIC_") for name in os.environ):
        raise ValueError("offline Anthropic replay refuses ambient SDK configuration")


def anthropic_model_usage(turn: NativeTurn) -> Object:
    """Map the pinned OpenAI recipe's totals to Anthropic's disjoint stages.

    OpenAI input includes cached tokens; Anthropic input excludes cache stages.
    Output already includes reasoning tokens. Anthropic has no separate
    reasoning bucket, so do not add that detail a second time.
    """
    usage = _OBJECT.validate_python(turn.snapshot["usage"])
    details = _OBJECT.validate_python(usage["input_tokens_details"])
    counts = (usage["input_tokens"], details["cached_tokens"], usage["output_tokens"])
    if any(not isinstance(c, int) or isinstance(c, bool) or c < 0 for c in counts):
        raise ValueError("recipe usage requires measured nonnegative integer totals")
    total, cached, output = (int(str(c)) for c in counts)
    if cached > total:
        raise ValueError("recipe cached input exceeds total input")
    return {
        "input_tokens": total - cached,
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": 0,
        "output_tokens": output,
    }


class AnthropicTurn(Record):
    turn: int = Field(ge=1)
    request_text: str
    events: tuple[Object, ...]

    @model_validator(mode="after")
    def native_shape(self) -> AnthropicTurn:
        ids = [event.get("id") for event in self.events]
        if not ids or any(not isinstance(id_, str) or not id_ for id_ in ids):
            raise ValueError("native events require nonempty IDs")
        if len(set(str(id_) for id_ in ids)) != len(ids):
            raise ValueError("native events require distinct IDs")
        for event in self.events:
            _EVENT.validate_python(event)
        if self.events[0].get("type") != "user.message" or self.events[0].get("content") != [
            {"type": "text", "text": self.request_text}
        ]:
            raise ValueError("native root must be the submitted input")
        if sum(e.get("type") == "user.message" for e in self.events) != 1:
            raise ValueError("fixture contains a foreign input root")
        if self.events[-1].get("type") != "session.status_idle" or self.events[-1].get(
            "stop_reason"
        ) != {"type": "end_turn"}:
            raise ValueError("authored turn needs an explicit terminal end_turn")
        if any(e.get("type") == "session.status_idle" for e in self.events[:-1]):
            raise ValueError("authored fixture ends before its final native record")
        uses: dict[str, str] = {}
        results: set[str] = set()
        model_starts: set[str] = set()
        model_ends: set[str] = set()
        for event in self.events:
            tag = event["type"]
            if tag in ("agent.tool_use", "agent.mcp_tool_use"):
                uses[str(event["id"])] = str(tag).replace("_use", "_result")
            elif tag in ("agent.tool_result", "agent.mcp_tool_result"):
                reference = str(event.get("tool_use_id", event.get("mcp_tool_use_id")))
                if uses.get(reference) != tag or reference in results:
                    raise ValueError("native result lacks its earlier unique tool call")
                results.add(reference)
            elif tag == "span.model_request_start":
                model_starts.add(str(event["id"]))
            elif tag == "span.model_request_end":
                reference = str(event["model_request_start_id"])
                if reference not in model_starts or reference in model_ends:
                    raise ValueError("native usage lacks its earlier unique model request")
                usage = _OBJECT.validate_python(event["model_usage"])
                counts = tuple(
                    usage[name]
                    for name in (
                        "input_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                        "output_tokens",
                    )
                )
                if any(not isinstance(c, int) or isinstance(c, bool) or c < 0 for c in counts):
                    raise ValueError("native usage requires nonnegative integer stages")
                model_ends.add(reference)
        if set(uses) != results:
            raise ValueError("native tool call lacks its result")
        if len(model_starts) != 1 or model_ends != model_starts:
            raise ValueError("authored turn requires one paired native model usage span")
        return self


class AnthropicScenario(Record):
    # Intentionally matches N9's ScenarioReplay JSON interface.
    scenario_id: str
    scenario_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    backend: Literal["anthropic"] = "anthropic"
    profile: Literal["anthropic.managed_agents"] = "anthropic.managed_agents"
    agent_model: Literal["claude-haiku-5-5"] = MODEL
    values: dict[str, str]
    turns: tuple[AnthropicTurn, ...]

    @model_validator(mode="after")
    def identities(self) -> AnthropicScenario:
        if len({t.turn for t in self.turns}) != len(self.turns):
            raise ValueError("duplicate catalog turn")
        ids = [str(e["id"]) for t in self.turns for e in t.events]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate native ID across turns")
        return self


class AnthropicBinding(Record):
    turn: int = Field(ge=1)
    location: str
    session_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$")
    root_id: str = Field(min_length=1)
    completion_gate: str | None = None


class AnthropicEntry(Record):
    scenario_id: str
    replay: SourcePin
    # Native OpenAI/Gemini recipe blob, not an assertion or result report.
    input: SourcePin
    bindings: tuple[AnthropicBinding, ...]
    gaps: tuple[FixtureGap, ...]


class AnthropicIndex(Record):
    version: Literal[1] = 1
    target_sha256: Literal["fc14eab684b6aa257ca5c01ab113ef154c9847938d263f2739480299c43cc2f4"] = (
        TARGET_SHA256
    )
    provenance: Literal["authored-sdk-wire"] = "authored-sdk-wire"
    sdk_distribution: Literal["anthropic"] = "anthropic"
    sdk_version: Literal["0.117.0"] = SDK_VERSION
    codec: SourcePin
    codec_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    authoring: SourcePin
    input_index: SourcePin
    entries: tuple[AnthropicEntry, ...]

    @model_validator(mode="after")
    def target(self) -> AnthropicIndex:
        ids = [entry.scenario_id for entry in self.entries]
        if (
            len(ids) != 53
            or len(set(ids)) != 53
            or hashlib.sha256(("\n".join(ids) + "\n").encode()).hexdigest() != self.target_sha256
        ):
            raise ValueError("Anthropic index differs from frozen TARGET-53")
        return self


@dataclass(frozen=True)
class AnthropicFixtures:
    index: AnthropicIndex
    scenarios: tuple[AnthropicScenario, ...]

    def select(self, scenario_id: str) -> tuple[AnthropicEntry, AnthropicScenario]:
        entry = next(e for e in self.index.entries if e.scenario_id == scenario_id)
        scenario = next(s for s in self.scenarios if s.scenario_id == scenario_id)
        return entry, scenario


def load_anthropic_fixtures(path: Path, *, source_root: Path) -> AnthropicFixtures:
    index = AnthropicIndex.model_validate_json(path.read_bytes())
    index.codec.resolve(source_root)
    index.authoring.resolve(source_root)
    index.input_index.verify(path.parent.parent)
    pack: FixturePack = load_provider_fixtures(
        path.parent.parent / index.input_index.path,
        catalog_root=path.parent.parent / "catalog",
    )
    rows: list[AnthropicScenario] = []
    for entry, source in zip(index.entries, pack.scenarios, strict=True):
        if entry.input.path != f"native/{source.scenario_id}.json":
            raise ValueError("Anthropic recipe pin belongs to another scenario")
        entry.input.verify(path.parent.parent)
        entry.replay.verify(path.parent)
        row = AnthropicScenario.model_validate_json((path.parent / entry.replay.path).read_bytes())
        if (
            row.scenario_id != entry.scenario_id
            or row.scenario_id != source.scenario_id
            or (row.scenario_sha256 != source.source.sha256 or row.values != source.values)
        ):
            raise ValueError("Anthropic replay differs from its pinned source scenario")
        if {b.turn for b in entry.bindings} != {t.turn for t in row.turns} or len(
            entry.bindings
        ) != len(row.turns):
            raise ValueError("Anthropic bindings must cover each native turn exactly")
        triggers = {
            int(str(i["turn"])): i for i in source.invocations if i.get("operation") == "host_turn"
        }
        covered: set[str] = set()
        native = next(p for p in source.providers if p.backend == "openai")
        # A native transcript cannot erase the source's missing effect or host
        # admission proof. Keep exact authored gap reasons for N9's allowlist.
        required = [
            gap
            for gap in native.gaps
            if gap.code not in ("MCP_SERVER_LIMIT", "MCP_AUTH_UNBOUND", "ARTIFACT_PROTOCOL_UNBOUND")
        ]
        if any(g.code == "ARTIFACT_PROTOCOL_UNBOUND" for g in native.gaps):
            required.extend(
                FixtureGap(code="RUNNER_HOOK", location=b.location, reason=ARTIFACT_GAP)
                for b in entry.bindings
            )
        if any(gap not in entry.gaps for gap in required):
            raise ValueError("Anthropic replay drops a required source/effect gap")
        for turn, binding in zip(row.turns, entry.bindings, strict=True):
            invocation = triggers.get(turn.turn)
            if (
                invocation is None
                or binding.turn != turn.turn
                or binding.location != invocation["location"]
            ):
                raise ValueError("Anthropic replay invents a host invocation")
            text = str(invocation["text"])
            for name, value in row.values.items():
                text = text.replace("{" + name + "}", value)
            if turn.request_text != text or binding.root_id != turn.events[0]["id"]:
                raise ValueError("Anthropic root/input differs from its owned invocation")
            peer = next((t for t in native.turns if t.turn == turn.turn), None)
            if peer is not None and (
                binding.session_id != peer.session_id
                or binding.completion_gate != peer.completion_gate
            ):
                raise ValueError("Anthropic session/gate differs from the pinned recipe")
            if peer is not None:
                usage = next(
                    e["model_usage"] for e in turn.events if e["type"] == "span.model_request_end"
                )
                if usage != anthropic_model_usage(peer):
                    raise ValueError("Anthropic usage differs from the pinned native recipe")
            covered.add(binding.location)
        covered.update(g.location for g in entry.gaps if g.code != "RUNNER_HOOK")
        if {str(i["location"]) for i in triggers.values()} - covered:
            raise ValueError("Anthropic replay silently drops a catalog host invocation")
        rows.append(row)
    return AnthropicFixtures(index, tuple(rows))


class AnthropicTape(Record):
    backend: Literal["anthropic"] = "anthropic"
    profile: Literal["anthropic.managed_agents"] = "anthropic.managed_agents"
    model: Literal["claude-haiku-5-5"] = MODEL
    sdk_version: Literal["0.117.0"] = SDK_VERSION
    source: SourcePin
    codec: SourcePin
    replies: tuple[WireReply, ...]

    @model_validator(mode="after")
    def dependencies(self) -> AnthropicTape:
        known: set[str] = set()
        for reply in self.replies:
            if reply.id in known or set(reply.after) - known:
                raise ValueError("Anthropic tape has duplicate or forward dependencies")
            known.add(reply.id)
        return self


def anthropic_turn_tape(
    entry: AnthropicEntry,
    scenario: AnthropicScenario,
    turn: AnthropicTurn,
    *,
    codec: SourcePin,
    session_id: str | None = None,
    post_turn_usage_catchup: bool = False,
) -> AnthropicTape:
    """Already admitted session only. Runner owns setup, side effects and verdicts.

    An explicit runner session override is allowed because native events carry
    no session ID. Input/root IDs and payloads remain immutable.
    Fresh product-host tapes explicitly opt into the post-turn first
    history page. Direct-driver tapes keep their existing requests unchanged.
    """
    if entry.scenario_id != scenario.scenario_id or turn not in scenario.turns:
        raise ValueError("foreign Anthropic scenario/turn")
    binding = next(b for b in entry.bindings if b.turn == turn.turn)
    if binding.root_id != turn.events[0]["id"]:
        raise ValueError("foreign Anthropic root")
    identity = session_id if session_id is not None else binding.session_id
    # Validate overrides as path segments before constructing wire requests.
    AnthropicBinding(
        turn=turn.turn, location=binding.location, session_id=identity, root_id=binding.root_id
    )
    path = f"/v1/sessions/{identity}"
    accepted = f"turn.{turn.turn}.accepted"
    # Usage spans do not change the tool-start/finish concurrency boundary.
    # The original tape exposed the native tool use before pausing its result.
    gate_begin = (
        next(
            n
            for n, event in enumerate(turn.events)
            if event["type"] in {"agent.tool_result", "agent.mcp_tool_result"}
        )
        if binding.completion_gate
        else len(turn.events)
    )
    tape = AnthropicTape(
        source=entry.replay,
        codec=codec,
        replies=(
            WireReply(
                id="session",
                method="GET",
                path=path,
                query=QUERY,
                headers=HEADERS,
                response_json=_OBJECT.validate_python(
                    ma_session(id=identity, model=MODEL).model_dump(mode="json")
                ),
            ),
            WireReply(
                id="send",
                method="POST",
                path=path + "/events",
                query=QUERY,
                headers=HEADERS,
                request_json={
                    "events": [
                        {
                            "type": "user.message",
                            "content": [{"type": "text", "text": turn.request_text}],
                        }
                    ]
                },
                response_json={"data": [turn.events[0]]},
                releases=(accepted,),
            ),
            WireReply(
                id="stream",
                method="GET",
                path=path + "/events/stream",
                query=QUERY,
                headers=HEADERS,
                frames=tuple(
                    WireFrame(
                        payload=event,
                        sse_event=str(event["type"]),
                        after_gate=binding.completion_gate
                        if binding.completion_gate and n >= gate_begin
                        else accepted,
                    )
                    for n, event in enumerate(turn.events)
                ),
            ),
        ),
    )
    if not post_turn_usage_catchup:
        return tape
    if turn.turn != 1:
        raise ValueError("first-pass usage catch-up requires a fresh first-turn tape")
    # The turn-end settle of the fresh session: its spans, newest first.
    # Exact query, native spans and dependencies; no global replay allowance.
    catchup = WireReply(
        id="usage-catchup",
        method="GET",
        path=path + "/events",
        query=(*QUERY, ("limit", "100"), ("order", "desc"), ("types[]", "span.model_request_end")),
        headers=HEADERS,
        response_json={
            "data": [
                event
                for event in reversed(turn.events)
                if event["type"] == "span.model_request_end"
            ],
            "next_page": None,
        },
        after=("send", "stream"),
    )
    return tape.model_copy(update={"replies": (*tape.replies, catchup)})


@dataclass(frozen=True)
class AnthropicReplay:
    client: AsyncAnthropic
    wire: WireReplay


def anthropic_scenario_tape(
    entry: AnthropicEntry, scenario: AnthropicScenario, *, codec: SourcePin
) -> AnthropicTape:
    """Compose admitted turns without replacing a follow-up session.

    Earlier HTTP send and stream opens precede the next turn's requests. The
    host still owns completion, gate release and any concurrency across slots.
    """
    replies: list[WireReply] = []
    previous: tuple[str, ...] = ()
    for turn in scenario.turns:
        tape = anthropic_turn_tape(entry, scenario, turn, codec=codec)
        prefix = f"turn.{turn.turn}."
        for reply in tape.replies:
            replies.append(
                reply.model_copy(
                    update={
                        "id": prefix + reply.id,
                        "after": previous + tuple(prefix + name for name in reply.after),
                    }
                )
            )
        previous = (prefix + "send", prefix + "stream")
    return AnthropicTape(source=entry.replay, codec=codec, replies=tuple(replies))


@asynccontextmanager
async def anthropic_replay(
    tape: AnthropicTape,
    *,
    source_root: Path,
    codec_root: Path,
    backend: str = "anthropic",
    profile: str = "anthropic.managed_agents",
    model: str = MODEL,
) -> AsyncIterator[AnthropicReplay]:
    _refuse_ambient_anthropic()
    tape = AnthropicTape.model_validate(tape.model_dump(mode="json"))
    if (backend, profile, model) != (tape.backend, tape.profile, tape.model):
        raise ValueError("persisted selection differs from Anthropic replay")
    tape.source.verify(source_root)
    tape.codec.resolve(codec_root)
    if version("anthropic") != tape.sdk_version:
        raise ValueError("Anthropic SDK version differs from the replay pin")
    wire = WireReplay(tape)
    async with build_async_anthropic(
        transport=httpx.MockTransport(wire.dispatch),
        api_key="offline-placeholder",
        base_url="https://offline.invalid",
        headers={},
        max_retries=0,
    ) as client:
        yield AnthropicReplay(client, wire)
