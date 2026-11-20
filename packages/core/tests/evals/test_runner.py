"""The eval runner against Postgres, a fake model server and the fake world: one item end to
end, a spending cap that stops a run between items, and the production turn port."""

from __future__ import annotations

import asyncio
import io
import json
import re
import tarfile
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types import MessageParam, ToolResultBlockParam
from daimon.core.config import McpSettings, SelfhostedBillingSettings, SelfhostedModelPrice
from daimon.core.confirmation import ConfirmationPrompt
from daimon.core.credential_requests import mint_request_token
from daimon.core.evals.access import Subject
from daimon.core.evals.chat.cards import EvalCardNotice, press_eval_card
from daimon.core.evals.chat.events import ChatLog
from daimon.core.evals.chat.forms import CredentialPresses
from daimon.core.evals.chat.script import HIDDEN, ScriptPlayer
from daimon.core.evals.chat.surface import EvalSurfaceLifecycle
from daimon.core.evals.eval_tenant import eval_tenant
from daimon.core.evals.format import Arm, JsonObject, LiveArm, Run, SuitePin, Usage, to_document
from daimon.core.evals.runner.gemini import GeminiArms
from daimon.core.evals.runner.harness import HarnessArms
from daimon.core.evals.runner.item import ItemDeps, TurnRequest
from daimon.core.evals.runner.openai import OpenAIArms
from daimon.core.evals.runner.tools import McpService
from daimon.core.evals.runner.turns import EvalTurns, PodSettings
from daimon.core.evals.runner.worker import run_next
from daimon.core.evals.runs import RunDraft, RunService
from daimon.core.evals.sandbox.runtime import CommandResult, PodHandle, PodSpec
from daimon.core.evals.service import EvalService, SuiteCasePick, SuiteDraft
from daimon.core.evals.world.dispatch import FakeWorld
from daimon.core.host_calls import WAKE_SEED, requested_call_card
from daimon.core.output_delivery import DeliverableFile
from daimon.core.posted_controls.cards import card_for_request
from daimon.core.stores import credential_requests, host_call_cards, identity
from daimon.core.stores import evals as store
from daimon.core.stores.agent_files import list_agent_files
from daimon.core.turn.state import ContentBlock, TextBlock, ToolUseBlock, TurnState
from daimon.testing.builders import plain_turn_deps
from daimon.testing.eval_world import eval_case
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.settings_mock import bare_settings
from mux.drivers.gemini.flash import FLASH_PRIMARY
from mux.profiles.openai import LUNA_MODEL
from mux.testing.drivers.anthropic.ma import (
    MARouter,
    build_fake_anthropic,
    list_response,
    make_fake_memory_store_handler,
    send_events_response,
    sse_response,
)
from mux.testing.drivers.anthropic.ma_models import ma_session, ma_session_agent
from mux.testing.drivers.gemini.fake import FakeTransport as GeminiFake
from mux.testing.fake_model_server import FakeModelServer
from mux.testing.fake_model_server.script import call_tool, say
from pydantic_core import to_jsonable_python
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from .test_sandbox import fixture_ca

ISSUE = {"owner": "example", "repo": "app", "title": "Export fails"}
WORLD = {"exchanges": [{
    "id": "x1", "surface": "mcp", "service": "github", "server": "github", "tool": "issue_write",
    "request": {"args": ISSUE}, "response": {"result": {"number": 42}},
    "match": {"mode": "keys", "keys": ["/owner", "/repo"]},
    "effects": [{"verb": "github.issue.create", "target": "github:example/app"}], "kind": "write",
}], "mcp_servers": [{"name": "github", "kind": "third_party", "tools": [
    {"name": "issue_write", "input_schema": {"type": "object"}}]}]}  # fmt: skip


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
    live: LiveArm | None = None,
    commit: bool = False,
) -> tuple[RunService, Subject, str, float]:
    """A one-case suite with a live arm, and a run of it capped at `cap_items` items' cost:
    service, actor, run id and one item's cost."""
    settings = bare_settings().model_copy(update={"features": {"evals": "on"}})
    tenant = await make_tenant(db_session, platform="web", workspace_id="team-a")
    if commit:
        await db_session.commit()
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
    live = live or LiveArm(backend="anthropic", model="test-model")
    arm = await evals.save_arm(alex, Arm(id="arm-a", label="A", kind="live", live=live))
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

    def __init__(self, address: str | None = None) -> None:
        self.address = address
        self.started: list[PodSpec] = []
        self.stopped: list[str] = []
        self.outputs = {"report.txt": b"All done."}

    async def start(self, spec: PodSpec) -> PodHandle:
        self.started.append(spec)
        return PodHandle(id="pod-1", runtime="docker", address=self.address)

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
    """Managed agents with the run workspace's default agent, which uses the case's github
    server, and the worker's untagged eval environment: forks are made on request, and the
    session calls the fork's issue_write once, then replies."""
    pool = {
        "ag_src": ma_agent(id="ag_src", name="default", tenant_id=source_tenant, tools=[
            {"type": "mcp_toolset", "mcp_server_name": "github", "configs": [],
             "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}}},
        ]).model_dump(mode="json"),
        "env_eval": ma_environment(id="env_eval", name="console").model_dump(mode="json")
        | {"metadata": {}},
    }  # fmt: skip
    reply = [
        {"id": "evt_tool", "type": "agent.custom_tool_use", "processed_at": "2026-01-05T09:15:01Z",
         "name": "issue_write", "input": ISSUE},
        {"id": "evt_wait", "type": "session.status_idle", "processed_at": "2026-01-05T09:15:01Z",
         "stop_reason": {"type": "requires_action", "event_ids": ["evt_tool"]}},
        {"id": "evt_msg", "type": "agent.message", "processed_at": "2026-01-05T09:15:02Z",
         "content": [{"type": "text", "text": "Opened issue 42. The report is attached."}]},
        {"id": "evt_idle", "type": "session.status_idle", "processed_at": "2026-01-05T09:15:03Z",
         "stop_reason": {"type": "end_turn"}},
    ]  # fmt: skip
    memory = make_fake_memory_store_handler()

    def handle(request: httpx.Request, match: re.Match[str]) -> httpx.Response:
        kind, rest = match["kind"], match["rest"]
        if request.method == "GET":
            if rest:
                return httpx.Response(200, json=pool[rest.strip("/")])
            return list_response([v for k, v in pool.items() if k.startswith(kind[:2])])
        if rest and rest.endswith("/archive"):
            created.append({"kind": "archive", "id": rest.split("/")[1]})
            return httpx.Response(200, json=pool[rest.split("/")[1]])
        body = json.loads(request.content)
        if rest:
            created.append({"kind": "update", "id": rest.strip("/"), **body})
            pool[rest.strip("/")] |= body
            return httpx.Response(200, json=pool[rest.strip("/")])
        created.append({"kind": kind, **body})
        made = ma_agent(id="ag_fork", name=body["name"], metadata=body["metadata"])
        pool[made.id] = made.model_dump(mode="json")
        return httpx.Response(200, json=pool[made.id])

    def session(request: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(200, json=ma_session(
            id="sess_1", agent=ma_session_agent(id=body["agent"], name="fork"),
            environment_id=body["environment_id"],
        ).model_dump(mode="json"))  # fmt: skip

    def sent(request: httpx.Request, _m: re.Match[str]) -> httpx.Response:
        created.extend({"kind": "event", **e} for e in json.loads(request.content)["events"])
        return send_events_response()

    router = MARouter()
    router.add("GET", r"/v1/(?P<kind>agents|environments)(?P<rest>/[^/]+)?$", handle)
    router.add("POST", r"/v1/(?P<kind>agents|environments)(?P<rest>/[^/]+(/archive)?)?$", handle)
    router.add("POST", r"/v1/memory_stores", lambda r, _m: memory(r))
    router.add("POST", r"/v1/sessions$", session)
    router.add("POST", r"/v1/sessions/[^/]+/events$", sent)
    router.add("GET", r"/v1/sessions/[^/]+/events/stream$", lambda _r, _m: sse_response(reply))
    router.add("GET", r"/v1/sessions/[^/]+/events$", lambda _r, _m: list_response(reply))
    return router


def _mcp_service(world: FakeWorld) -> McpService:
    """The deployment's MCP service as the eval route answers: the world, to the world token."""

    async def handle(request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content)
        if "id" not in message:
            return httpx.Response(202)
        result: JsonObject = {}
        if message["method"] == "tools/call":
            token = request.headers["authorization"].removeprefix("Bearer ")
            item = await world.item_for_token(token)
            assert item is not None and request.url.path == f"/eval-mcp/{item.id}/github"
            params = message["params"]
            answer = await world.mcp(item, "github", params["name"], params["arguments"])
            result = {"content": [{"type": "text", "text": json.dumps(
                to_document(answer.response))}]}  # fmt: skip
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})

    async def resolve(_host: str, _port: int) -> str:
        return "203.0.113.7"

    return McpService("https://mcp.example.com/mcp", b"secret", transport=httpx.MockTransport(
        handle), resolve=resolve)  # fmt: skip


async def test_production_port_when_item_runs_then_fork_pod_turn_and_files_reach_the_chat(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    runs, alex, run_id, _ = await _start(db_session, db_session_factory, repeats=1)
    created: list[dict[str, Any]] = []
    anthropic = build_fake_anthropic(_provider(alex.tenant_id, created).dispatch)
    turn_deps = plain_turn_deps(anthropic, db_session_factory, defaults_root=tmp_path)
    world, pods = FakeWorld(db_session_factory, None, tmp_path), FakePods()
    settings = PodSettings(runtime=pods, image="sandbox@sha256:0", ca=fixture_ca(),
                           world_url="http://world", environment_id="env_eval",
                           executor_config={"KEY": "k"})  # fmt: skip
    turns = EvalTurns(turn_deps, world, settings, _mcp_service(world))
    deps = ItemDeps(db_session_factory, turns, world)
    item = await run_next(deps)
    assert item and not isinstance(item, bool), "the queued item runs"
    assert item.status == "done", item.error
    eval_id = str(await eval_tenant(db_session_factory))
    fork, tag = (next(c for c in created if c["kind"] == k) for k in ("agents", "update"))
    assert fork["metadata"]["daimon_tenant"] == eval_id, "the fork lives in the eval tenant"
    assert fork.get("mcp_servers") in (None, []), "the fork dials no MCP servers"
    assert [(t["type"], t["name"]) for t in fork["tools"]] == [("custom", "issue_write")], (
        "the github toolset is a custom tool the worker runs"
    )
    assert (tag["id"], tag["metadata"]["daimon_tenant"]) == ("env_eval", eval_id), (
        "the worker's environment is named in the eval tenant, not made per item"
    )
    assert not any(c["kind"] == "environments" for c in created)
    (spec,) = pods.started
    assert spec.executor_config == {"KEY": "k", "ANTHROPIC_ENVIRONMENT_ID": "env_eval"}
    assert pods.stopped == ["pod-1"], "the pod stops when the item ends"
    assert [c["id"] for c in created if c["kind"] == "archive"] == ["ag_fork"]
    (result,) = (c for c in created if c.get("type") == "user.custom_tool_result")
    assert result["custom_tool_use_id"] == "evt_tool" and "42" in json.dumps(result["content"])
    assert [(c.origin, c.tool) for c in item.world_calls or []] == [("recorded", "issue_write")]
    kinds = [e.kind for e in item.chat or []]
    assert kinds == ["message", "reaction_add", "reaction_remove", "message", "file"], kinds
    (output,) = item.outputs or []
    assert (output.name, output.source) == ("report.txt", "sandbox"), output
    assert not pods.outputs, "a delivered file leaves the pod's outputs folder"
    run = await runs.status(alex, run_id)
    assert isinstance(run, Run) and run.status == "done", run.status


async def test_card_when_script_approves_then_call_is_approved_and_its_wake_queued(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session, platform="web", workspace_id="team-c")
    alex = await identity.get_or_create_platform_principal(db_session, tenant_id=tenant.id,
                                                           platform="web", external_id="u1")  # fmt: skip
    created = await host_call_cards.create_card(
        db_session, tenant_id=tenant.id, origin_id=uuid.uuid4(), requester_account_id=alex.account_id,
        requester_platform_user_id="u1", platform="web", channel_id="eval-1", thread_id="eval-1",
        executing_agent_id=uuid.uuid4(), executing_ma_agent_id="ag_fork", agent_name="eval-1",
        tool_name="issue_write", args_digest="d", reference={"args": ISSUE},
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )  # fmt: skip
    assert created is not None
    card_id = f"hcc:{created.row.id}"
    await host_call_cards.mark_posted(db_session, card_id=created.row.id, message_id=card_id)
    await db_session.flush()
    mcp = McpSettings.model_validate(
        {"public_url": "https://mcp.example.com/mcp", "jwt_secret": "s" * 32}
    )
    deps = plain_turn_deps(None, db_session_factory, settings=bare_settings(), mcp=mcp)
    chat = ChatLog()
    surface = EvalSurfaceLifecycle(chat, ScriptPlayer(eval_case({}), chat))
    notice = EvalCardNotice(item_id=uuid.uuid4(), card_id=card_id,
                            card=requested_call_card(created.row))  # fmt: skip
    await press_eval_card(deps, tenant.id, surface, notice, is_admin=False)
    assert [e.kind for e in chat.events] == ["card", "card_update"], "the script pressed it"
    async with db_session_factory() as session:
        row = await host_call_cards.get_card(session, tenant_id=tenant.id, card_id=created.row.id)
    assert row is not None and row.status == "approved"
    assert len(surface.wakes) == 1, "the approved call waits for the next turn"


async def test_credential_card_when_it_comes_as_a_notice_then_the_script_saves_the_key(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    agent_id = uuid.uuid4()
    row = await credential_requests.create_credential_request(
        db_session, token=mint_request_token(), kind="env", tenant_id=tenant.id,
        agent_id=agent_id, account_id=account.id, target="NOTES_TOKEN", mcp_server_url=None,
        requester_platform_user_id="u1", channel_id="c", idempotency_key=uuid.uuid4(),
        expires_at=datetime.now(UTC) + timedelta(minutes=30), target_ma_agent_id="ag",
        target_name="acme", requested_work=None,
    )  # fmt: skip
    await db_session.commit()
    chat = ChatLog()
    case = eval_case({}, script={"defaults": {"credential_request": "submit_fake"}})
    script = ScriptPlayer(case, chat, presses=CredentialPresses(db_session_factory))
    notice = EvalCardNotice(item_id=uuid.uuid4(), card_id=f"cred:{row.token}",
                            card=card_for_request(row, state="requested"))  # fmt: skip
    deps = plain_turn_deps(None, db_session_factory, settings=bare_settings())
    await press_eval_card(deps, tenant.id, EvalSurfaceLifecycle(chat, script), notice,
                          is_admin=False)  # fmt: skip
    async with db_session_factory() as session:
        files = await list_agent_files(session, tenant_id=tenant.id, agent_id=agent_id)
    assert [f.key for f in files] == ["NOTES_TOKEN"], "the fake key went through the real save"
    shown = chat.events[-1].card
    assert shown is not None and shown.answer is not None
    assert shown.answer.fields == {"value": HIDDEN}, "the chat never shows the secret"


class WakingTurns:
    """A turn port whose first turn leaves one approved call's wake."""

    def __init__(self) -> None:
        self.turns: list[tuple[str, bool]] = []

    async def turn(self, request: TurnRequest) -> Usage:
        self.turns.append((request.text, request.follow_up))
        if len(self.turns) == 1:

            async def ran() -> str:
                return "Ran issue_write."

            request.surface.wakes.append(ran)
        await request.surface.on_terminal_success(TurnState(content=[TextBlock("text", "Ok.")]))
        return Usage(cost_usd=0.01)

    async def close(self, item_id: uuid.UUID) -> None:
        return None


async def test_wake_when_a_call_was_approved_then_it_runs_as_the_next_turn(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    await _start(db_session, db_session_factory, repeats=1)
    turns, world = WakingTurns(), FakeWorld(db_session_factory, None, tmp_path)
    item = await run_next(ItemDeps(db_session_factory, turns, world))
    assert item and not isinstance(item, bool) and item.status == "done", item
    assert turns.turns[1:] == [(f"Ran issue_write.\n\n{WAKE_SEED}", True)], turns.turns


def _interaction(id_: str, status: str, step: dict[str, Any]) -> JsonObject:
    stamp = datetime.now(UTC).isoformat()
    return {"id": id_, "status": status, "created": stamp, "updated": stamp,
            "environment_id": "environment-1", "steps": [step],
            "usage": {"total_input_tokens": 10, "total_output_tokens": 5}}  # fmt: skip


async def test_production_port_when_gemini_item_runs_then_its_environment_pod_and_tools_answer(
    db_committing_engine: AsyncEngine, db_clean: None, tmp_path: Path
) -> None:
    # Committing sessions: Gemini's records read in read-only transactions of their own.
    db_session_factory = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    async with db_session_factory() as db_session:
        runs, alex, run_id, _ = await _start(
            db_session, db_session_factory, repeats=1, commit=True,
            live=LiveArm(backend="gemini", model=FLASH_PRIMARY),
        )  # fmt: skip
    created: list[dict[str, Any]] = []
    anthropic = build_fake_anthropic(_provider(alex.tenant_id, created).dispatch)
    turn_deps = plain_turn_deps(anthropic, db_session_factory, defaults_root=tmp_path)
    world, pods, gemini = FakeWorld(db_session_factory, None, tmp_path), FakePods(), GeminiFake()
    gemini.responses += [
        _interaction("i1", "requires_action", {"type": "function_call", "id": "call",
                                               "name": "issue_write", "arguments": ISSUE}),
        _interaction("i2", "completed", {"type": "model_output", "content": [
            {"type": "text", "text": "Opened issue 42."}]}),
    ]  # fmt: skip
    settings = PodSettings(runtime=pods, image="sandbox@sha256:0", ca=fixture_ca(),
                           world_url="http://world", environment_id="env_eval")  # fmt: skip
    turns = EvalTurns(turn_deps, world, settings, _mcp_service(world),
                      GeminiArms(db_session_factory, gemini))  # fmt: skip
    item = await run_next(ItemDeps(db_session_factory, turns, world))
    assert item and not isinstance(item, bool), "the queued item runs"
    assert item.status == "done", item.error
    assert not any(c["kind"] in ("agents", "environments") for c in created), (
        "a Gemini item makes nothing on Managed Agents"
    )
    (spec,) = pods.started
    assert (spec.executor, spec.executor_config) == ("gemini", {
        "GEMINI_ENVIRONMENT_ID": "environment-1", "GEMINI_ENVIRONMENT_KEY": "key-environment-1",
    }), "the pod polls the item's own environment with its key"  # fmt: skip
    first, second = gemini.requests
    assert first["environment"] == "environment-1", "the session runs in that environment"
    assert [t["name"] for t in cast(list[JsonObject], first["tools"])] == ["issue_write"]
    assert "42" in json.dumps(second["input"]), "the world's answer is the tool's result"
    assert [(c.origin, c.tool) for c in item.world_calls or []] == [("recorded", "issue_write")]
    assert "Opened issue 42." in json.dumps([e.model_dump(mode="json") for e in item.chat or []])
    assert pods.stopped == ["pod-1"] and gemini.deleted_environments == ["environment-1"], (
        "the pod stops and the environment goes when the item ends"
    )
    run = await runs.status(alex, run_id)
    assert isinstance(run, Run) and run.status == "done", run.status


class FakeHarness:
    """The harness in an item's pod, over its wire: the model calls the host tool
    `issue_write`, waits for the host's result, then replies with it."""

    def __init__(self) -> None:
        self.turns: list[JsonObject] = []
        self.actions: list[JsonObject] = []
        self.cancels = 0
        self.events: list[JsonObject] = []

    def _emit(self, turn_id: str, session_id: str, *events: tuple[str, JsonObject]) -> None:
        for kind, data in events:
            self.events.append({"seq": len(self.events) + 1, "session_id": session_id,
                                "turn_id": turn_id, "at": datetime.now(UTC).isoformat(),
                                "type": kind, "data": data})  # fmt: skip

    def _usage(self, n: int) -> tuple[str, JsonObject]:
        return "usage", {"request_id": f"r{n}", "model": "harness-model", "input_tokens": 10,
                         "cached_input_tokens": 0, "output_tokens": 5, "reasoning_tokens": 0}  # fmt: skip

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "10.0.0.5" and request.url.port == 8088, request.url
        assert request.headers["authorization"].startswith("Bearer "), "the item's token"
        session = request.url.params["session_id"]
        if request.method == "GET":
            after = int(request.url.params.get("replay_after_seq",
                                               request.url.params.get("after_seq", "0")))  # fmt: skip
            events = [e for e in self.events if e["seq"] > after]
            return httpx.Response(200, json={"events": events, "complete": True})
        body = json.loads(request.content) if request.content else {}
        path = request.url.path
        if path == "/v0/turns":
            self.turns.append(body)
            call = {"call_id": "call-1", "name": "issue_write", "input": ISSUE}
            self._emit(body["turn_id"], session, ("status_running", {}), self._usage(1),
                       ("tool_use", {**call, "executor": "host"}),
                       ("requires_action", {**call, "action_id": "act-1",
                                            "kind": "function_result"}))  # fmt: skip
            return httpx.Response(202, json={"accepted": True, "turn_id": body["turn_id"]})
        turn_id = path.split("/")[3]
        if path.endswith("/cancel"):
            self.cancels += 1
            return httpx.Response(200, json={"cancelled": False})
        self.actions.append(body)
        text = "".join(p["text"] for p in body["result"]["content"])
        self._emit(turn_id, session,
                   ("tool_result", {"call_id": "call-1", "output": text, "is_error": False}),
                   self._usage(2),
                   ("agent_message", {"message_id": "m1", "parts": [
                       {"type": "text", "text": "Opened issue 42."}]}),
                   ("turn_ended", {"outcome": "completed"}))  # fmt: skip
        return httpx.Response(200, json={"accepted": True})


async def test_production_port_when_harness_item_runs_then_its_pod_harness_and_tools_answer(
    db_committing_engine: AsyncEngine, db_clean: None, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    monkeypatch.setenv("DAIMON_SELFHOSTED__ADMISSION_ENABLED", "true")
    prices = SelfhostedBillingSettings(models={"harness-model": [SelfhostedModelPrice(
        effective_from=date(2026, 1, 1), input="2", output="10", cache_read="1")]})  # fmt: skip
    monkeypatch.setenv("DAIMON_BILLING__SELFHOSTED", prices.model_dump_json())
    db_session_factory = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    async with db_session_factory() as db_session:
        runs, alex, run_id, _ = await _start(
            db_session, db_session_factory, repeats=1, commit=True,
            live=LiveArm(backend="harness", model="harness-model"),
        )  # fmt: skip
    created: list[dict[str, Any]] = []
    anthropic = build_fake_anthropic(_provider(alex.tenant_id, created).dispatch)
    turn_deps = plain_turn_deps(anthropic, db_session_factory, defaults_root=tmp_path)
    world, pods, harness = FakeWorld(db_session_factory, None, tmp_path), FakePods("10.0.0.5"), \
        FakeHarness()  # fmt: skip
    settings = PodSettings(runtime=pods, image="sandbox@sha256:0", ca=fixture_ca(),
                           world_url="http://world", environment_id="env_eval",
                           harness_config={"MODEL_ENDPOINT": "https://model.example.com"})  # fmt: skip
    arms = HarnessArms(lambda: httpx.AsyncClient(transport=httpx.MockTransport(harness.handle)))
    turns = EvalTurns(turn_deps, world, settings, _mcp_service(world), harness=arms)
    item = await run_next(ItemDeps(db_session_factory, turns, world))
    assert item and not isinstance(item, bool), "the queued item runs"
    assert item.status == "done", item.error
    assert not any(c["kind"] in ("agents", "environments") for c in created), (
        "a harness item makes nothing on Managed Agents"
    )
    (spec,) = pods.started
    token = spec.executor_config["HARNESS_TOKEN"]
    assert (spec.executor, spec.executor_config) == ("selfhosted", {
        "MODEL_ENDPOINT": "https://model.example.com", "HARNESS_TOKEN": token,
    }), "the pod runs the harness with its settings and the item's token"  # fmt: skip
    (turn,) = harness.turns
    assert turn["mcp_servers"] == [], "the pod dials no MCP server"
    assert [t["name"] for t in turn["host_tools"]] == ["issue_write"], "the worker runs it"
    (action,) = harness.actions
    assert action["decision"] == "result" and "42" in json.dumps(action["result"]), (
        "the world's answer is the host tool's result"
    )
    assert [(c.origin, c.tool) for c in item.world_calls or []] == [("recorded", "issue_write")]
    assert "Opened issue 42." in json.dumps([e.model_dump(mode="json") for e in item.chat or []])
    assert pods.stopped == ["pod-1"], "the pod stops when the item ends"
    run = await runs.status(alex, run_id)
    assert isinstance(run, Run) and run.status == "done", run.status


class FakeOpenAI:
    """The OpenAI agents API over its transport: a turn waits for its self-hosted environment,
    calls `issue_write` once the item's pod serves it, then replies with the result."""

    def __init__(self) -> None:
        self.agents: dict[str, JsonObject] = {}
        self.deleted: list[str] = []
        self.session: JsonObject = {}
        self.sent: list[JsonObject] = []
        self.turn: JsonObject = {}
        self.items: list[JsonObject] = []
        self.events: list[JsonObject] = []
        self._pulse = asyncio.Event()

    def _emit(self, kind: str, **fields: object) -> None:
        self.events.append({"type": "agent.session." + kind, "event_id": f"e{len(self.events)}",
                            "session_id": "session-1", **fields})  # fmt: skip
        self._pulse.set()

    def _wait(self, actions: list[JsonObject], status: str = "waiting") -> None:
        self.session.update(status="requires_action", required_actions=actions)
        self.turn["status"] = status
        self._emit("requires_action", session={"id": "session-1", "required_actions": actions})

    def _item(self, item: JsonObject) -> None:
        self.items.append({"turn_id": "turn-1", "status": "completed", **item})
        self._emit("turn.item.done", turn_id="turn-1", item=self.items[-1])

    def connected(self) -> None:
        """The pod's exec-server dialled in: the model calls the host tool."""
        call = {"call_id": "call-1", "name": "issue_write", "arguments": ISSUE}
        self._item({"id": "fc-1", "type": "function_call", **call})
        self._wait([{"type": "function_call", "turn_id": "turn-1", **call}])

    def _page(self, values: list[JsonObject]) -> JsonObject:
        return {"data": values, "has_more": False, "last_id": values[-1]["id"] if values else None}

    async def request(self, method: str, path: str, *, body: JsonObject | None = None,
                      query: Any = None, key: str | None = None) -> JsonObject:  # fmt: skip
        sessions = "/agents/sessions/session-1"
        if (method, path) == ("POST", "/agents"):
            agent = {**(body or {}), "id": f"agent-{len(self.agents) + 1}", "created_at": 0,
                     "multi_agent": {"enabled": False}}  # fmt: skip
            self.agents[agent["id"]] = agent
            return agent
        if path.startswith("/agents/agent-"):
            if method == "DELETE":
                self.deleted.append(path.split("/")[2])
                return {}
            return self.agents[path.split("/")[2]]
        if (method, path) == ("POST", "/agents/sessions"):
            assert body is not None and "input" not in body, "the turn's input is an event"
            agent = self.agents[cast(str, body["agent_id"])]
            self.session = {"id": "session-1", "created_at": 0, "status": "idle",
                            "required_actions": [], "metadata": body["metadata"],
                            "environment": {**cast(JsonObject, body["environment"]),
                                            "id": "env-1"},
                            "agent": {**agent, **cast(JsonObject, body.get("agent") or {})}}  # fmt: skip
            return self.session
        if path == sessions:
            return self.session
        if (method, path) == ("POST", sessions + "/events"):
            assert body is not None
            (event,) = cast(list[JsonObject], body["events"])
            self.sent.append(event)
            if event["type"] == "agent.session.input.message":
                self.turn = {"id": "turn-1", "session_id": "session-1", "agent_id": "agent-1",
                             "subagent_id": None, "status": "in_progress", "created_at": 0,
                             "usage": {"input_tokens": 10, "output_tokens": 5}}  # fmt: skip
                self._emit("turn.in_progress", turn=dict(self.turn))
                self._wait([{"type": "environment_connection", "environment_id": "env-1"}])
            elif event["type"] == "agent.session.input.tool_result":
                self.session.update(status="in_progress", required_actions=[])
                self.turn["status"] = "in_progress"
                self._item({"id": "fco-1", "type": "function_call_output", "call_id": "call-1",
                            "output": json.dumps(event["output"])})  # fmt: skip
                self._item({"id": "m-1", "type": "message", "role": "assistant",
                            "content": [{"type": "output_text", "text": "Opened issue 42."}]})  # fmt: skip
                self.turn["status"] = "completed"
                self.session["status"] = "idle"
                self._emit("turn.completed", turn=dict(self.turn))
            return {}
        if path == sessions + "/turns":
            return self._page([self.turn] if self.turn else [])
        if path == sessions + "/turns/turn-1":
            return self.turn
        if path in (sessions + "/items", sessions + "/turns/turn-1/items"):
            return self._page(self.items)
        raise AssertionError(f"unscripted OpenAI request {method} {path}")

    async def open_stream(self, path: str) -> AsyncIterator[JsonObject]:
        assert path == "/agents/sessions/session-1/events", path

        async def events() -> AsyncIterator[JsonObject]:
            sent = 0
            while True:
                while sent < len(self.events):
                    sent += 1
                    yield self.events[sent - 1]
                self._pulse.clear()
                await self._pulse.wait()

        return events()

    async def multipart(self, *args: Any, **kwargs: Any) -> JsonObject:
        raise AssertionError("an eval arm uploads nothing")

    async def download(self, path: str) -> AsyncIterator[bytes]:
        raise AssertionError("an eval arm downloads nothing")


class ConnectingPods(FakePods):
    """Pods whose exec-server dials the provider as soon as it starts."""

    def __init__(self, provider: FakeOpenAI) -> None:
        super().__init__()
        self.provider = provider

    async def start(self, spec: PodSpec) -> PodHandle:
        pod = await super().start(spec)
        self.provider.connected()
        return pod


async def test_production_port_when_openai_item_runs_then_its_pod_starts_on_connection_and_tools_answer(
    db_committing_engine: AsyncEngine, db_clean: None, tmp_path: Path
) -> None:
    db_session_factory = async_sessionmaker(db_committing_engine, expire_on_commit=False)
    async with db_session_factory() as db_session:
        runs, alex, run_id, _ = await _start(
            db_session, db_session_factory, repeats=1, commit=True,
            live=LiveArm(backend="openai", model=LUNA_MODEL),
        )  # fmt: skip
    created: list[dict[str, Any]] = []
    anthropic = build_fake_anthropic(_provider(alex.tenant_id, created).dispatch)
    turn_deps = plain_turn_deps(anthropic, db_session_factory, defaults_root=tmp_path)
    openai = FakeOpenAI()
    world, pods = FakeWorld(db_session_factory, None, tmp_path), ConnectingPods(openai)
    codex = {"CODEX_REMOTE_URL": "https://remote.example.com", "CODEX_API_KEY": "k"}
    settings = PodSettings(runtime=pods, image="sandbox@sha256:0", ca=fixture_ca(),
                           world_url="http://world", environment_id="env_eval",
                           codex_config=codex)  # fmt: skip
    turns = EvalTurns(turn_deps, world, settings, _mcp_service(world),
                      openai=OpenAIArms(cast(Any, openai)))  # fmt: skip
    item = await run_next(ItemDeps(db_session_factory, turns, world))
    assert item and not isinstance(item, bool), "the queued item runs"
    assert item.status == "done", item.error
    assert not any(c["kind"] in ("agents", "environments") for c in created), (
        "an OpenAI item makes nothing on Managed Agents"
    )
    (agent,) = openai.agents.values()
    assert [(t["type"], t["name"]) for t in cast(list[JsonObject], agent["tools"])] == [
        ("function", "issue_write")], "the fork's MCP tool is a function the worker answers"  # fmt: skip
    assert cast(JsonObject, openai.session["environment"])["type"] == "self_hosted"
    (spec,) = pods.started
    assert (spec.executor, spec.executor_config) == ("openai", {
        **codex, "CODEX_ENVIRONMENT_ID": "env-1",
    }), "the pod starts once the session asks for its environment"  # fmt: skip
    message, result = openai.sent
    assert message["type"] == "agent.session.input.message"
    assert result["type"] == "agent.session.input.tool_result" and "42" in json.dumps(result)
    assert [(c.origin, c.tool) for c in item.world_calls or []] == [("recorded", "issue_write")]
    assert "Opened issue 42." in json.dumps([e.model_dump(mode="json") for e in item.chat or []])
    assert pods.stopped == ["pod-1"] and openai.deleted == ["agent-1"], (
        "the pod stops and the provider agent goes when the item ends"
    )
    run = await runs.status(alex, run_id)
    assert isinstance(run, Run) and run.status == "done", run.status
