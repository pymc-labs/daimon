"""The fake world against Postgres: dispatch order, checked simulator answers, per-item git."""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from daimon.core.evals.format import Case, ExchangeRequest, HttpRequest, WorldCall
from daimon.core.evals.world.dispatch import FakeWorld, issue_world_token
from daimon.core.evals.world.http_app import create_world_app
from daimon.core.evals.world.model import HttpCall, ItemRef, WorldRequest
from daimon.core.evals.world.simulator import SimAnswer, SimContext, build_prompt, parse_answer
from daimon.core.stores import eval_world, evals
from daimon.core.stores.evals import BlobRow
from daimon.testing.eval_world import eval_case, make_running_item
from daimon.testing.factories import make_tenant
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class ScriptedSimulator:
    """Answers with `reply` every time and keeps what it was asked."""

    def __init__(self, reply: JsonValue) -> None:
        self.reply, self.asked = reply, list[SimContext]()

    async def answer(self, context: SimContext) -> SimAnswer:
        self.asked.append(context)
        return SimAnswer(parse_answer(context.request, json.dumps(self.reply)), "scripted")

    async def same(self, service: str, recorded: JsonValue, live: JsonValue) -> bool:
        return False


async def _item(
    db_session: AsyncSession,
    factory: async_sessionmaker[AsyncSession],
    world: FakeWorld,
    case: Case,
) -> ItemRef:
    tenant = await make_tenant(db_session, platform="web")
    item_id = await make_running_item(db_session, tenant.id, case)
    item = await world.item(item_id)
    assert item is not None, "a running item is found"
    return item


def _get(url: str) -> HttpCall:
    return HttpCall("GET", url, {})


async def test_world_when_called_then_answers_recorded_overlay_cached_simulated_then_denied(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    simulator = ScriptedSimulator({"status": 200, "body": {"plan": "team"}})
    world = FakeWorld(db_session_factory, simulator, tmp_path)
    ping = {"http": {"method": "GET", "url": "https://api.example.com/v1/ping"}}
    case = eval_case(
        {"exchanges": [{"id": "x1", "surface": "http", "request": ping,
                        "response": {"http": {"status": 200, "body": {"json": {"ok": True}}}}}],
         "simulator": {"max_calls": 1}},
        limits={"max_world_calls": 6},
    )  # fmt: skip
    item = await _item(db_session, db_session_factory, world, case)
    issues = "https://api.github.com/repos/acme/app/issues"
    created = await world.http(item, HttpCall("POST", issues, {}, b'{"title": "Export fails"}'))
    number = json.loads(created.raw_body)["number"]
    answers = [
        await world.http(item, _get("https://api.example.com/v1/ping")),
        created,
        await world.http(item, _get(f"{issues}/{number}")),
        await world.http(item, _get("https://api.example.com/v1/plan")),
        await world.http(item, _get("https://api.example.com/v1/plan")),
        await world.http(item, _get("https://api.example.com/v1/other")),
        await world.http(item, _get("https://api.example.com/v1/ping")),
    ]
    assert [(a.origin, a.reason) for a in answers] == [
        ("recorded", None), ("overlay", None), ("overlay", None), ("simulated", "written"),
        ("simulated", "cached"), ("denied", "simulator_max_calls"), ("denied", "max_world_calls"),
    ], "each call is answered by the first source that can"  # fmt: skip
    assert json.loads(answers[2].raw_body)["title"] == "Export fails", "a read sees the write"
    assert [e.verb for e in created.effects] == ["github.issue.create"], "the write's effect"
    assert len(simulator.asked) == 1, "the cached answer did not call the simulator again"
    async with db_session_factory() as session:
        logged = await eval_world.list_calls(session, item_id=item.id)
    assert len(logged) == 7, "every call is logged, denied ones too"


async def test_simulated_github_answer_when_invalid_twice_then_denied(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    simulator = ScriptedSimulator({"status": 200, "body": {"number": "five", "title": 3}})
    world = FakeWorld(db_session_factory, simulator, tmp_path)
    item = await _item(db_session, db_session_factory, world, eval_case({}))
    answer = await world.http(item, _get("https://api.github.com/repos/acme/app/issues/5"))
    assert (answer.origin, len(simulator.asked)) == ("denied", 2), "one retry, then denied"
    assert answer.reason and answer.reason.startswith("invalid"), "denied as invalid"
    problem = simulator.asked[1].problem or ""
    assert "number" in problem, f"the retry is told what was wrong: {problem}"


def repo_bundle(tmp_path: Path) -> bytes:
    repo = tmp_path / "source"
    git = ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "README.md").write_text("hello\n")
    subprocess.run([*git, "-C", str(repo), "add", "."], check=True)
    subprocess.run([*git, "-C", str(repo), "commit", "-qm", "first"], check=True)
    subprocess.run(["git", "-C", str(repo), "bundle", "create", "-q", "../repo.bundle", "--all"],
                   check=True)  # fmt: skip
    return (tmp_path / "repo.bundle").read_bytes()


@asynccontextmanager
async def serve_world(world: FakeWorld, host: str = "127.0.0.1") -> AsyncIterator[str]:
    config = uvicorn.Config(
        create_world_app(world), host=host, port=0, log_level="error", ws="none"
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


async def _git(*args: str, cwd: Path | None = None) -> str:
    process = await asyncio.create_subprocess_exec(
        "git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    out, err = await process.communicate()
    assert process.returncode == 0, f"git {args[0]} failed: {err.decode()}"
    return out.decode()


async def test_push_when_made_by_one_item_then_lands_in_its_copy_only(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    content = repo_bundle(tmp_path)
    world = FakeWorld(db_session_factory, None, tmp_path / "world")
    tenant = await make_tenant(db_session, platform="web")
    sha = hashlib.sha256(content).hexdigest()
    blob = BlobRow(sha256=sha, size=len(content), media_type="application/x-git-bundle",
                   scrub="clean", needs_human_check=False)  # fmt: skip
    await evals.put_blob(db_session, tenant_id=tenant.id, blob=blob, content=content)
    repo = {"url": "https://github.com/acme/app", "default_branch": "main",
            "bundle": {"sha256": sha, "size": len(content), "media_type": blob.media_type}}  # fmt: skip
    case = eval_case({"repos": [repo]})
    ids = [await make_running_item(db_session, tenant.id, case) for _ in range(2)]
    tokens = [await issue_world_token(db_session, i) for i in ids]
    async with serve_world(world) as base:

        def url(n: int) -> list[str]:
            header = f"http.extraHeader=Authorization: Bearer {tokens[n]}"
            return ["-c", header, f"{base}/world/{ids[n]}/git/github.com/acme/app.git"]

        first, second = tmp_path / "first", tmp_path / "second"
        await _git(*url(0)[:2], "clone", "-q", url(0)[2], str(first))
        (first / "fix.txt").write_text("fixed\n")
        await _git("add", ".", cwd=first)
        await _git("commit", "-qm", "fix", cwd=first)
        await _git(*url(0)[:2], "push", "-q", url(0)[2], "main", cwd=first)
        await _git(*url(1)[:2], "clone", "-q", url(1)[2], str(second))
    assert (await _git("log", "-1", "--format=%s", cwd=first)).strip() == "fix", "pushed"
    assert (await _git("log", "-1", "--format=%s", cwd=second)).strip() == "first", (
        "the other item's copy is untouched"
    )
    async with db_session_factory() as session:
        logged = await eval_world.list_calls(session, item_id=ids[0])
    verbs = [e.verb for c in logged for e in WorldCall.model_validate(c).effects or ()]
    assert verbs == ["git.repo.clone", "git.repo.push"], f"clone and push are effects: {verbs}"
    assert all(c["surface"] == "git" for c in logged), "logged as git calls"


def test_simulator_sees_recorded_commands_when_answering_a_binary() -> None:
    cli = {"cli": {"argv": ["gh", "pr", "list"]}}
    case = eval_case(
        {"exchanges": [{"id": "c1", "surface": "cli", "service": "gh", "request": cli,
                        "response": {"cli": {"exit_code": 0, "stdout": {"text": "#7 Fix"}}}}]}
    )  # fmt: skip
    request = ExchangeRequest(http=HttpRequest(method="GET", url="https://api.github.com/x"))
    context = SimContext(uuid.uuid4(), WorldRequest("http", "github", request), case, "", {}, ())
    assert "#7 Fix" in build_prompt(context), "the recorded output is what to reproduce"
