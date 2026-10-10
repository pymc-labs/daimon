"""Author TARGET-53 native SDK fixtures from pinned source, never from assertions.

This generator runs offline. Recipes are explicit scenario inputs; fixture gaps
remain gaps. The output has no outcome/verdict fields and never executes tools.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import cast

import yaml
from daimon.testing.provider_fixtures import (
    AnthropicSource,
    BackendFixture,
    FixtureGap,
    FixtureIndex,
    FixturePack,
    NativeTurn,
    ScenarioFixture,
)
from daimon.testing.provider_replay import Backend, Object, SourcePin
from pydantic import JsonValue, TypeAdapter

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "packages/testing/fixtures/target53"
OBJECT: TypeAdapter[Object] = TypeAdapter(Object)
STAMP = "2026-10-10T00:00:00Z"
VALUES = {
    "nonce": "qa-fixture-0001",
    "guild_id": "qa-guild",
    "channel_id": "qa-channel",
    "channel:A": "qa-channel-a",
    "channel:B": "qa-channel-b",
    "fork.name": "qa-channel-agent",
    "env.default_agent": "daimon",
}


def pin(path: str, content: bytes) -> SourcePin:
    return SourcePin(path=path, sha256=hashlib.sha256(content).hexdigest())


def gap(code: str, location: str, reason: str) -> FixtureGap:
    return FixtureGap.model_validate({"code": code, "location": location, "reason": reason})


def recipe(sid: str, index: int) -> tuple[str | None, tuple[tuple[str, str], ...]]:
    """Hand-authored task replies. No assertion pattern or verdict is consulted."""
    short: dict[str, tuple[str, ...]] = {
        "QA-D11-FOOTER-COST-MATCHES-LEDGER": ("FOOTER-OK",),
        "QA-D1-REPLACE-NOTICE-HIDDEN-FOR-SKILLS": ("SAVED", "KEEP=qa-fixture-0001"),
        "QA-D1-CANARY-TWO-TURN": ("CODE=TANGERINE-4471", "FILE=a1b2c3d4e5f6"),
        "QA-D6-LONG-TURN-CARD-LIFECYCLE": ("SLEPT",),
        "QA-D7-BIG-WORKSPACE-CHECKPOINT": ("BIG-OK", "PONG"),
        "QA-NEW12-AGENT-SWITCH-HANDOFF-COPY": ("H1", "H2", "H3", "qa-channel-agent"),
        "QA-NEW15-CHANNEL-BUDGET-EXHAUSTED": ("TINY-OK",),
        "QA-NEW19-SKILL-DELETE-WHILE-ATTACHED": ("NOT-STRANDED",),
        "QA-NEW1-PDF-BARE-MENTION-NO-DROP": ("I can help review the attached document.",),
        "QA-NEW2-CSV-ATTACHMENT-READ": ("TOP=west",),
        "QA-NEW3-BURST-CHANNEL-MENTIONS": ("B1", "B2", "B3"),
        "QA-NEW4-DOUBLE-MENTION-DEDUPE": ("DUP-OK",),
        "QA-NEW5-THREAD-QUEUE-WHILE-BUSY": ("FIRST-DONE", "SECOND-DONE"),
        "QA-NEW6-TENANT-CAP-QUEUE": ("C1", "C2", "C3"),
        "QA-I6-BARE-MENTION-THREAD-TITLE": ("Thursday at 3pm works for the Q4 review.",),
        "QA-NEW8-LINK-PAGE-THREAD-TITLE": ("Please paste the version changes to review.",),
        "QA-NEW9-NO-INTERNAL-PLUMBING": (
            "I can help you analyze data, write code, and review documents.",
        ),
        # Send ordinary markdown to the real renderer, not a pre-rendered expected answer.
        "QA-NEW37-MARKDOWN-TABLE-READABLE": (
            "| Fruit | Colour | Price |\n|---|---|---|\n| Apple | Red | $1 |\n"
            "| Banana | Yellow | $2 |\n| Orange | Orange | $3 |\n| Pear | Green | $4 |",
        ),
    }
    commands: dict[tuple[str, int], tuple[tuple[str, str], ...]] = {
        ("QA-I3-TOOL-ONLY-NO-EMPTY-RESPONSE", 0): (("true", ""),),
        ("QA-D6-LONG-TURN-CARD-LIFECYCLE", 0): (("sleep 45", ""),),
        ("QA-NEW5-THREAD-QUEUE-WHILE-BUSY", 0): (("sleep 30", ""),),
        ("QA-NEW6-TENANT-CAP-QUEUE", 0): (("sleep 30", ""),),
        ("QA-NEW6-TENANT-CAP-QUEUE", 1): (("sleep 30", ""),),
        ("QA-D7-BIG-WORKSPACE-CHECKPOINT", 0): (("head -c 40M /dev/urandom > qa_big.bin", ""),),
        ("QA-D1-REPLACE-NOTICE-HIDDEN-FOR-SKILLS", 0): (
            ("echo qa-fixture-0001 > qa_keep.txt", ""),
        ),
        ("QA-D1-REPLACE-NOTICE-HIDDEN-FOR-SKILLS", 1): (("cat qa_keep.txt", "qa-fixture-0001\n"),),
        ("QA-D1-CANARY-TWO-TURN", 0): (
            ('python3 -c "import secrets;print(secrets.token_hex(6))" > qa_canary.txt', ""),
        ),
        ("QA-D1-CANARY-TWO-TURN", 1): (("cat qa_canary.txt", "a1b2c3d4e5f6\n"),),
    }
    if sid == "QA-I3-TOOL-ONLY-NO-EMPTY-RESPONSE":
        return None, commands[(sid, index)]
    if sid == "QA-NEW10-SPLIT-ANSWER-AND-CHUNK-REPLY":
        if index:
            return "CHUNK-OK", ()
        lines = ["class Inventory:", '    """Toy inventory used by the QA fixture."""']
        lines.extend(
            f"    def item_{i}(self): return {i}  # inventory entry {i}" for i in range(118)
        )
        return "```python\n" + "\n".join(lines) + "\n```\n" + "\n".join(
            (
                "- Stores entries.",
                "- Lists items.",
                "- Counts stock.",
                "- Uses methods.",
                "- Includes docs.",
            )
        ), ()
    if sid not in short or index >= len(short[sid]):
        raise ValueError("no authored native recipe for this invocation")
    return short[sid][index], commands.get((sid, index), ())


def native_turn(
    backend: Backend,
    *,
    sid: str,
    invocation: Object,
    index: int,
    previous: NativeTurn | None,
    answer: str | None,
    commands: tuple[tuple[str, str], ...],
) -> NativeTurn:
    number = cast(int, invocation["turn"])
    channel, thread = str(invocation["channel"]), str(invocation["thread"])
    text = str(invocation["text"])
    for name, value in VALUES.items():
        text = text.replace("{" + name + "}", value)
    namespace = hashlib.sha256((sid + channel + thread).encode()).hexdigest()[:16]
    session, root = f"qa-session-{namespace}", f"qa-{backend}-{namespace}-turn-{number}"
    frames: list[Object] = []
    if backend == "openai":
        request: Object = {
            "events": [
                {
                    "type": "agent.session.input.message",
                    "input": [{"role": "user", "content": [{"type": "input_text", "text": text}]}],
                }
            ]
        }
        snapshot: Object = {
            "id": root,
            "session_id": session,
            "agent_id": "qa-agent",
            "subagent_id": None,
            "status": "completed",
            "created_at": 0,
            "usage": {
                "input_tokens": 64,
                "input_tokens_details": {"cached_tokens": 16},
                "output_tokens": 8,
                "output_tokens_details": {"reasoning_tokens": 3},
            },
        }

        def event(suffix: str, tag: str, **fields: JsonValue) -> Object:
            return {
                "type": "agent.session." + suffix,
                "event_id": root + ":" + tag,
                "session_id": session,
                "turn": snapshot,
                **fields,
            }

        frames.append(
            event("turn.in_progress", "running", turn={**snapshot, "status": "in_progress"})
        )
        frames.append(
            event(
                "turn.item.done",
                "input",
                item={
                    "id": root + ":input",
                    "turn_id": root,
                    "type": "message",
                    "role": "user",
                    "status": "completed",
                    "content": [{"type": "input_text", "text": text}],
                },
            )
        )
        for n, (command, output) in enumerate(commands):
            frames.append(
                event(
                    "turn.item.done",
                    f"tool:{n}",
                    item={
                        "id": root + f":tool:{n}",
                        "turn_id": root,
                        "type": "command_execution",
                        "status": "completed",
                        "command": command,
                        "cwd": None,
                        "exit_code": 0,
                        "output": output,
                    },
                )
            )
        if answer is not None:
            frames.append(
                event(
                    "turn.item.done",
                    "answer",
                    item={
                        "id": root + ":answer",
                        "turn_id": root,
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": answer}],
                    },
                )
            )
        frames.append(event("turn.completed", "end"))
    else:
        request = {
            "agent": "antigravity-preview-05-2026",
            "agent_config": {"type": "antigravity", "model": "gemini-3.8-flash"},
            "environment": {"type": "remote", "sources": []},
            "input": [{"type": "text", "text": text}],
            "background": True,
            "store": True,
        }
        if previous is not None and previous.session_id == session:
            request["previous_interaction_id"] = previous.root_id
            request["environment"] = f"qa-workspace-{namespace}"
        steps: list[JsonValue] = []
        for n, (command, output) in enumerate(commands):
            call = root + f":tool:{n}"
            steps.extend(
                (
                    {"id": call, "type": "code_execution_call", "arguments": {"command": command}},
                    {
                        "id": call + ":result",
                        "type": "code_execution_result",
                        "call_id": call,
                        "result": output,
                        "is_error": False,
                    },
                )
            )
        if answer is not None:
            steps.append(
                {
                    "id": root + ":answer",
                    "type": "model_output",
                    "content": [{"type": "text", "text": answer}],
                }
            )
        snapshot = {
            "id": root,
            "created": STAMP,
            "updated": STAMP,
            "status": "completed",
            "environment_id": f"qa-workspace-{namespace}",
            "steps": steps,
            "usage": {
                "total_input_tokens": 64,
                "total_cached_tokens": 16,
                "total_output_tokens": 8,
                "total_thought_tokens": 3,
            },
        }
        frames.append(
            {
                "event_type": "interaction.completed",
                "event_id": root + ":end",
                "interaction": {"id": root, "status": "completed"},
            }
        )
    return NativeTurn(
        turn=number,
        location=str(invocation["location"]),
        channel=channel,
        thread=thread,
        request_text=text,
        session_id=session,
        root_id=root,
        request=request,
        frames=tuple(frames),
        snapshot=snapshot,
        completion_gate=f"turn.{number}.tool.finished"
        if any(command.startswith("sleep ") for command, _ in commands)
        else None,
    )


def objects(value: JsonValue) -> list[Object]:
    if not isinstance(value, list):
        raise ValueError("expected matrix objects")
    return [OBJECT.validate_python(item) for item in value]


def generate(catalog: Path, matrix: Object) -> FixturePack:
    ids = (catalog / "TARGET-53.txt").read_text().splitlines()
    plans = {
        str(p["scenario"]["id"]): p
        for p in objects(matrix["plans"])
        if p["backend"] == "openai" and isinstance(p["scenario"], dict)
    }
    sources = {
        "openai": ("nc/openai-host-turn-codec", "packages/core/daimon/core/turn/openai_codec.py"),
        "gemini": ("nc/gemini-host-codec", "packages/core/daimon/core/turn/gemini.py"),
    }
    sdk = {
        "openai": ("agents-sessions", "openai", "2.54.0"),
        "gemini": ("interactions", "google-genai", "2.7.0"),
    }
    anthropic_path = ROOT / "tests/judge/fixtures/catalog_anthropic.json"
    anthropic = objects(cast(JsonValue, json.loads(anthropic_path.read_bytes())))
    anth_ids = {str(s["scenario_id"]) for s in anthropic}
    scenarios: list[ScenarioFixture] = []
    for sid in ids:
        plan = plans[sid]
        entry = OBJECT.validate_python(plan["scenario"])
        document = OBJECT.validate_python(entry["scenario"])
        document.pop("sources", None)
        filename = str(entry["filename"])
        source = pin("scenarios/" + filename, (catalog / "scenarios" / filename).read_bytes())
        if source.sha256 != entry["sha256"]:
            raise ValueError("catalog differs from the planner source pin")
        providers: list[BackendFixture] = []
        invocations = tuple(objects(plan["invocations"]))
        assets = tuple(
            {
                str(i["fixture"]): SourcePin(
                    path=str(i["fixture"]), sha256=str(i["fixture_sha256"])
                )
                for i in invocations
                if i.get("fixture") and i.get("fixture_sha256")
            }.values()
        )
        for backend in cast(tuple[Backend, ...], ("openai", "gemini")):
            gaps: list[FixtureGap] = []
            turns: list[NativeTurn] = []
            host = [i for i in invocations if i["operation"] == "host_turn"]
            if not host:
                gaps.append(
                    gap(
                        "MANUAL_TRIGGER" if document.get("human") else "ADMIN_READBACK_ONLY",
                        "steps",
                        "Source has no automated host trigger; do not invent one from a checklist.",
                    )
                )
            for i in invocations:
                if i["operation"] in ("unbound", "context", "wait") or i.get("fixture"):
                    gaps.append(
                        gap(
                            "RUNNER_HOOK",
                            str(i["location"]),
                            "Runner must bind the preserved operation or asset.",
                        )
                    )
            for index, invocation in enumerate(host):
                if (
                    sid
                    in (
                        "QA-NEW13-WRITERS-NONE-NOT-SILENT",
                        "QA-NEW14-MISSING-THREAD-PERMISSION-COPY",
                        "QA-NEW16-TENANT-CREDIT-DEPLETED-COPY",
                    )
                    or index == 1
                    and sid
                    in ("QA-NEW15-CHANNEL-BUDGET-EXHAUSTED", "QA-NEW4-DOUBLE-MENTION-DEDUPE")
                ):
                    gaps.append(
                        gap(
                            "HOST_GATE",
                            str(invocation["location"]),
                            "Host must reject/dedupe before dispatch; no provider reply.",
                        )
                    )
                    continue
                try:
                    answer, commands = recipe(sid, index)
                except ValueError:
                    code = (
                        "ARTIFACT_PROTOCOL_UNBOUND"
                        if sid
                        in (
                            "QA-D12-PDF-CLAIM-HAS-PDF",
                            "QA-D13-FILES-NOT-REPOSTED",
                            "QA-NEW36-OVERSIZE-OUTPUT-KEPT",
                        )
                        else "NATIVE_TOOL_SCHEMA_UNBOUND"
                    )
                    gaps.append(
                        gap(
                            code,
                            str(invocation["location"]),
                            "Native tool/artifact recipe needs the owner's real schema.",
                        )
                    )
                    continue
                turns.append(
                    native_turn(
                        backend,
                        sid=sid,
                        invocation=invocation,
                        index=index,
                        previous=turns[-1] if turns else None,
                        answer=answer,
                        commands=commands,
                    )
                )
                if commands:
                    gaps.append(
                        gap(
                            "RUNNER_HOOK",
                            str(invocation["location"]),
                            "Remote tool/workspace effects remain uncaptured.",
                        )
                    )
                if any(command.startswith("sleep ") for command, _ in commands):
                    gaps.append(
                        gap(
                            "RUNNER_HOOK",
                            str(invocation["location"]),
                            "Runner must gate completion with its clock/concurrency hook.",
                        )
                    )
                raw_step = invocation["source"]
                if isinstance(raw_step, dict) and raw_step.get("do") == "burst":
                    gaps.append(
                        gap(
                            "RUNNER_HOOK",
                            str(invocation["location"]),
                            "Burst requires actual overlap/routing.",
                        )
                    )
            ref, module = sources[backend]
            commit = subprocess.check_output(["git", "rev-parse", ref], cwd=ROOT, text=True).strip()
            content = subprocess.check_output(["git", "show", f"{commit}:{module}"], cwd=ROOT)
            family, distribution, sdk_version = sdk[backend]
            providers.append(
                BackendFixture.model_validate(
                    {
                        "backend": backend,
                        "profile": f"{backend}."
                        + ("persistent_workspace" if backend == "openai" else "inline_reuse"),
                        "model": "gpt-6-luna" if backend == "openai" else "gemini-3.8-flash",
                        "api_family": family,
                        "sdk_distribution": distribution,
                        "sdk_version": sdk_version,
                        "codec_source": pin(module, content).model_dump(mode="json"),
                        "codec_commit": commit,
                        "turns": [t.model_dump(mode="json") for t in turns],
                        "gaps": [g.model_dump(mode="json") for g in gaps],
                    }
                )
            )
        scenarios.append(
            ScenarioFixture(
                scenario_id=sid,
                source=source,
                projection=pin(source.path, yaml.safe_dump(document, sort_keys=False).encode()),
                scenario=document,
                values=VALUES,
                assets=assets,
                invocations=invocations,
                providers=tuple(providers),
                anthropic=AnthropicSource(
                    pin=pin(str(anthropic_path.relative_to(ROOT)), anthropic_path.read_bytes()),
                    scenario_id=sid,
                    source_kind="catalog-authored",
                )
                if sid in anth_ids
                else None,
                anthropic_gaps=()
                if sid in anth_ids
                else (
                    gap(
                        "ANTHROPIC_SOURCE_UNBOUND",
                        "anthropic",
                        "N9 tapes do not bind this source scenario; no golden substitution.",
                    ),
                ),
            )
        )
    return FixturePack(
        target=pin("TARGET-53.txt", (catalog / "TARGET-53.txt").read_bytes()),
        scenarios=tuple(scenarios),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--matrix", type=Path, required=True)
    args = parser.parse_args()
    pack = generate(args.catalog, OBJECT.validate_json(args.matrix.read_bytes()))
    OUTPUT.mkdir(parents=True, exist_ok=True)
    native_pins: list[SourcePin] = []
    for scenario in pack.scenarios:
        path = f"native/{scenario.scenario_id}.json"
        content = (scenario.model_dump_json(indent=2) + "\n").encode()
        (OUTPUT / "native").mkdir(exist_ok=True)
        (OUTPUT / path).write_bytes(content)
        native_pins.append(pin(path, content))
    index = FixtureIndex(target=pack.target, scenarios=tuple(native_pins))
    (OUTPUT / "index.json").write_text(index.model_dump_json(indent=2) + "\n")
    for source in (
        pack.target,
        *(a for s in pack.scenarios for a in s.assets),
    ):
        destination = OUTPUT / "catalog" / source.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((args.catalog / source.path).read_bytes())
    for scenario in pack.scenarios:
        destination = OUTPUT / "catalog" / scenario.projection.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(yaml.safe_dump(scenario.scenario, sort_keys=False))
    print(
        f"Wrote {len(pack.scenarios)} source-pinned scenarios, "
        f"{sum(len(p.turns) for s in pack.scenarios for p in s.providers)} native turns"
    )


if __name__ == "__main__":
    main()
