"""`/api/workflows/*` as the web app calls it: shapes, rights, drafts, runs and the gate."""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from daimon.adapters.web.runtime import WebRuntime
from daimon.adapters.web.turns import WebTurns
from daimon.adapters.web.workflows import workflows_router
from daimon.core.config import Settings
from daimon.core.features.service import set_workspace_feature
from daimon.core.stores import web_roles as roles_store
from daimon.core.stores import workflow_runs
from daimon.core.stores.domain import WebPersonRow
from daimon.testing import ma_agent
from daimon.testing.ma import MARouter
from daimon.testing.turn_router import AGENT_ID, build_turn_router
from daimon.testing.workflows import ma_agents_with
from fastapi.routing import APIRoute
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .chat_support import Workspace, build_turns, funded_workspace, one_session, signed_in
from .conftest import PUBLIC_URL, WebClientFactory, discord_settings, web_settings
from .test_routines_routes import _member, _seed  # pyright: ignore[reportPrivateUsage]

_OWN = {"X-Daimon-Web": "1", "Origin": PUBLIC_URL}
_DEFAULTS = Path(__file__).parents[4] / "defaults"
_BLANK = {"from": "blank", "name": "Tidy up"}
# Every request body the routes take at once, so a route refuses on the gate, not on its shape.
_ANY_BODY: dict[str, object] = {
    **_BLANK,
    "base_revision": 1,
    "spec": {},
    "decision": "fail",
    "note": "",
    "inputs": {},
    "delivery": "x",
    "route": "approve",
}


class Rig:
    """A workspace with an owner and a member, signed in as either, workflows on or off."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        web_client: WebClientFactory,
    ) -> None:
        self.factory, self.tmp_path, self.web_client = factory, tmp_path, web_client
        self.ws: Workspace
        self.member: WebPersonRow
        self.client: httpx.AsyncClient
        self.turns: WebTurns
        self.settings: Settings
        self.sent: list[dict[str, Any]] = []

    async def sign_in(self, who: str) -> None:
        person = self.ws.person if who == "owner" else self.member
        token = await signed_in(self.factory, person)
        self.client.cookies.set("__Host-daimon_web", token, domain="web.example.com")

    @asynccontextmanager
    async def open(
        self,
        *,
        engine: bool = True,
        page: bool = True,
        agents: dict[str, list[str]] | None = None,
        **settings: Any,
    ) -> AsyncIterator[Rig]:
        self.ws = await funded_workspace(self.factory)
        self.member = await _member(self.factory, self.ws.tenant_id, "Mia", discord_id="m-1")
        async with self.factory.begin() as db:
            await roles_store.set_workspace_role(
                db,
                tenant_id=self.ws.tenant_id,
                account_id=self.ws.person.account_id,
                role="owner",
                granted_by_account_id=None,
            )
            await set_workspace_feature(
                db,
                None,
                tenant_id=self.ws.tenant_id,
                platform="web",
                key="web.workflows",
                value="on" if page else "off",
                account_id=self.ws.person.account_id,
                surface="web",
            )
        tenant = str(self.ws.tenant_id)
        listed = MARouter()
        if agents:
            # The turn router's own agent stays listed beside the workflows' named ones.
            listed.add_agent_list(
                ma_agent(id=AGENT_ID, tenant_id=tenant), *ma_agents_with(agents, tenant)
            )
        router = build_turn_router(tenant, sent_event_bodies=self.sent, router=listed)
        self.router = router
        built = build_turns(self.factory, router, self.tmp_path)
        features = {"workflows": "on" if engine else "off", **settings.pop("features", {})}
        config = discord_settings(features=features, defaults_root=_DEFAULTS, **settings)
        self.settings = config
        turns = WebTurns(config, built.web, replace(built.deps, settings=config))
        self.turns = turns
        async with self.web_client(
            web_settings(), sessionmaker=self.factory, turns=turns, settings=config
        ) as client:
            self.client = client
            client.headers.update(_OWN)
            await self.sign_in("owner")
            yield self

    async def make(self, body: dict[str, Any] | None = None) -> dict[str, Any]:
        res = await self.client.post("/api/workflows", json=body or _BLANK)
        assert res.status_code == 201, f"the workflow is created: {res.text}"
        return res.json()


@pytest.fixture
def rig(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> Rig:
    return Rig(db_session_factory, tmp_path, web_client)


@pytest.mark.parametrize(("engine", "page"), [(False, True), (False, False)])
async def test_every_workflows_route_refuses_when_the_feature_is_off(
    rig: Rig, engine: bool, page: bool
) -> None:
    async with rig.open(engine=engine, page=page) as r:
        routes = [
            (method, route.path)
            for route in workflows_router(cast(WebRuntime, None)).routes
            if isinstance(route, APIRoute)
            for method in route.methods
        ]
        assert len(routes) > 30, f"the routes are found: {len(routes)}"
        for method, path in routes:
            url = re.sub(r"\{(workflow_id|run_id|item_id|routine_id)\}", str(uuid.uuid4()), path)
            url = re.sub(r"\{number\}", "1", url)
            url = re.sub(r"\{\w+\}", "x", url)
            res = await r.client.request(method, url, json=_ANY_BODY)
            assert res.status_code == 403, (
                f"{method} {path} refuses while workflows are off: {res.text}"
            )


async def test_a_blank_workflow_is_drafted_published_and_run_to_the_end(rig: Rig) -> None:
    async with rig.open() as r:
        made = await r.make()
        assert (made["status"], made["live_version"], made["mine"]) == ("draft", None, True)
        assert made["owner"]["name"] and made["can_edit"] is True, "the owner may edit"

        draft = (await r.client.get(f"/api/workflows/{made['id']}/draft")).json()
        assert draft["validation"]["errors"] == [], "a blank workflow is valid"

        published = await r.client.post(f"/api/workflows/{made['id']}/draft/publish", json={})
        assert published.status_code == 200, published.text
        assert (published.json()["status"], published.json()["live_version"]) == ("live", 1)

        run = await r.client.post(f"/api/workflows/{made['id']}/runs", json={"inputs": {}})
        assert run.status_code == 201, run.text
        assert run.json()["status"] == "succeeded", "a lone Finish step ends at once"

        detail = (await r.client.get(f"/api/workflows/runs/{run.json()['id']}")).json()
        assert [s["key"] for s in detail["steps"]] == ["done"], "the run page lists its steps"
        assert detail["untrusted_inputs"] is True, "a manual run's inputs are always untrusted"
        assert detail["outcome"] == "nothing_to_do", (
            "a Finish with nothing to deliver is a quiet end"
        )

        events = (await r.client.get(f"/api/workflows/runs/{detail['id']}/events")).json()
        assert [e["type"] for e in events][:2] == ["run.queued", "run.started"], "the ledger"
        verified = (await r.client.get(f"/api/workflows/runs/{detail['id']}/verify")).json()
        assert verified == {"ok": True, "events": len(events)}, "the ledger chain holds"

        by_number = await r.client.get(f"/api/workflows/{made['id']}/runs/{detail['number']}")
        assert by_number.json()["id"] == detail["id"], "a run opens by its number too"
        listed = (await r.client.get("/api/workflows")).json()
        assert [w["ticks"][0]["status"] for w in listed] == ["succeeded"], "the list shows ticks"


async def test_the_saved_state_is_read_and_cleared_through_the_routes(rig: Rig) -> None:
    async with rig.open() as r:
        made = await r.make()
        shown = await r.client.get(f"/api/workflows/{made['id']}/state")
        assert shown.json() == {"state": {}, "updated_at": None}, "nothing saved yet"
        cleared = await r.client.delete(f"/api/workflows/{made['id']}/state")
        assert cleared.status_code == 200 and cleared.json()["state"] == {}, cleared.text


async def test_runs_answer_as_a_board_or_a_calendar(rig: Rig) -> None:
    async with rig.open() as r:
        made = await r.make()
        base = f"/api/workflows/{made['id']}/runs"
        board = (await r.client.get(f"{base}?view=board")).json()
        assert [c["stage"] for c in board["columns"]] == ["In progress", "Done", "Failed"], board
        month = (await r.client.get(f"{base}?view=calendar&month=2026-11")).json()
        assert month == {"view": "calendar", "month": "2026-11", "cards": []}, month
        assert (await r.client.get(f"{base}?view=calendar&month=soon")).status_code == 422
        assert (await r.client.get(f"{base}?view=wall")).status_code == 422, "an unknown view"
        assert (await r.client.get(base)).json() == [], "the list stays the default"


async def test_a_run_file_downloads_for_a_viewer_and_no_other_file_does(rig: Rig) -> None:
    async with rig.open() as r:
        made = await r.make()
        await r.client.post(f"/api/workflows/{made['id']}/draft/publish", json={})
        run = (await r.client.post(f"/api/workflows/{made['id']}/runs", json={})).json()
        ref = {"file_id": "file_x", "filename": "notes.txt", "mime_type": "text/plain"}
        async with r.factory.begin() as db:
            await workflow_runs.save_steps(
                db,
                tenant_id=r.ws.tenant_id,
                run_id=uuid.UUID(run["id"]),
                steps={("done", 1): {"files": [ref | {"size_bytes": 5}]}},
            )
        r.router.add(
            "GET",
            r"/v1/files/file_x/content",
            lambda _req, _m: httpx.Response(200, content=b"hello"),
        )
        detail = (await r.client.get(f"/api/workflows/runs/{run['id']}/steps/done")).json()
        [shown] = detail["files"]
        assert shown["url"] == f"/api/workflows/runs/{run['id']}/files/file_x", shown
        got = await r.client.get(shown["url"])
        assert (got.status_code, got.content) == (200, b"hello"), got.text
        assert got.headers["content-disposition"].startswith("attachment;"), "always a download"
        other = await r.client.get(f"/api/workflows/runs/{run['id']}/files/file_y")
        assert other.status_code == 404, "only files the run holds download"


async def test_each_starter_template_creates_a_draft_that_validates(rig: Rig) -> None:
    async with rig.open(agents={"default": ["gmail/send_message"]}) as r:
        starters = (await r.client.get("/api/workflows/templates")).json()
        assert len(starters) == 6, f"the six starters are listed: {starters}"
        for starter in starters:
            made = await r.make({"from": "template", "template": starter["id"]})
            draft = (await r.client.get(f"/api/workflows/{made['id']}/draft")).json()
            assert draft["validation"]["errors"] == [], (
                f"{starter['id']} starts without errors: {draft['validation']}"
            )


async def test_draft_save_when_the_revision_is_stale_answers_409_with_the_current_draft(
    rig: Rig,
) -> None:
    async with rig.open() as r:
        made = await r.make()
        url = f"/api/workflows/{made['id']}/draft"
        draft = (await r.client.get(url)).json()
        spec = {**draft["spec"], "name": "Renamed"}
        saved = await r.client.put(url, json={"base_revision": draft["revision"], "spec": spec})
        assert saved.status_code == 200, saved.text
        assert saved.json()["revision"] == draft["revision"] + 1, "a save bumps the revision"

        stale = await r.client.put(url, json={"base_revision": draft["revision"], "spec": spec})
        assert stale.status_code == 409, "the old revision is refused"
        assert stale.json()["draft"]["revision"] == draft["revision"] + 1, "with the draft now"


async def test_export_then_import_when_a_workflow_is_copied_then_the_spec_matches(rig: Rig) -> None:
    async with rig.open() as r:
        made = await r.make()
        exported = await r.client.get(f"/api/workflows/{made['id']}/export")
        assert exported.status_code == 200, exported.text
        copy = await r.make({"from": "import", "yaml": exported.text})
        spec = (await r.client.get(f"/api/workflows/{copy['id']}/draft")).json()["spec"]
        original = (await r.client.get(f"/api/workflows/{made['id']}/draft")).json()["spec"]
        assert spec == original, "the import reads back what the export wrote"

        bad = await r.client.post(
            "/api/workflows", json={"from": "import", "yaml": "- just\n- a list"}
        )
        assert bad.status_code == 422, f"something else is refused: {bad.text}"


async def test_a_private_workflow_is_hidden_from_a_member_until_it_is_shared(rig: Rig) -> None:
    async with rig.open() as r:
        made = await r.make()
        await r.sign_in("member")
        assert (await r.client.get("/api/workflows")).json() == [], "private stays private"
        assert (await r.client.get(f"/api/workflows/{made['id']}")).status_code == 404

        await r.sign_in("owner")
        shared = await r.client.patch(f"/api/workflows/{made['id']}", json={"visibility": "shared"})
        assert shared.status_code == 200, shared.text

        await r.sign_in("member")
        seen = await r.client.get(f"/api/workflows/{made['id']}")
        assert (seen.status_code, seen.json()["can_edit"]) == (200, False), "a viewer only reads"
        refused = await r.client.put(
            f"/api/workflows/{made['id']}/draft", json={"base_revision": 1, "spec": {}}
        )
        assert refused.status_code == 403, f"a viewer cannot edit: {refused.text}"


async def test_versions_list_and_restore_when_a_workflow_is_published_twice(rig: Rig) -> None:
    async with rig.open() as r:
        made = await r.make()
        base = f"/api/workflows/{made['id']}"
        await r.client.post(f"{base}/draft/publish", json={})
        draft = (await r.client.get(f"{base}/draft")).json()
        spec = {**draft["spec"], "name": "Second"}
        await r.client.put(f"{base}/draft", json={"base_revision": draft["revision"], "spec": spec})
        second = await r.client.post(f"{base}/draft/publish", json={"note": "two"})
        assert second.status_code == 200, second.text

        versions = (await r.client.get(f"{base}/versions")).json()
        assert [v["number"] for v in versions] == [2, 1], "newest first"
        restored = await r.client.post(f"{base}/versions/1/restore")
        assert restored.status_code == 200, restored.text
        assert restored.json()["spec"]["name"] == "Tidy up", "the draft is version 1 again"


async def test_run_stream_when_a_run_has_ended_replays_its_events_with_ledger_ids(rig: Rig) -> None:
    async with rig.open() as r:
        made = await r.make()
        await r.client.post(f"/api/workflows/{made['id']}/draft/publish", json={})
        run = (await r.client.post(f"/api/workflows/{made['id']}/runs", json={"inputs": {}})).json()
        events = (await r.client.get(f"/api/workflows/runs/{run['id']}/events")).json()

        async with r.client.stream("GET", f"/api/workflows/runs/{run['id']}/stream") as res:
            body = "".join([chunk async for chunk in res.aiter_text()])
        ids = [line[4:] for line in body.splitlines() if line.startswith("id: ")]
        assert ids == [str(e["seq"]) for e in events], f"one frame per ledger event: {body}"


_ASKS = {
    "schema": "daimon.workflow/v1",
    "name": "Ask first",
    "triggers": [{"id": "manual", "kind": "manual"}],
    "start": "review",
    "nodes": {
        "review": {
            "kind": "approval",
            "title": "Check it",
            "approvers": ["owner"],
            "routes": {"approve": "done", "reject": "done"},
        },
        "done": {"kind": "finish", "title": "Done"},
    },
}


async def test_a_test_run_that_has_not_finished_can_be_cancelled_and_forked_from_a_step(
    rig: Rig,
) -> None:
    async with rig.open() as r:
        made = await r.make()
        base = f"/api/workflows/{made['id']}"
        saved = await r.client.put(f"{base}/draft", json={"base_revision": 1, "spec": _ASKS})
        assert saved.status_code == 200, saved.text
        started = await r.client.post(f"{base}/runs", json={"inputs": {}, "test": {}})
        assert started.status_code == 201, started.text
        run = started.json()
        assert run["test"] is not None, f"a draft runs as a test: {run}"
        cancelled = await r.client.post(f"/api/workflows/runs/{run['id']}/cancel")
        assert cancelled.json()["status"] == "cancelled", cancelled.text

        again = await r.client.post(
            f"/api/workflows/runs/{run['id']}/fork", json={"step": "review", "version": "draft"}
        )
        assert again.status_code == 200, again.text
        assert again.json()["id"] != run["id"], "a fork is a new run"


async def test_ask_daimon_chat_when_opened_twice_gives_one_thread_hidden_from_the_chat_list(
    rig: Rig,
) -> None:
    async with rig.open() as r:
        made = await r.make()
        opened = await r.client.get(f"/api/workflows/{made['id']}/chat")
        assert opened.status_code == 200, opened.text
        chat = opened.json()
        assert chat["agent"]["id"] == AGENT_ID, "the person's default agent answers"
        again = (await r.client.get(f"/api/workflows/{made['id']}/chat")).json()
        assert again["session_id"] == chat["session_id"], "one thread per workflow and person"

        listed = (await r.client.get("/api/chat/sessions")).json()
        assert listed == [], "a subject thread is not in the chat list"
        thread = await r.client.get(f"/api/chat/sessions/{chat['session_id']}")
        assert thread.status_code == 200, "but the chat routes still serve it"

        other = await r.make({**_BLANK, "name": "Other"})
        elsewhere = (await r.client.get(f"/api/workflows/{other['id']}/chat")).json()
        assert elsewhere["session_id"] != chat["session_id"], "each workflow has its own"


async def test_ask_daimon_chat_when_the_person_can_only_view_the_workflow_is_refused(
    rig: Rig,
) -> None:
    async with rig.open() as r:
        made = await r.make()
        await r.client.patch(f"/api/workflows/{made['id']}", json={"visibility": "shared"})
        await r.sign_in("member")
        refused = await r.client.get(f"/api/workflows/{made['id']}/chat")
        assert refused.status_code == 403, f"a viewer cannot ask Daimon to edit: {refused.text}"


async def test_ask_daimon_message_when_a_node_is_selected_names_the_workflow_to_the_agent(
    rig: Rig,
) -> None:
    async with rig.open() as r:
        made = await r.make()
        chat = (await r.client.get(f"/api/workflows/{made['id']}/chat")).json()
        with one_session():
            sent = await r.client.post(
                f"/api/chat/sessions/{chat['session_id']}/messages",
                json={
                    "id": str(uuid.uuid4()),
                    "content": "What does this step do?",
                    "context": {"selected_node": "done"},
                },
            )
            await r.turns.drain(10)
        assert sent.status_code == 200, sent.text
        text = str(r.sent)
        assert made["id"] in text, f"the controls carry the workflow: {text[:300]}"
        assert '"selected_node": {"id": "done", "kind": "finish"' in text, "and the node"


async def test_an_admin_moves_a_routine_to_a_workflow_and_back(rig: Rig) -> None:
    async with rig.open() as r:
        routine = await _seed(r.factory, r.ws.tenant_id, owner="m-1", message="Sum up")
        moved = await r.client.post(f"/api/workflows/from-routine/{routine}")
        assert moved.status_code == 201, moved.text
        flow = moved.json()
        assert flow["source_routine"]["id"] == str(routine), "the list links the routine"
        assert flow["migration"]["routine"]["id"] == str(routine), "the page links it too"
        again = await r.client.post(f"/api/workflows/from-routine/{routine}")
        assert again.status_code == 409, "a routine moves once"
        [row] = (await r.client.get("/api/routines")).json()
        assert row["workflow"]["id"] == flow["id"], "the routine links its workflow"
        paused = await r.client.post(f"/api/routines/{routine}/resume")
        assert paused.status_code == 422 and "Workflows" in paused.text, paused.text
        await r.sign_in("member")
        refused = await r.client.post(f"/api/workflows/{flow['id']}/to-routine")
        assert refused.status_code == 403, "only admins move routines"
        await r.sign_in("owner")
        back = await r.client.post(f"/api/workflows/{flow['id']}/to-routine")
        assert back.status_code == 200 and back.json() == {"routine_id": str(routine)}, back.text
