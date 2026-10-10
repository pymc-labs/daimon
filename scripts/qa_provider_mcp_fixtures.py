"""Extend the signed TARGET-53 pack with authored native MCP recipes, offline.

Registry inspection performs registration only. Tools are never invoked here.
The frozen source pack, not a moving external catalog, defines the scenarios.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import subprocess
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Protocol, cast

from daimon.testing.provider_fixtures import (
    BackendFixture,
    FixtureGap,
    FixtureIndex,
    FixturePack,
    MCPFixtureBinding,
    NativeMCPCall,
    NativeTurn,
    ScenarioFixture,
    load_provider_fixtures,
)
from daimon.testing.provider_replay import Object, SourcePin
from jsonschema import Draft202012Validator
from mux.contracts.resources import MCPConnection
from pydantic import JsonValue, TypeAdapter

from scripts.generate_mcp_tool_catalogue import (
    _collect_surfaces,  # pyright: ignore[reportPrivateUsage, reportUnknownVariableType]
    _isolated_env,  # pyright: ignore[reportPrivateUsage]
)
from scripts.qa_provider_fixtures import native_turn

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "packages/testing/fixtures/target53"
AUTH_COMMIT = "9cf23f6c1eaf1d269ed9cab3f281ddd0cc9a6b82"
OBJECT = TypeAdapter[Object](Object)
SCENARIOS = frozenset(
    {
        "QA-D22-WHO-ANSWERS-HERE",
        "QA-D5-UNBOUND-AGENT-EDIT-REFUSED",
        "QA-D8-GITHUB-CONNECT-OFFERED",
        "QA-ISO-TEAM-CHANNEL-ISOLATION",
        "QA-NEW17-MCP-USABLE-NEXT-MESSAGE",
        "QA-NEW18-SKILL-ADD-YES-PLEASE",
        "QA-NEW21-FORM-EXPIRY-UPDATES-CARD",
        "QA-NEW22-ROUTINE-OUTPUT-NOT-TRUNCATED",
        "QA-NEW23-ROUTINE-WITHOUT-DESTINATION",
        "QA-NEW35-ROUTINE-FAILURE-IS-REPORTED",
    }
)
TOOL_NAMES = frozenset(
    {
        "get_agent",
        "update_agent",
        "attach_mcp_server",
        "github_connect",
        "read_channel",
        "search_messages",
        "add_skill",
        "request_agent_key",
        "create_routine",
    }
)


class RegisteredTool(Protocol):
    name: str
    parameters: object
    output_schema: object
    fn: object


class SchemaValidator(Protocol):
    def validate(self, instance: Object, /) -> None: ...


def pin(path: str, body: bytes) -> SourcePin:
    return SourcePin(path=path, sha256=hashlib.sha256(body).hexdigest())


async def registry() -> Object:
    # Reuse exactly the existing catalogue's fully configured, scrubbed build.
    # These private authoring helpers never enter the reusable fixture runtime.
    collect = cast(
        Callable[[], Awaitable[tuple[Sequence[RegisteredTool], Sequence[RegisteredTool]]]],
        _collect_surfaces,
    )
    with _isolated_env():
        tools, _ = await collect()
    result: Object = {}
    for tool in tools:
        if tool.name not in TOOL_NAMES:
            continue
        module = inspect.getmodule(tool.fn)
        if module is None or module.__file__ is None:
            raise ValueError("tool source could not be pinned")
        source = Path(module.__file__)
        result[tool.name] = {
            "name": tool.name,
            "inputSchema": OBJECT.validate_python(tool.parameters),
            "outputSchema": OBJECT.validate_python(tool.output_schema)
            if tool.output_schema is not None
            else None,
            "source": pin(str(source.relative_to(ROOT)), source.read_bytes()).model_dump(
                mode="json"
            ),
        }
    if set(result) != set(TOOL_NAMES):
        raise ValueError("the configured registry omitted a required fixture tool")
    return result


def result(text: str, *, error: bool = False) -> Object:
    return {"content": [{"type": "text", "text": text}], "isError": error}


def recipe(
    scenario: ScenarioFixture, index: int, invocation: Object
) -> tuple[str, tuple[tuple[str, str], ...], tuple[NativeMCPCall, ...]]:
    sid = scenario.scenario_id
    nonce = scenario.values["nonce"]
    channel = str(invocation["channel"])
    origin = f"qa-origin-{sid}-{invocation['turn']}"
    calls: list[NativeMCPCall] = []

    def call(
        name: str,
        arguments: Object,
        output: str,
        *,
        error: bool = False,
        server: str = "daimon-mcp",
    ) -> None:
        calls.append(
            NativeMCPCall(
                call_id=f"qa-call-{sid}-{invocation['turn']}-{len(calls)}",
                server=server,
                name=name,
                arguments=arguments,
                result=result(output, error=error),
                schema_origin="daimon-registry" if server == "daimon-mcp" else "authored-external",
            )
        )

    commands: tuple[tuple[str, str], ...] = ()
    if sid == "QA-D22-WHO-ANSWERS-HERE":
        if index == 0:
            call(
                "get_agent",
                {"name": "daimon", "origin_context_id": origin},
                '{"name":"daimon","answering_agent_name":"qa-channel-agent"}',
            )
            answer = "I am daimon. qa-channel-agent answers in this channel."
        else:
            call(
                "read_channel",
                {"channel_id": channel, "origin_context_id": origin},
                f'{{"messages":[{{"content":"QA-SEED-{nonce} is the release codename."}}]}}',
            )
            answer = f"QA-SEED-{nonce}"
    elif sid == "QA-D5-UNBOUND-AGENT-EDIT-REFUSED":
        name = f"qa-unbound-{nonce}"
        call(
            "update_agent",
            {"name": name, "description": "qa probe", "origin_context_id": origin},
            "This agent is not bound to the caller's channel. Nothing changed.",
            error=True,
        )
        call(
            "attach_mcp_server",
            {
                "agent_name": name,
                "server_name": "deepwiki",
                "url": "https://mcp.deepwiki.com/mcp",
                "origin_context_id": origin,
            },
            "This agent is not bound to the caller's channel. Nothing changed.",
            error=True,
        )
        answer = "I cannot edit that agent from this channel. Ask an admin to bind it here first."
    elif sid == "QA-D8-GITHUB-CONNECT-OFFERED":
        call(
            "github_connect",
            {"origin_context_id": origin, "agent_name": "qa-channel-agent"},
            '{"status":"sent","message":"A private connection button was offered to the admin."}',
        )
        answer = "Use the private connection button to connect GitHub."
    elif sid == "QA-ISO-TEAM-CHANNEL-ISOLATION":
        if index == 0:
            call(
                "read_channel",
                {"channel_id": channel, "origin_context_id": origin},
                f'{{"messages":[{{"content":"QAISO_PRIVATE_B_{nonce}"}}]}}',
            )
            commands = ((f"printf %s QAISO_PRIVATE_B_{nonce} > qa_iso_b.txt", ""),)
            answer = f"QAISO_PRIVATE_B_{nonce}"
        elif index == 1:
            other = scenario.values["channel:B"]
            call(
                "read_channel",
                {"channel_id": other, "origin_context_id": origin},
                "The channel is outside the caller's readable scope.",
                error=True,
            )
            call(
                "search_messages",
                {"content": "QAISO_PRIVATE_B", "channel_ids": [other], "origin_context_id": origin},
                '{"messages":[]}',
            )
            answer = "FOUND=NONE"
        else:
            commands = (
                ("cat qa_iso_b.txt; ls /mnt", "cat: qa_iso_b.txt: No such file or directory\n"),
            )
            answer = "FILE=NONE"
    elif sid == "QA-NEW17-MCP-USABLE-NEXT-MESSAGE":
        if index == 0:
            call(
                "attach_mcp_server",
                {
                    "agent_name": "qa-channel-agent",
                    "server_name": "deepwiki",
                    "url": "https://mcp.deepwiki.com/mcp",
                    "origin_context_id": origin,
                },
                '{"name":"qa-channel-agent","mcp_servers":[{"name":"deepwiki","url":"https://mcp.deepwiki.com/mcp"}]}',
            )
            answer = "Attached deepwiki; its tools will be available on your next message."
        else:
            call(
                "ask_question",
                {"repoName": "pymc-labs/pymc-marketing", "question": "What is this repository?"},
                "A Python package for Bayesian marketing models built with PyMC.",
                server="deepwiki",
            )
            answer = (
                "pymc-marketing is a Python package for Bayesian marketing models built with PyMC."
            )
    elif sid == "QA-NEW18-SKILL-ADD-YES-PLEASE":
        skill = (DATA / "catalog/fixtures/SKILL.md").read_text()
        if index < 2:
            args: Object = {
                "agent_name": "qa-channel-agent",
                "skill_md": skill,
                "origin_context_id": origin,
            }
            if index == 1:
                from daimon.core.skills.ingest import bundle_from_markdown

                args["content_hash"] = bundle_from_markdown(skill).preview.content_hash
            call(
                "add_skill",
                args,
                "Preview the qa-echo-skill bundle before confirming."
                if index == 0
                else "Confirmation is linked to the preview and this follow-up; "
                "approval capture is required.",
            )
            answer = (
                "Preview: qa-echo-skill. Confirm to add it."
                if index == 0
                else "I requested confirmation for qa-echo-skill."
            )
        else:
            commands = (("cat .agents/skills/qa-echo-skill/SKILL.md", skill),)
            answer = "ECHO-SKILL-OK"
    elif sid == "QA-NEW21-FORM-EXPIRY-UPDATES-CARD":
        call(
            "request_agent_key",
            {
                "agent_name": "qa-channel-agent",
                "purpose": "Use Acme API",
                "channel_id": channel,
                "origin_context_id": origin,
                "expected_ma_agent_id": "qa-agent",
                "key": "ACME_API_KEY",
            },
            "The private form request was created; the adapter must deliver it to the requester.",
        )
        answer = "Use the private form to supply ACME_API_KEY."
    elif sid in {
        "QA-NEW22-ROUTINE-OUTPUT-NOT-TRUNCATED",
        "QA-NEW23-ROUTINE-WITHOUT-DESTINATION",
        "QA-NEW35-ROUTINE-FAILURE-IS-REPORTED",
    }:
        if sid == "QA-NEW22-ROUTINE-OUTPUT-NOT-TRUNCATED":
            task = (
                "Save ROUTINE-MEM to memory note qa_routine.md; on failure say "
                "MEMRY-FAIL-X plus the error. Reply with numbers 1 to 400 "
                "separated by spaces. Do not call send_message."
            )
        elif sid == "QA-NEW23-ROUTINE-WITHOUT-DESTINATION":
            task = f"Say ROUTINE-PING-{nonce}."
        else:
            task = (
                "curl -sL https://example.com -o qa.pdf; read qa.pdf as a PDF document "
                f"and reply ROUTINE-RAN-{nonce} plus one sentence."
            )
        args = {
            "agent_name": "qa-channel-agent",
            "cron_expr": "* * * * *",
            "timezone": "UTC",
            "trigger_message": task,
            "origin_context_id": origin,
        }
        if sid != "QA-NEW23-ROUTINE-WITHOUT-DESTINATION":
            args.update({"destination_kind": "channel", "destination_id": channel})
        call(
            "create_routine",
            args,
            "Routine creation reply received; scheduling and delivery require host capture.",
        )
        answer = "The routine request is configured to run every minute."
    else:
        raise ValueError("scenario has no authored MCP recipe")
    return answer, commands, tuple(calls)


def add_calls(turn: NativeTurn, calls: tuple[NativeMCPCall, ...], backend: str) -> NativeTurn:
    if backend == "openai":
        frames = list(turn.frames)
        offset = 2
        for call in calls:
            item: Object = {
                "id": call.call_id,
                "type": "mcp_call",
                "turn_id": turn.root_id,
                "name": call.name,
                "server_label": call.server,
                "arguments": call.arguments,
                "output": call.result,
                "error": None,
                "status": "completed",
            }
            frames.insert(
                offset,
                {
                    "type": "agent.session.turn.item.done",
                    "event_id": call.call_id,
                    "session_id": turn.session_id,
                    "turn": turn.snapshot,
                    "item": item,
                },
            )
            offset += 1
        return turn.model_copy(update={"mcp_calls": calls, "frames": tuple(frames)})
    steps: list[JsonValue] = []
    for call in calls:
        steps.extend(
            (
                {
                    "id": call.call_id,
                    "type": "mcp_server_tool_call",
                    "name": call.name,
                    "server_name": call.server,
                    "arguments": call.arguments,
                },
                {
                    "id": call.call_id + ":result",
                    "type": "mcp_server_tool_result",
                    "call_id": call.call_id,
                    "result": call.result,
                    "is_error": call.result["isError"],
                },
            )
        )
    existing = turn.snapshot["steps"]
    assert isinstance(existing, list)
    return turn.model_copy(
        update={"mcp_calls": calls, "snapshot": {**turn.snapshot, "steps": steps + existing}}
    )


def extend(pack: FixturePack, schema_bytes: bytes) -> FixturePack:
    scenarios: list[ScenarioFixture] = []
    auth_path = "packages/mux/mux/drivers/openai/mcp_auth.py"
    auth_bytes = subprocess.run(
        ["git", "show", f"{AUTH_COMMIT}:{auth_path}"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    if (ROOT / auth_path).read_bytes() != auth_bytes:
        raise ValueError("auth source differs from the approved commit")
    schema_pin = pin("fixtures/mcp-tools.json", schema_bytes)
    schemas = OBJECT.validate_json(schema_bytes)
    for scenario in pack.scenarios:
        if scenario.scenario_id not in SCENARIOS:
            scenarios.append(scenario)
            continue
        host = [i for i in scenario.invocations if i["operation"] == "host_turn"]
        providers: list[BackendFixture] = []
        for fixture in scenario.providers:
            turns: list[NativeTurn] = []
            for index, invocation in enumerate(host):
                answer, commands, calls = recipe(scenario, index, invocation)
                for call in calls:
                    if call.schema_origin == "daimon-registry":
                        schema = OBJECT.validate_python(schemas[call.name])
                        validator = cast(
                            SchemaValidator,
                            Draft202012Validator(OBJECT.validate_python(schema["inputSchema"])),
                        )
                        validator.validate(call.arguments)
                base = native_turn(
                    fixture.backend,
                    sid=scenario.scenario_id,
                    invocation=invocation,
                    index=index,
                    previous=turns[-1] if turns else None,
                    answer=answer,
                    commands=commands,
                )
                turns.append(add_calls(base, calls, fixture.backend))
            names: dict[str, list[str]] = {}
            for turn in turns:
                for call in turn.mcp_calls:
                    names.setdefault(call.server, []).append(call.name)
            connections = tuple(
                MCPConnection(
                    name=name,
                    url="https://qa.invalid/mcp"
                    if name == "daimon-mcp"
                    else "https://mcp.deepwiki.com/mcp",
                    credential_ref=f"host:qa-{name}",
                    tool_policy=OBJECT.validate_python(
                        {"allowed_tools": sorted(set(allowed)), "required": True}
                    ),
                )
                for name, allowed in sorted(names.items())
            )
            binding = MCPFixtureBinding(
                connections=connections,
                schemas=schema_pin,
                auth_commit=AUTH_COMMIT,
                auth_source=pin(auth_path, auth_bytes),
                host_source=pin(
                    "packages/testing/daimon/testing/qa_mcp_host.py",
                    (ROOT / "packages/testing/daimon/testing/qa_mcp_host.py").read_bytes(),
                ),
            )
            if fixture.backend == "gemini":
                # Actual Interactions MCPServer headers shape. This fictional
                # wire template is not a driver resolver or anonymous fallback.
                tools: list[JsonValue] = [
                    {
                        "type": "mcp_server",
                        "name": c.name,
                        "url": c.url,
                        "headers": {"Authorization": "Bearer offline-mcp-fixture"},
                    }
                    for c in connections
                ]
                turns = [
                    t.model_copy(update={"request": {**t.request, "tools": tools}}) for t in turns
                ]
            gaps = [
                g
                for g in fixture.gaps
                if g.code
                not in {
                    "NATIVE_TOOL_SCHEMA_UNBOUND",
                    "MCP_AUTH_UNBOUND",
                    "EXTERNAL_TOOL_SCHEMA_UNBOUND",
                    "MCP_SERVER_LIMIT",
                }
                and not (g.code == "RUNNER_HOOK" and g.location == "mcp")
            ]
            gaps.append(
                FixtureGap(
                    code="RUNNER_HOOK",
                    location="mcp",
                    reason=(
                        "Authored outputs do not execute permissions/mutations, approvals/forms, "
                        "skills, routine dispatch or delivery. Bind actual owned host effects; "
                        "no scenario PASS follows from this recipe."
                    ),
                )
            )
            if fixture.backend == "openai" and len(connections) > 1:
                gaps.append(
                    FixtureGap(
                        code="MCP_SERVER_LIMIT",
                        location="mcp",
                        reason=(
                            "Approved OpenAI auth accepts exactly one resolver-bound MCP server. "
                            "This two-server intent must raise single_bound_mcp_server before "
                            "credential resolution, agent/session POST or tool dispatch. "
                            "Native follow-up frames are authored inputs, not execution evidence."
                        ),
                    )
                )
            if fixture.backend == "gemini":
                gaps.append(
                    FixtureGap(
                        code="MCP_AUTH_UNBOUND",
                        location="mcp",
                        reason=(
                            "Gemini refuses credential_ref/tool_policy. Native decoding is "
                            "scripted; authenticated preparation needs the owner's resolver "
                            "seam. Never downgrade to anonymous MCP."
                        ),
                    )
                )
            if "deepwiki" in names:
                gaps.append(
                    FixtureGap(
                        code="EXTERNAL_TOOL_SCHEMA_UNBOUND",
                        location="deepwiki",
                        reason=(
                            "Deepwiki arguments are authored offline, not captured discovery. "
                            "Validate discovery before claiming execution. Mixed public/auth "
                            "production preparation remains a runner binding."
                        ),
                    )
                )
            providers.append(
                fixture.model_copy(
                    update={"turns": tuple(turns), "gaps": tuple(gaps), "mcp_binding": binding}
                )
            )
        scenarios.append(scenario.model_copy(update={"providers": tuple(providers)}))
    return FixturePack.model_validate_json(
        pack.model_copy(update={"scenarios": tuple(scenarios)}).model_dump_json()
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, default=DATA / "index.json")
    parser.add_argument("--output", type=Path, default=DATA)
    args = parser.parse_args()
    pack = load_provider_fixtures(args.index, catalog_root=args.index.parent / "catalog")
    schemas = (json.dumps(asyncio.run(registry()), indent=2, sort_keys=True) + "\n").encode()
    pack = extend(pack, schemas)
    args.output.mkdir(parents=True, exist_ok=True)
    # The default extends the existing pack in place, preserving every frozen source.
    if args.output.resolve() != args.index.parent.resolve():
        import shutil

        shutil.copytree(args.index.parent / "catalog", args.output / "catalog", dirs_exist_ok=True)
    destination = args.output / "catalog/fixtures/mcp-tools.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(schemas)
    pins: list[SourcePin] = []
    (args.output / "native").mkdir(exist_ok=True)
    for scenario in pack.scenarios:
        path = f"native/{scenario.scenario_id}.json"
        body = (
            (scenario.model_dump_json(indent=2) + "\n").encode()
            if scenario.scenario_id in SCENARIOS
            else (args.index.parent / path).read_bytes()
        )
        (args.output / path).write_bytes(body)
        pins.append(pin(path, body))
    (args.output / "index.json").write_text(
        FixtureIndex(target=pack.target, scenarios=tuple(pins)).model_dump_json(indent=2) + "\n"
    )
    print(
        f"{len(SCENARIOS)} MCP recipes; "
        f"{sum(len(p.turns) for s in pack.scenarios for p in s.providers)} native turns; "
        "no scenario verdicts"
    )


if __name__ == "__main__":
    main()
