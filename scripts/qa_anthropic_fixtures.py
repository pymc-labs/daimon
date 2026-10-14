"""Offline Anthropic SDK tapes from pinned native recipes, never from assertions."""

from __future__ import annotations

import argparse
import hashlib
import runpy
from collections.abc import Callable
from pathlib import Path
from typing import cast

from daimon.testing.anthropic_fixtures import (
    ARTIFACT_GAP,
    AnthropicBinding,
    AnthropicEntry,
    AnthropicIndex,
    AnthropicScenario,
    AnthropicTurn,
    anthropic_model_usage,
)
from daimon.testing.provider_fixtures import (
    FixtureGap,
    NativeTurn,
    ScenarioFixture,
    load_provider_fixtures,
)
from daimon.testing.provider_replay import Object, SourcePin
from pydantic import JsonValue, TypeAdapter

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "packages/testing/fixtures/target53"
CODEC_COMMIT = "ac1b72f0adb1aba7b5ed99b879bcab51f359e777"
CODEC = "packages/mux/mux/drivers/anthropic/transport.py"
# The commit identifies reviewed provenance; regeneration verifies checkout bytes.
CODEC_SHA256 = "34a56b293e3cfbe641125c37e142eebda5df1ab1859ba94da609c4e0afcdd457"
STAMP = "2026-10-10T00:00:00Z"
_OBJECT = TypeAdapter[Object](Object)
# Same authored artifact commands/content as QA-PROVIDER-ARTIFACTS 64b971b.
# No file bytes, remote execution or upload receipt is inferred from these commands.
_ARTIFACTS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "QA-D12-PDF-CLAIM-HAS-PDF": (
        (
            "Attached qa_pdf.pdf.",
            'python3 -c \'from pathlib import Path; Path("outputs").mkdir(exist_ok=True); '
            'from reportlab.pdfgen.canvas import Canvas; c=Canvas("outputs/qa_pdf.pdf"); '
            'c.drawString(72,720,"QA PDF qa-fixture-0001"); '
            'c.drawString(72,700,"A fixture sentence."); c.save()\'',
            "",
        ),
    ),
    "QA-D13-FILES-NOT-REPOSTED": (
        (
            "Attached qa_one.csv.",
            "mkdir -p outputs; printf '1\\n2\\n3\\n4\\n5\\n' > outputs/qa_one.csv",
            "",
        ),
        ("Attached qa_two.csv.", "printf '6\\n7\\n8\\n9\\n10\\n' > outputs/qa_two.csv", ""),
    ),
    "QA-NEW36-OVERSIZE-OUTPUT-KEPT": (
        (
            "Created outputs/qa_big_out.bin.",
            "mkdir -p outputs; head -c 12M /dev/urandom > outputs/qa_big_out.bin",
            "",
        ),
        (
            "SIZE=12582912",
            "ls -l outputs/qa_big_out.bin",
            "-rw-r--r-- 1 user user 12582912 Oct 10 00:00 outputs/qa_big_out.bin\n",
        ),
    ),
}


def pin(path: str, content: bytes) -> SourcePin:
    return SourcePin(path=path, sha256=hashlib.sha256(content).hexdigest())


def native_events(turn: NativeTurn) -> AnthropicTurn:
    root = "qa-anthropic:" + turn.root_id.removeprefix("qa-openai-")

    def event(suffix: str, tag: str, **fields: JsonValue) -> Object:
        return {"id": root + ":" + suffix, "type": tag, "processed_at": STAMP, **fields}

    events = [
        event("input", "user.message", content=[{"type": "text", "text": turn.request_text}]),
        event("running", "session.status_running"),
        event("model:start", "span.model_request_start"),
    ]
    for n, frame in enumerate(turn.frames):
        if frame.get("type") != "agent.session.turn.item.done":
            continue
        item = _OBJECT.validate_python(frame["item"])
        tag = item.get("type")
        if tag == "command_execution":
            use = event(
                f"bash:{n}",
                "agent.tool_use",
                name="bash",
                input={"command": item["command"]},
                evaluated_permission="allow",
            )
            events.extend(
                (
                    use,
                    event(
                        f"bash:{n}:result",
                        "agent.tool_result",
                        tool_use_id=use["id"],
                        content=[{"type": "text", "text": item["output"]}],
                        is_error=item["exit_code"] != 0,
                    ),
                )
            )
        elif tag == "mcp_call":
            use = event(
                f"mcp:{n}",
                "agent.mcp_tool_use",
                name=item["name"],
                input=item["arguments"],
                mcp_server_name=item["server_label"],
                evaluated_permission="allow",
            )
            result = _OBJECT.validate_python(item["output"])
            events.extend(
                (
                    use,
                    event(
                        f"mcp:{n}:result",
                        "agent.mcp_tool_result",
                        mcp_tool_use_id=use["id"],
                        content=result["content"],
                        is_error=result["isError"],
                    ),
                )
            )
        elif tag == "message" and item.get("role") == "assistant":
            content = item["content"]
            if not isinstance(content, list):
                raise ValueError("authored assistant content is not a list")
            blocks = [_OBJECT.validate_python(part) for part in content]
            if any(block.get("type") != "output_text" for block in blocks):
                raise ValueError("no Anthropic recipe for this native assistant block")
            events.append(
                event(
                    f"answer:{n}",
                    "agent.message",
                    content=[{"type": "text", "text": block["text"]} for block in blocks],
                )
            )
        elif tag != "message" or item.get("role") != "user":
            raise ValueError("no Anthropic recipe for this native item")
    events.extend(
        (
            event(
                "model:end",
                "span.model_request_end",
                model_request_start_id=root + ":model:start",
                model_usage=anthropic_model_usage(turn),
                is_error=False,
            ),
            event("ended", "session.status_idle", stop_reason={"type": "end_turn"}),
        )
    )
    return AnthropicTurn(turn=turn.turn, request_text=turn.request_text, events=tuple(events))


def artifact_turn(source: ScenarioFixture, invocation: Object, index: int) -> NativeTurn:
    # Shared native builder keeps request/session/thread/root numbering identical.
    module = runpy.run_path(str(ROOT / "scripts/qa_provider_fixtures.py"))
    native_turn = cast(Callable[..., NativeTurn], module["native_turn"])

    answer, command, output = _ARTIFACTS[source.scenario_id][index]
    return native_turn(
        "openai",
        sid=source.scenario_id,
        invocation=invocation,
        index=index,
        previous=None,
        answer=answer,
        commands=((command, output),),
    )


def generate(data: Path, output: Path) -> AnthropicIndex:
    pack = load_provider_fixtures(data / "index.json", catalog_root=data / "catalog")
    codec = SourcePin(path=CODEC, sha256=CODEC_SHA256)
    codec.resolve(ROOT)
    entries: list[AnthropicEntry] = []
    output.mkdir(parents=True, exist_ok=True)
    for source in pack.scenarios:
        fixture = next(p for p in source.providers if p.backend == "openai")
        native = list(fixture.turns)
        gaps = [g for g in fixture.gaps if g.code not in ("MCP_SERVER_LIMIT", "MCP_AUTH_UNBOUND")]
        if source.scenario_id in _ARTIFACTS and not native:
            triggers = [i for i in source.invocations if i.get("operation") == "host_turn"]
            native = [artifact_turn(source, invocation, n) for n, invocation in enumerate(triggers)]
            gaps = [g for g in gaps if g.code != "ARTIFACT_PROTOCOL_UNBOUND"]
            gaps.extend(
                FixtureGap(
                    code="RUNNER_HOOK",
                    location=t.location,
                    reason=ARTIFACT_GAP,
                )
                for t in native
            )
        # Missing admin/context/platform/approval evidence remains the runner's gap.
        if not native and not gaps:
            gaps.append(
                FixtureGap(
                    code="ADMIN_READBACK_ONLY",
                    location="scenario",
                    reason="No automatic catalog host trigger is available for replay.",
                )
            )
        turns = tuple(native_events(t) for t in native)
        row = AnthropicScenario(
            scenario_id=source.scenario_id,
            scenario_sha256=source.source.sha256,
            values=source.values,
            turns=turns,
        )
        content = (row.model_dump_json(indent=2) + "\n").encode()
        filename = source.scenario_id + ".json"
        (output / filename).write_bytes(content)
        entries.append(
            AnthropicEntry(
                scenario_id=source.scenario_id,
                replay=pin(filename, content),
                input=pin(
                    f"native/{source.scenario_id}.json", (data / "native" / filename).read_bytes()
                ),
                bindings=tuple(
                    AnthropicBinding(
                        turn=t.turn,
                        location=t.location,
                        session_id=t.session_id,
                        root_id=str(a.events[0]["id"]),
                        completion_gate=t.completion_gate,
                    )
                    for t, a in zip(native, turns, strict=True)
                ),
                gaps=tuple(gaps),
            )
        )
    manifest = AnthropicIndex(
        codec=codec,
        codec_commit=CODEC_COMMIT,
        authoring=pin("scripts/qa_anthropic_fixtures.py", Path(__file__).read_bytes()),
        input_index=pin("index.json", (data / "index.json").read_bytes()),
        entries=tuple(entries),
    )
    (output / "index.json").write_text(manifest.model_dump_json(indent=2) + "\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DATA)
    parser.add_argument("--output", type=Path, default=DATA / "anthropic")
    args = parser.parse_args()
    manifest = generate(args.input, args.output)
    print(
        f"Wrote {len(manifest.entries)} Anthropic scenario bindings, "
        f"{sum(len(e.bindings) for e in manifest.entries)} authored native turns; "
        "no outcome verdicts"
    )


if __name__ == "__main__":
    main()
