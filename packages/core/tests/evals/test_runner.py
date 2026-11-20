"""The eval runner against Postgres, a fake model server and the fake world: one item end to
end, a spending cap that stops a run between items, and the production turn port."""

from __future__ import annotations

import io
import json
import re
import tarfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from anthropic import AsyncAnthropic
from anthropic.types import MessageParam, ToolResultBlockParam
from daimon.core.confirmation import ConfirmationPrompt
from daimon.core.evals.access import Subject
from daimon.core.evals.eval_tenant import eval_tenant
from daimon.core.evals.format import Arm, JsonObject, LiveArm, Run, SuitePin, Usage, to_document
from daimon.core.evals.runner.item import ItemDeps, TurnRequest
from daimon.core.evals.runner.turns import EvalTurns, PodSettings
from daimon.core.evals.runner.worker import run_next
from daimon.core.evals.runs import RunDraft, RunService
from daimon.core.evals.sandbox.runtime import CommandResult, PodHandle, PodSpec
from daimon.core.evals.service import EvalService, SuiteCasePick, SuiteDraft
from daimon.core.evals.world.dispatch import FakeWorld
from daimon.core.output_delivery import DeliverableFile
from daimon.core.stores import evals as store
from daimon.core.turn.state import ContentBlock, TextBlock, ToolUseBlock, TurnState
from daimon.testing.builders import plain_turn_deps
from daimon.testing.eval_world import eval_case
from daimon.testing.factories import make_tenant
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.settings_mock import bare_settings
from mux.testing.drivers.anthropic.ma import (
    MARouter,
    build_fake_anthropic,
    list_response,
    make_fake_memory_store_handler,
    send_events_response,
    sse_response,
)
from mux.testing.drivers.anthropic.ma_models import ma_session, ma_session_agent
from mux.testing.fake_model_server import FakeModelServer
from mux.testing.fake_model_server.script import call_tool, say
from pydantic_core import to_jsonable_python
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .test_sandbox import fixture_ca

ISSUE = {"owner": "example", "repo": "app", "title": "Export fails"}
WORLD = {"exchanges": [{
    "id": "x1", "surface": "mcp", "service": "github", "server": "github", "tool": "issue_write",
    "request": {"args": ISSUE}, "response": {"result": {"number": 42}},
    "match": {"mode": "keys", "keys": ["/owner", "/repo"]},
    "effects": [{"verb": "github.issue.create", "target": "github:example/app"}], "kind": "write",
}]}  # fmt: skip


class ModelTurns:
    """A minimal agent loop on the fake model server: every tool call is confirmed on the
    eval surface, then answered by the fake world."""

    def __init__(self, model: FakeModelServer, world: FakeWorld, cost_usd: float) -> None:
        transport = httpx.ASGITransport(app=model)
        self._client = AsyncAnthropic(
            api_key="test",
            base_url="http://model",
            http_client=httpx.AsyncClient(transport=transport),
        )
        self._world, self._cost = world, cost_usd

    async def turn(self, request: TurnRequest) -> Usage:
        messages: list[MessageParam] = [{"role": "user", "content": request.text}]
        blocks: list[ContentBlock] = []
        while True:
            reply = await self._client.messages.create(
                model="test-model", max_tokens=100, messages=messages,
                tools=[{"name": "issue_write", "input_schema": {"type": "object"}}],
            )  # fmt: skip
            messages.append({"role": "assistant", "content": reply.content})  # pyright: ignore[reportArgumentType]
            blocks += [TextBlock("text", b.text) for b in reply.content if b.type == "text"]
            results: list[ToolResultBlockParam] = []
            for call in (b for b in reply.content if b.type == "tool_use"):
                args: JsonObject = to_jsonable_python(call.input)
                prompt = ConfirmationPrompt(
                    title="Open an issue", requester_platform_user_id="u1",
                    expires_at=datetime.now(UTC) + timedelta(minutes=5),
                )  # fmt: skip
                content = "denied"
                if await request.surface.confirm(prompt) == "approved":
                    item = await self._world.item(request.item_id)
                    assert item is not None
                    answer = await self._world.mcp(item, "github", call.name, args)
                    content = json.dumps(to_document(answer.response))
                blocks.append(ToolUseBlock(kind="tool_use", id=call.id, type="agent.mcp_tool_use",
                                           name=call.name, input=dict(args), mcp_server_name="github"))  # fmt: skip
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": content})
            if not results:
                break
            messages.append({"role": "user", "content": results})
        await request.surface.on_terminal_success(TurnState(content=blocks))
        return Usage(cost_usd=self._cost)

    async def close(self, item_id: uuid.UUID) -> None:
        return None


async def _start(
    db_session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    *,
    repeats: int,
    cap_items: float = 10,
) -> tuple[RunService, Subject, str, float]:
    """A one-case suite with a live arm, and a run of it capped at `cap_items` items' cost:
    service, actor, run id and one item's cost."""
    settings = bare_settings().model_copy(update={"features": {"evals": "on"}})
    tenant = await make_tenant(db_session, platform="web", workspace_id="team-a")
    alex = Subject(tenant_id=tenant.id, platform="web", key="alex", role="admin")
    evals = EvalService(factory, settings)
    case = eval_case(WORLD, expect={
        "effects": {"mode": "subset", "items": WORLD["exchanges"][0]["effects"]},
        "outputs": [{"kind": "reply", "text": {"op": "contains", "value": "42"}}],
    })  # fmt: skip
    await evals.save_suite(alex, SuiteDraft(id="runner", name="Runner"), new=True)
    ref = await evals.put_case(alex, "runner", to_document(case))
    suite = await evals.save_suite(alex, SuiteDraft(
        id="runner", name="Runner", cases=[SuiteCasePick(id=ref.id)],
    ))  # fmt: skip
    arm = await evals.save_arm(alex, Arm(id="arm-a", label="A", kind="live",
                                         live=LiveArm(backend="anthropic", model="test-model")))  # fmt: skip
    runs = RunService(factory, settings, evals)
    draft = RunDraft(suite=SuitePin(id="runner", version=suite.version), arm_ids=[arm.id],
                     repeats=repeats)  # fmt: skip
    per_item = (await runs.estimate(alex, draft)).expected_usd / repeats
    run = await runs.start(alex, draft, cap_usd=per_item * cap_items)
    assert run.status == "approved" and run.approval is not None, "an admin's run starts at once"
    assert run.id.startswith("run-") and arm.id not in run.id, "a local run id names no arm"
    return runs, alex, run.id, per_item


async def test_item_when_run_then_card_approved_world_answers_and_reply_is_scored(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runs, alex, run_id, _ = await _start(db_session, db_session_factory, repeats=1)
    model = FakeModelServer()
    model.script([call_tool("issue_write", ISSUE), say("Opened issue 42.")])
    world = FakeWorld(db_session_factory, None, tmp_path)
    deps = ItemDeps(db_session_factory, ModelTurns(model, world, 0.01), world)
    item = await run_next(deps)
    assert item and not isinstance(item, bool), "the queued item runs"
    assert item.status == "done", item.error
    kinds = [e.kind for e in item.chat or []]
    assert kinds == ["message", "card", "card_update", "message"], kinds
    assert (item.chat or [])[2].actor == "u1", "the requester answered the card"
    assert [(c.origin, c.tool) for c in item.world_calls or []] == [("recorded", "issue_write")]
    assert "issue_write" in item.model_dump_json(include={"transcript"}), (
        "the tool call is recorded"
    )
    run = await runs.status(alex, run_id)
    assert isinstance(run, Run) and run.status == "done", run.status
    assert run.spend and run.spend.total_usd, "spend is settled"
    async with db_session_factory() as session:
        row = await store.run_by_key(session, tenant_id=alex.tenant_id, key=run_id)
        assert row is not None
        scores = await store.scores_of_run(session, run_id=row.id)
    assert scores and all(s["value"] for s in scores), scores
    assert await run_next(deps) is False, "nothing is left to run"


async def test_cap_when_next_item_would_pass_it_then_run_is_partial_after_the_first(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runs, alex, run_id, per_item = await _start(db_session, db_session_factory, repeats=2,
                                                cap_items=1.5)  # fmt: skip
    model = FakeModelServer()
    model.script([say("Opened issue 42.")] * 2)
    world = FakeWorld(db_session_factory, None, tmp_path)
    deps = ItemDeps(db_session_factory, ModelTurns(model, world, per_item * 0.9), world)
    first, second = await run_next(deps), await run_next(deps)
    assert first and not isinstance(first, bool) and first.status == "done"
    assert second is None, "the second item would pass the cap, so it does not run"
    run = await runs.status(alex, run_id)
    assert isinstance(run, Run) and run.status == "partial", run.status
    assert await run_next(deps) is False


class FakePods:
    """A sandbox runtime with no pods: every command works, and the outputs folder holds one
    report until it is removed."""

    def __init__(self) -> None:
        self.started: list[PodSpec] = []
        self.stopped: list[str] = []
        self.outputs = {"report.txt": b"All done."}

    async def start(self, spec: PodSpec) -> PodHandle:
        self.started.append(spec)
        return PodHandle(id="pod-1", runtime="docker")

    async def run(self, pod: PodHandle, command: str, *, timeout_s: float = 300) -> CommandResult:
        out = io.BytesIO()
        if "tar -c" in command:
            with tarfile.open(fileobj=out, mode="w") as tar:
                for name, content in self.outputs.items():
                    info = tarfile.TarInfo(f"./{name}")
                    info.size = len(content)
                    tar.addfile(info, io.BytesIO(content))
        elif command.startswith("rm -f -- "):
            self.outputs.pop(command.rsplit("/", 1)[-1], None)
        return CommandResult(exit_code=0, stdout=out.getvalue(), stderr=b"")

    async def files(self, pod: PodHandle, root: str) -> list[DeliverableFile]:
        return []

    async def stop(self, pod: PodHandle) -> None:
        self.stopped.append(pod.id)

    async def reap(self) -> list[str]:
        return []


def _provider(source_tenant: uuid.UUID, created: list[dict[str, Any]]) -> MARouter:
    """Managed agents with the run workspace's default agent: forks and environments are
    made on request, and each session replies once."""
    pool = {"ag_src": ma_agent(id="ag_src", name="default", tenant_id=source_tenant).model_dump(
        mode="json")}  # fmt: skip
    reply = [
        {"id": "evt_msg", "type": "agent.message", "processed_at": "2026-01-05T09:15:01Z",
         "content": [{"type": "text", "text": "Opened issue 42. The report is attached."}]},
        {"id": "evt_idle", "type": "session.status_idle", "processed_at": "2026-01-05T09:15:02Z",
         "stop_reason": {"type": "end_turn"}},
    ]  # fmt: skip
    memory = make_fake_memory_store_handler()

    def handle(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        kind, rest = match["kind"], match["rest"]
        if request.method == "GET":
            if rest:
                return httpx.Response(200, json=pool[rest.strip("/")])
            return list_response([v for k, v in pool.items() if k.startswith(kind[:2])])
        if rest:
            created.append({"kind": "archive", "id": rest.split("/")[1]})
            return httpx.Response(200, json=pool[rest.split("/")[1]])
        body = json.loads(request.content)
        created.append({"kind": kind, **body})
        fields = {"name": body["name"], "metadata": body["metadata"]}
        made = (ma_agent(id="ag_fork", **fields) if kind == "agents"
                else ma_environment(id="env_fork", **fields))  # fmt: skip
        pool[made.id] = made.model_dump(mode="json")
        return httpx.Response(200, json=pool[made.id])

    def session(request: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json=ma_session(
            id="sess_1", agent=ma_session_agent(id=body["agent"], name="fork"),
            environment_id=body["environment_id"],
        ).model_dump(mode="json"))  # fmt: skip

    router = MARouter()
    router.add("GET", r"/v1/(?P<kind>agents|environments)(?P<rest>/[^/]+)?$", handle)
    router.add("POST", r"/v1/(?P<kind>agents|environments)(?P<rest>/[^/]+/archive)?$", handle)
    router.add("POST", r"/v1/memory_stores", lambda r, _m: memory(r))
    router.add("POST", r"/v1/sessions$", session)
    router.add("POST", r"/v1/sessions/[^/]+/events$", lambda _r, _m: send_events_response())
    router.add("GET", r"/v1/sessions/[^/]+/events/stream$", lambda _r, _m: sse_response(reply))
    router.add("GET", r"/v1/sessions/[^/]+/events$", lambda _r, _m: list_response(reply))
    return router


async def test_production_port_when_item_runs_then_fork_pod_turn_and_files_reach_the_chat(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runs, alex, run_id, _ = await _start(db_session, db_session_factory, repeats=1)
    created: list[dict[str, Any]] = []
    anthropic = build_fake_anthropic(_provider(alex.tenant_id, created).dispatch)
    turn_deps = plain_turn_deps(anthropic, db_session_factory, defaults_root=tmp_path)
    world, pods = FakeWorld(db_session_factory, None, tmp_path), FakePods()
    settings = PodSettings(runtime=pods, image="sandbox@sha256:0", ca=fixture_ca(),
                           world_url="http://world", executor_config={"KEY": "k"})  # fmt: skip
    deps = ItemDeps(db_session_factory, EvalTurns(turn_deps, world, settings), world)
    item = await run_next(deps)
    assert item and not isinstance(item, bool), "the queued item runs"
    assert item.status == "done", item.error
    fork, environment = (
        next(c for c in created if c["kind"] == k) for k in ("agents", "environments")
    )
    eval_id = str(await eval_tenant(db_session_factory))
    assert fork["metadata"]["daimon_tenant"] == eval_id, "the fork lives in the eval tenant"
    assert fork.get("mcp_servers") in (None, []), "the fork dials no MCP servers"
    assert environment["config"] == {"type": "self_hosted"}, "its environment is the pod"
    (spec,) = pods.started
    assert spec.executor_config == {"KEY": "k", "ANTHROPIC_ENVIRONMENT_ID": "env_fork"}
    assert pods.stopped == ["pod-1"], "the pod stops when the item ends"
    assert sum(c["kind"] == "archive" for c in created) == 2, "fork and environment are archived"
    kinds = [e.kind for e in item.chat or []]
    assert kinds == ["message", "reaction_add", "reaction_remove", "message", "file"], kinds
    (output,) = item.outputs or []
    assert (output.name, output.source) == ("report.txt", "sandbox"), output
    assert not pods.outputs, "a delivered file leaves the pod's outputs folder"
    run = await runs.status(alex, run_id)
    assert isinstance(run, Run) and run.status == "done", run.status
