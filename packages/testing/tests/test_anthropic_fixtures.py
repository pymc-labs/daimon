"""Real SDK/legacy host replays, equivalent input and hostile missing coverage."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from anthropic import APIConnectionError
from anthropic.types.beta.sessions import BetaManagedAgentsSpanModelRequestEndEvent
from daimon.core.turn.driver import run_turn
from daimon.core.turn.posture import BillingExempt
from daimon.testing.anthropic_fixtures import (
    AnthropicEntry,
    AnthropicIndex,
    AnthropicScenario,
    AnthropicTurn,
    anthropic_model_usage,
    anthropic_replay,
    anthropic_scenario_tape,
    anthropic_turn_tape,
    load_anthropic_fixtures,
)
from daimon.testing.provider_fixtures import FixturePack, load_provider_fixtures
from daimon.testing.provider_replay import Object, SourcePin, WireFrame, WireReplay
from daimon.testing.turn_fakes import RecordingLifecycle
from mux.drivers.anthropic.transport import LegacyTurnTransport
from pydantic import JsonValue, TypeAdapter, ValidationError

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "qa_anthropic_fixtures", ROOT / "scripts/qa_anthropic_fixtures.py"
)
assert SPEC is not None and SPEC.loader is not None
generator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generator)
generate = cast(Callable[[Path, Path], AnthropicIndex], generator.generate)
DATA = ROOT / "packages/testing/fixtures/target53"
DIRECTORY = DATA / "anthropic"
PACK = load_anthropic_fixtures(DIRECTORY / "index.json", source_root=ROOT)
INPUT = load_provider_fixtures(DATA / "index.json", catalog_root=DATA / "catalog")
CASES = [
    (entry, scenario, turn)
    for scenario in PACK.scenarios
    for entry in PACK.index.entries
    if entry.scenario_id == scenario.scenario_id
    for turn in scenario.turns
]
_OBJECT = TypeAdapter[Object](Object)


@pytest.mark.asyncio
async def test_main_824_catchup_reads_recorded_usage_after_the_real_sdk_turn() -> None:
    entry, row = PACK.select("QA-NEW9-NO-INTERNAL-PLUMBING")
    turn = row.turns[0]
    original = anthropic_turn_tape(entry, row, turn, codec=PACK.index.codec)
    tape = anthropic_turn_tape(
        entry, row, turn, codec=PACK.index.codec, post_turn_usage_catchup=True
    )
    assert tape.replies[:-1] == original.replies
    async with anthropic_replay(tape, source_root=DIRECTORY, codec_root=ROOT) as replay:
        identity = entry.bindings[0].session_id
        edge = LegacyTurnTransport(replay.client, identity)
        assert await edge.status() == "idle"
        lifecycle = RecordingLifecycle()
        state = await run_turn(
            anthropic=replay.client,
            session_id=identity,
            user_message=turn.request_text,
            lifecycle=lifecycle,
            cancel=asyncio.Event(),
            billing=BillingExempt(reason="headless-unrecorded"),
            path="legacy",
            render_interval_s=3600,
        )
        assert state.error is None and lifecycle.terminal_success
        # The turn-end settle reads the session's spans newest first.
        native_spans = [
            event for event in reversed(turn.events) if event["type"] == "span.model_request_end"
        ]
        assert tape.replies[-1].response_json == {"data": native_spans, "next_page": None}
        page = await replay.client.beta.sessions.events.list(
            identity, limit=100, order="desc", types=["span.model_request_end"]
        )
        assert [event.model_dump(mode="json") for event in page.data] == [
            BetaManagedAgentsSpanModelRequestEndEvent.model_validate(event).model_dump(mode="json")
            for event in native_spans
        ]
        assert page.next_page is None
        replay.wire.assert_consumed()
        with pytest.raises(APIConnectionError):
            await replay.client.beta.sessions.events.list(
                identity, limit=100, order="desc", types=["span.model_request_end"]
            )
        assert len(replay.wire.violations) == 1


@pytest.mark.parametrize("mutant", ("premature", "session", "limit", "type", "order"))
def test_main_824_catchup_does_not_allow_foreign_or_mismatched_reads(mutant: str) -> None:
    import httpx

    entry, row = PACK.select("QA-NEW9-NO-INTERNAL-PLUMBING")
    tape = anthropic_turn_tape(
        entry, row, row.turns[0], codec=PACK.index.codec, post_turn_usage_catchup=True
    )
    reply = tape.replies[-1]
    if mutant != "premature":
        tape = tape.model_copy(update={"replies": (reply.model_copy(update={"after": ()}),)})
    path = reply.path if mutant != "session" else "/v1/sessions/foreign/events"
    query = dict(reply.query)
    if mutant in {"limit", "type", "order"}:
        query[{"limit": "limit", "type": "types[]", "order": "order"}[mutant]] = {
            "limit": "1",
            "type": "agent.message",
            "order": "asc",
        }[mutant]
    wire = WireReplay(tape)
    request = httpx.Request(
        "GET", "https://offline.invalid" + path, params=query, headers=dict(reply.headers)
    )
    with pytest.raises(AssertionError, match="unscripted, mismatched or premature"):
        wire.dispatch(request)
    assert len(wire.violations) == 1


_AMBIENT_SETTINGS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_WEBHOOK_SIGNING_KEY",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_CONFIG_DIR",
    "ANTHROPIC_IDENTITY_TOKEN_FILE",
    "ANTHROPIC_FUTURE_SETTING",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _AMBIENT_SETTINGS)
@pytest.mark.parametrize("value", ("", "invented-offline-credential"))
async def test_ambient_sdk_setting_refuses_before_client_creation(
    name: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from daimon.testing import anthropic_fixtures

    # Replace the mapping: do not inspect or save any operator setting values.
    ambient = {name: value}
    monkeypatch.setattr(os, "environ", ambient)

    def forbidden_client(*args: object, **kwargs: object) -> None:
        pytest.fail("SDK client constructed before ambient setting refusal")

    monkeypatch.setattr(anthropic_fixtures, "build_async_anthropic", forbidden_client)
    entry, scenario, turn = CASES[0]
    tape = anthropic_turn_tape(entry, scenario, turn, codec=PACK.index.codec)
    with pytest.raises(ValueError, match="refuses ambient SDK configuration") as caught:
        async with anthropic_replay(tape, source_root=DIRECTORY, codec_root=ROOT):
            pytest.fail("ambient replay admitted")
    assert str(caught.value) == "offline Anthropic replay refuses ambient SDK configuration"
    assert ambient == {name: value}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _AMBIENT_SETTINGS)
@pytest.mark.parametrize("method", ("copy", "with_options", "with_middleware"))
async def test_sdk_copies_refuse_settings_introduced_after_admission(
    name: str, method: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ambient: dict[str, str] = {}
    monkeypatch.setattr(os, "environ", ambient)
    entry, scenario, turn = CASES[0]
    tape = anthropic_turn_tape(entry, scenario, turn, codec=PACK.index.codec)
    async with anthropic_replay(tape, source_root=DIRECTORY, codec_root=ROOT) as replay:
        client = replay.client
        assert client.api_key == "offline-placeholder"
        assert client.auth_token is None
        assert client.webhook_key is None
        assert client.credentials is None
        assert str(client.base_url) == "https://offline.invalid"
        # The actual SDK public copies retain the same constructor guard.
        clean_copy = client.with_options(timeout=1)
        assert type(clean_copy) is type(client)
        assert clean_copy.api_key == client.api_key
        assert clean_copy.auth_token is None
        assert clean_copy.webhook_key is None
        assert clean_copy.credentials is None
        assert clean_copy.default_headers == client.default_headers
        ambient[name] = "invented-offline-credential"
        for source in (client, clean_copy):
            with pytest.raises(ValueError, match="refuses ambient"):
                if method == "copy":
                    source.copy()
                elif method == "with_options":
                    source.with_options(timeout=1)
                else:
                    source.with_middleware()
        assert ambient == {name: "invented-offline-credential"}
        assert replay.wire.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entry", "scenario", "turn"), CASES, ids=[f"{s.scenario_id}:{t.turn}" for _, s, t in CASES]
)
async def test_each_turn_drives_real_anthropic_sdk_and_legacy_host(
    entry: AnthropicEntry,
    scenario: AnthropicScenario,
    turn: AnthropicTurn,
) -> None:
    tape = anthropic_turn_tape(entry, scenario, turn, codec=PACK.index.codec)
    binding = next(b for b in entry.bindings if b.turn == turn.turn)
    async with anthropic_replay(tape, source_root=DIRECTORY, codec_root=ROOT) as replay:
        edge = LegacyTurnTransport(replay.client, binding.session_id)
        assert await edge.status() == "idle"
        lifecycle = RecordingLifecycle()
        # An authored tool-finish gate is a decoder control, not real elapsed time.
        if binding.completion_gate:
            replay.wire.release(binding.completion_gate)
        state = await asyncio.wait_for(
            run_turn(
                anthropic=replay.client,
                session_id=binding.session_id,
                user_message=turn.request_text,
                lifecycle=lifecycle,
                cancel=asyncio.Event(),
                billing=BillingExempt(reason="headless-unrecorded"),
                path="legacy",
                render_interval_s=3600,
                stream_read_timeout_s=5,
            ),
            timeout=15,
        )
        assert state.error is None and lifecycle.terminal_success
        assert lifecycle.terminal_failures == []
        usage = _OBJECT.validate_python(
            next(e["model_usage"] for e in turn.events if e["type"] == "span.model_request_end")
        )
        assert state.usage_totals.input_tokens == usage["input_tokens"]
        assert state.usage_totals.cache_read_input_tokens == usage["cache_read_input_tokens"]
        assert (
            state.usage_totals.cache_creation_input_tokens == usage["cache_creation_input_tokens"]
        )
        assert state.usage_totals.output_tokens == usage["output_tokens"]
        assert [e.model_dump(mode="json")["id"] for e in lifecycle.sse_events] == [
            e["id"] for e in turn.events
        ]
        replay.wire.assert_consumed()
        assert len(replay.wire.requests) == 3
        assert replay.client.max_retries == 0
        assert {k for r in replay.wire.requests for k, _ in r.protocol_headers} <= {
            "content-type",
            "accept",
            "anthropic-beta",
            "anthropic-version",
        }


def test_all_53_cells_have_sources_and_unsupported_triggers_remain_typed() -> None:
    assert len(PACK.scenarios) == 53
    assert len(CASES) == 53
    assert sum(bool(s.turns) for s in PACK.scenarios) == 33
    assert len([s for s in PACK.scenarios if not s.turns]) == 20
    for source, entry in zip(INPUT.scenarios, PACK.index.entries, strict=True):
        assert source.scenario_id == entry.scenario_id
        assert all(g.status == "BLOCKED" for g in entry.gaps)
        assert not any(g.code == "ANTHROPIC_SOURCE_UNBOUND" for g in entry.gaps)
        row = next(s for s in PACK.scenarios if s.scenario_id == entry.scenario_id)
        represented = {b.location for b in entry.bindings} | {
            g.location for g in entry.gaps if g.code != "RUNNER_HOOK"
        }
        assert {
            str(i["location"]) for i in source.invocations if i.get("operation") == "host_turn"
        } <= represented
        assert row.scenario_sha256 == source.source.sha256
        if source.scenario.get("human"):
            assert row.turns == () and any(g.code == "MANUAL_TRIGGER" for g in entry.gaps)
    # Rejection/dedupe must never acquire a made-up Anthropic reply.
    for sid in (
        "QA-NEW13-WRITERS-NONE-NOT-SILENT",
        "QA-NEW14-MISSING-THREAD-PERMISSION-COPY",
        "QA-NEW16-TENANT-CREDIT-DEPLETED-COPY",
    ):
        entry, row = PACK.select(sid)
        assert not row.turns and any(g.code == "HOST_GATE" for g in entry.gaps)
    for sid in ("QA-NEW4-DOUBLE-MENTION-DEDUPE", "QA-NEW15-CHANNEL-BUDGET-EXHAUSTED"):
        entry, row = PACK.select(sid)
        assert len(row.turns) == 1 and any(g.code == "HOST_GATE" for g in entry.gaps)


def _messages(events: tuple[Object, ...]) -> list[JsonValue]:
    return [e["content"] for e in events if e.get("type") == "agent.message"]


def test_bound_turns_preserve_other_backends_content_tools_and_followup_sessions() -> None:
    for source in INPUT.scenarios:
        openai = next(p for p in source.providers if p.backend == "openai")
        gemini = next(p for p in source.providers if p.backend == "gemini")
        entry, row = PACK.select(source.scenario_id)
        for native in openai.turns:
            turn = next(t for t in row.turns if t.turn == native.turn)
            other = next(t for t in gemini.turns if t.turn == native.turn)
            items = [
                _OBJECT.validate_python(f["item"])
                for f in native.frames
                if f.get("type") == "agent.session.turn.item.done"
            ]
            texts = [
                item["content"]
                for item in items
                if item.get("type") == "message" and item.get("role") == "assistant"
            ]
            expected = [
                [
                    {"type": "text", "text": _OBJECT.validate_python(part)["text"]}
                    for part in cast(list[JsonValue], content)
                ]
                for content in texts
            ]
            assert _messages(turn.events) == expected
            assert turn.request_text == native.request_text == other.request_text
            binding = next(b for b in entry.bindings if b.turn == native.turn)
            assert binding.session_id == native.session_id == other.session_id
            assert binding.location == native.location
            tools = [e for e in turn.events if e.get("type") == "agent.mcp_tool_use"]
            results = [e for e in turn.events if e.get("type") == "agent.mcp_tool_result"]
            assert [e["name"] for e in tools] == [c.name for c in native.mcp_calls]
            assert [e["input"] for e in tools] == [c.arguments for c in native.mcp_calls]
            assert [e["mcp_server_name"] for e in tools] == [c.server for c in native.mcp_calls]
            assert [e["content"] for e in results] == [
                c.result["content"] for c in native.mcp_calls
            ]
            assert [e["is_error"] for e in results] == [
                c.result["isError"] for c in native.mcp_calls
            ]
            commands = [item for item in items if item.get("type") == "command_execution"]
            assert [e["input"] for e in turn.events if e.get("type") == "agent.tool_use"] == [
                {"command": item["command"]} for item in commands
            ]
            assert [e["content"] for e in turn.events if e.get("type") == "agent.tool_result"] == [
                [{"type": "text", "text": item["output"]}] for item in commands
            ]
    entry, row = PACK.select("QA-D1-CANARY-TWO-TURN")
    assert entry.bindings[0].session_id == entry.bindings[1].session_id
    assert row.turns[0].events[0]["id"] != row.turns[1].events[0]["id"]
    _, row = PACK.select("QA-NEW37-MARKDOWN-TABLE-READABLE")
    content = _messages(row.turns[0].events)
    assert content == [
        [
            {
                "type": "text",
                "text": "| Fruit | Colour | Price |\n|---|---|---|\n| Apple | Red | $1 |\n"
                "| Banana | Yellow | $2 |\n| Orange | Orange | $3 |\n| Pear | Green | $4 |",
            }
        ]
    ]


def test_all_native_model_usage_spans_match_other_provider_totals_and_sdk() -> None:
    seen = 0
    for source in INPUT.scenarios:
        openai = next(p for p in source.providers if p.backend == "openai")
        gemini = next(p for p in source.providers if p.backend == "gemini")
        _, row = PACK.select(source.scenario_id)
        for native in openai.turns:
            turn = next(t for t in row.turns if t.turn == native.turn)
            end = next(e for e in turn.events if e["type"] == "span.model_request_end")
            parsed = TypeAdapter(BetaManagedAgentsSpanModelRequestEndEvent).validate_python(end)
            assert parsed.model_usage.model_dump(exclude_none=True) == anthropic_model_usage(native)
            assert parsed.model_request_start_id in {
                str(e["id"]) for e in turn.events if e["type"] == "span.model_request_start"
            }
            other = next(t for t in gemini.turns if t.turn == native.turn)
            usage = _OBJECT.validate_python(other.snapshot["usage"])
            assert (
                parsed.model_usage.input_tokens + parsed.model_usage.cache_read_input_tokens
                == usage["total_input_tokens"]
            )
            assert parsed.model_usage.cache_read_input_tokens == usage["total_cached_tokens"]
            assert parsed.model_usage.output_tokens == usage["total_output_tokens"]
            assert parsed.model_usage.cache_creation_input_tokens == 0
            seen += 1
    assert seen == 48
    assert sum(e["type"] == "span.model_request_end" for _, _, t in CASES for e in t.events) == 53


@pytest.mark.parametrize(
    "change",
    [
        "no_start",
        "no_end",
        "no_usage",
        "foreign_start",
        "early_end",
        "duplicate_end",
        "negative",
        "boolean",
    ],
)
def test_native_usage_omission_foreign_pairs_or_invalid_stages_refuse(change: str) -> None:
    blob = CASES[0][2].model_dump(mode="json")
    events = cast(list[Object], blob["events"])
    end = next(e for e in events if e["type"] == "span.model_request_end")
    if change in {"no_start", "no_end", "no_usage"}:
        kinds = (
            {"span.model_request_start"}
            if change == "no_start"
            else {"span.model_request_end"}
            if change == "no_end"
            else {"span.model_request_start", "span.model_request_end"}
        )
        blob["events"] = [e for e in events if e["type"] not in kinds]
    elif change == "foreign_start":
        end["model_request_start_id"] = events[0]["id"]
    elif change == "early_end":
        events.remove(end)
        events.insert(1, end)
    elif change == "duplicate_end":
        events.insert(-1, {**end, "id": "second-end"})
    else:
        cast(Object, end["model_usage"])["input_tokens"] = -1 if change == "negative" else True
    with pytest.raises(ValueError):
        AnthropicTurn.model_validate_json(json.dumps(blob))


@pytest.mark.parametrize(
    "change",
    [
        "foreign_root",
        "unknown_type",
        "missing_command",
        "missing_result",
        "foreign_result",
        "duplicate_result",
        "wrong_text",
        "no_terminal",
        "early_terminal",
        "wrong_tool_kind",
    ],
)
def test_hostile_native_events_refuse_before_sdk(change: str) -> None:
    _, _, original = CASES[0]
    # Use a bash turn for every negative to exercise real required SDK fields.
    _, source = PACK.select("QA-I3-TOOL-ONLY-NO-EMPTY-RESPONSE")
    document = source.turns[0].model_dump(mode="json")
    events = cast(list[Object], document["events"])
    tool = next(e for e in events if e["type"] == "agent.tool_use")
    result = next(e for e in events if e["type"] == "agent.tool_result")
    if change == "foreign_root":
        events.insert(1, {**events[0], "id": "foreign"})
    elif change == "unknown_type":
        tool["type"] = "oracle.pass"
    elif change == "missing_command":
        del tool["input"]
    elif change == "missing_result":
        events.remove(result)
    elif change == "foreign_result":
        result["tool_use_id"] = "foreign"
    elif change == "duplicate_result":
        events.insert(events.index(result) + 1, {**result, "id": "another-result"})
    elif change == "wrong_text":
        document["request_text"] = original.request_text + "foreign"
    elif change == "no_terminal":
        events.pop()
    elif change == "early_terminal":
        events.insert(2, {**events[-1], "id": "early-end"})
    else:
        result["type"] = "agent.mcp_tool_result"
        result["mcp_tool_use_id"] = result.pop("tool_use_id")
    with pytest.raises(ValueError):
        AnthropicTurn.model_validate(document)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["selection", "source", "sdk"])
async def test_replay_factory_refuses_wrong_contract_before_requests(
    bad: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry, source, turn = CASES[0]
    tape = anthropic_turn_tape(entry, source, turn, codec=PACK.index.codec)
    backend = "anthropic"
    if bad == "selection":
        backend = "gemini"
    elif bad == "source":
        tape = tape.model_copy(
            update={"source": SourcePin(path=entry.replay.path, sha256="0" * 64)}
        )
    else:

        def foreign_version(distribution: str) -> str:
            return "foreign"

        monkeypatch.setattr("daimon.testing.anthropic_fixtures.version", foreign_version)
    with pytest.raises(ValueError):
        async with anthropic_replay(tape, source_root=DIRECTORY, codec_root=ROOT, backend=backend):
            pytest.fail("wrong contract constructed an SDK client")


@pytest.mark.asyncio
async def test_unexpected_sdk_request_cannot_fall_back_to_network_or_hide_violation() -> None:
    entry, row, turn = CASES[0]
    tape = anthropic_turn_tape(entry, row, turn, codec=PACK.index.codec)
    async with anthropic_replay(tape, source_root=DIRECTORY, codec_root=ROOT) as replay:
        with pytest.raises(APIConnectionError):
            await LegacyTurnTransport(replay.client, "foreign-session").status()
        assert replay.wire.violations == ["unscripted, mismatched or premature provider request"]
        with pytest.raises(AssertionError):
            replay.wire.assert_consumed()
        assert len(replay.wire.requests) == 1


def test_regeneration_accepts_benign_codec_refactor_and_keeps_recorded_payloads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reviewed = tmp_path / generator.CODEC
    reviewed.parent.mkdir(parents=True)
    reviewed.write_bytes((ROOT / generator.CODEC).read_bytes() + b"\n# harmless codec comment\n")
    author = tmp_path / "scripts/qa_provider_fixtures.py"
    author.parent.mkdir(parents=True)
    author.write_bytes((ROOT / "scripts/qa_provider_fixtures.py").read_bytes())
    monkeypatch.setattr(generator, "ROOT", tmp_path)
    output = tmp_path / "replay"
    generated = generate(DATA, output)
    assert len(generated.entries) == 53
    for entry in generated.entries:
        assert (output / entry.replay.path).read_bytes() == (
            DIRECTORY / entry.replay.path
        ).read_bytes()


def test_replay_regenerates_byte_identically_without_consulting_assertions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable_git(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("regeneration depended on historical Git objects")

    monkeypatch.setattr(subprocess, "check_output", unavailable_git)
    first, second = tmp_path / "first", tmp_path / "second"
    generate(DATA, first)
    # Poison every outcome predicate. The recipe inputs remain identical.
    poisoned = INPUT.model_copy(
        update={
            "scenarios": tuple(
                s.model_copy(
                    update={
                        "scenario": {
                            **s.scenario,
                            "assert": [{"kind": "text_contains", "pattern": "DO-NOT-AUTHOR-ME"}],
                        }
                    }
                )
                for s in INPUT.scenarios
            )
        }
    )

    def load(path: Path, *, catalog_root: Path) -> FixturePack:
        return poisoned

    monkeypatch.setattr(generator, "load_provider_fixtures", load)
    generate(DATA, second)
    for filename in first.iterdir():
        assert filename.read_bytes() == (second / filename.name).read_bytes()
        if filename.name == "index.json":
            current = AnthropicIndex.model_validate_json(filename.read_bytes())
            recorded = AnthropicIndex.model_validate_json((DIRECTORY / filename.name).read_bytes())
            assert current.model_dump(exclude={"codec", "authoring"}) == recorded.model_dump(
                exclude={"codec", "authoring"}
            )
        else:
            assert filename.read_bytes() == (DIRECTORY / filename.name).read_bytes()
        assert b"DO-NOT-AUTHOR-ME" not in filename.read_bytes()
    assert len(list(first.iterdir())) == 54


@pytest.mark.parametrize(
    "change",
    [
        "digest",
        "root",
        "text",
        "source",
        "dropped_binding",
        "dropped_gap",
        "effect_gap",
        "artifact_gap",
        "session",
        "gate",
        "recipe_pin",
        "usage",
    ],
)
def test_manifest_and_native_source_binding_survive_json_roundtrip(
    tmp_path: Path, change: str
) -> None:
    copied = tmp_path / "target53"
    shutil.copytree(DATA, copied)
    path = copied / "anthropic/index.json"
    document = _OBJECT.validate_json(path.read_bytes())
    entries = cast(list[Object], document["entries"])
    entry = next(e for e in entries if e["scenario_id"] == "QA-D1-CANARY-TWO-TURN")
    if change == "digest":
        document["target_sha256"] = "0" * 64
    elif change == "dropped_binding":
        cast(list[JsonValue], entry["bindings"]).pop()
    elif change == "dropped_gap":
        entry = next(e for e in entries if e["scenario_id"] == "QA-NEW4-DOUBLE-MENTION-DEDUPE")
        entry["gaps"] = []
    elif change in ("effect_gap", "artifact_gap"):
        sid = (
            "QA-D6-LONG-TURN-CARD-LIFECYCLE"
            if change == "effect_gap"
            else "QA-D12-PDF-CLAIM-HAS-PDF"
        )
        entry = next(e for e in entries if e["scenario_id"] == sid)
        entry["gaps"] = []
    elif change in ("session", "gate"):
        field = "session_id" if change == "session" else "completion_gate"
        cast(list[Object], entry["bindings"])[0][field] = "foreign"
    elif change == "recipe_pin":
        entry["input"] = entries[-1]["input"]
    else:
        replay_pin = _OBJECT.validate_python(entry["replay"])
        replay_path = copied / "anthropic" / str(replay_pin["path"])
        replay = _OBJECT.validate_json(replay_path.read_bytes())
        if change == "source":
            replay["scenario_sha256"] = "0" * 64
        else:
            turns = cast(list[Object], replay["turns"])
            if change == "root":
                cast(list[Object], turns[0]["events"])[0]["id"] = "foreign-root"
            elif change == "usage":
                end = next(
                    e
                    for e in cast(list[Object], turns[0]["events"])
                    if e["type"] == "span.model_request_end"
                )
                cast(Object, end["model_usage"])["input_tokens"] = 49
            else:
                turns[0]["request_text"] = "foreign-text"
        content = json.dumps(replay).encode()
        replay_path.write_bytes(content)
        replay_pin["sha256"] = hashlib.sha256(content).hexdigest()
        entry["replay"] = replay_pin
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError):
        load_anthropic_fixtures(path, source_root=ROOT)


def test_sse_event_name_cannot_inject_another_event() -> None:
    with pytest.raises(ValidationError):
        WireFrame(payload={}, sse_event="agent.message\ndata: foreign")
    assert AnthropicIndex.model_validate_json((DIRECTORY / "index.json").read_bytes()) == PACK.index


@pytest.mark.asyncio
async def test_followup_uses_one_retained_sdk_client_and_native_session() -> None:
    entry, row = PACK.select("QA-D1-CANARY-TWO-TURN")
    tape = anthropic_scenario_tape(entry, row, codec=PACK.index.codec)
    async with anthropic_replay(tape, source_root=DIRECTORY, codec_root=ROOT) as replay:
        seen: list[str] = []
        for turn, binding in zip(row.turns, entry.bindings, strict=True):
            edge = LegacyTurnTransport(replay.client, binding.session_id)
            assert await edge.status() == "idle"
            lifecycle = RecordingLifecycle()
            state = await asyncio.wait_for(
                run_turn(
                    anthropic=replay.client,
                    session_id=binding.session_id,
                    user_message=turn.request_text,
                    lifecycle=lifecycle,
                    cancel=asyncio.Event(),
                    billing=BillingExempt(reason="headless-unrecorded"),
                    path="legacy",
                    render_interval_s=3600,
                    stream_read_timeout_s=5,
                ),
                timeout=15,
            )
            assert state.error is None and lifecycle.terminal_success
            assert lifecycle.terminal_failures == []
            seen.extend(str(e.model_dump(mode="json")["id"]) for e in lifecycle.sse_events)
        replay.wire.assert_consumed()
        assert seen == [e["id"] for t in row.turns for e in t.events]
        assert len(replay.wire.requests) == 6
        assert {r.path for r in replay.wire.requests} == {
            f"/v1/sessions/{entry.bindings[0].session_id}",
            f"/v1/sessions/{entry.bindings[0].session_id}/events",
            f"/v1/sessions/{entry.bindings[0].session_id}/events/stream",
        }


@pytest.mark.asyncio
async def test_event_named_stream_cannot_deliver_a_root_before_post_acceptance() -> None:
    entry, row = PACK.select("QA-NEW9-NO-INTERNAL-PLUMBING")
    turn = row.turns[0]
    tape = anthropic_turn_tape(entry, row, turn, codec=PACK.index.codec)
    binding = entry.bindings[0]
    async with anthropic_replay(tape, source_root=DIRECTORY, codec_root=ROOT) as replay:
        edge = LegacyTurnTransport(replay.client, binding.session_id)
        assert await edge.status() == "idle"
        stream = await edge.open_stream(read_timeout_s=5)
        first = asyncio.create_task(anext(stream))
        # One scheduling barrier, never a timeout/sleep assumption under load.
        barrier = asyncio.Event()
        asyncio.get_running_loop().call_soon(barrier.set)
        await barrier.wait()
        assert not first.done()
        await edge.send(
            [{"type": "user.message", "content": [{"type": "text", "text": turn.request_text}]}]
        )
        root = await first
        assert root.model_dump(mode="json")["id"] == binding.root_id
        records = [root, *[e async for e in stream]]
        await stream.close()
        replay.wire.assert_consumed()
        assert [e.model_dump(mode="json")["id"] for e in records] == [e["id"] for e in turn.events]


@pytest.mark.asyncio
async def test_usage_span_preserves_the_actual_tool_start_before_completion_gate() -> None:
    entry, row = PACK.select("QA-D6-LONG-TURN-CARD-LIFECYCLE")
    turn, binding = row.turns[0], entry.bindings[0]
    assert binding.completion_gate is not None
    tape = anthropic_turn_tape(entry, row, turn, codec=PACK.index.codec)
    async with anthropic_replay(tape, source_root=DIRECTORY, codec_root=ROOT) as replay:
        edge = LegacyTurnTransport(replay.client, binding.session_id)
        assert await edge.status() == "idle"
        await edge.send(
            [{"type": "user.message", "content": [{"type": "text", "text": turn.request_text}]}]
        )
        stream = await edge.open_stream(read_timeout_s=5)
        prefix_ready = asyncio.Event()
        seen: list[str] = []

        async def collect() -> None:
            async for record in stream:
                seen.append(str(record.type))
                if record.type == "agent.tool_use":
                    prefix_ready.set()

        task = asyncio.create_task(collect())
        try:
            await asyncio.wait_for(prefix_ready.wait(), timeout=15)
            barrier = asyncio.Event()
            asyncio.get_running_loop().call_soon(barrier.set)
            await barrier.wait()
            assert seen == [
                "user.message",
                "session.status_running",
                "span.model_request_start",
                "agent.tool_use",
            ]
            assert not task.done()
            replay.wire.release(binding.completion_gate)
            await task
            assert seen[-2:] == ["span.model_request_end", "session.status_idle"]
            replay.wire.assert_consumed()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await stream.close()


@pytest.mark.asyncio
async def test_codec_provenance_is_inert_but_actual_sdk_payloads_still_match() -> None:
    entry, row, turn = CASES[0]
    tape = anthropic_turn_tape(entry, row, turn, codec=PACK.index.codec)
    tape = tape.model_copy(
        update={"codec": PACK.index.codec.model_copy(update={"sha256": "0" * 64})}
    )
    async with anthropic_replay(tape, source_root=DIRECTORY, codec_root=ROOT) as replay:
        identity = entry.bindings[0].session_id
        status = await LegacyTurnTransport(replay.client, identity).status()
        assert status == "idle"
        assert replay.wire.requests[0].method == "GET"
        assert replay.wire.requests[0].path == tape.replies[0].path
        assert not replay.wire.violations
