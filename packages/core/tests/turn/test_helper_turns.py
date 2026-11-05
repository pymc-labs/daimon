"""Helper turns end to end: an asking turn on a real host serves a helper's ask.

Only the Anthropic HTTP transport is fake. Admission, session binding, turn
origins, the helper runner and every store run against Postgres. The asking
agent's stream is held open until its ask is finished, as an agent waiting on
`ask_helper` would be; a second Anthropic agent answers the ask.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from daimon.core import helpers
from daimon.core.config import McpSettings
from daimon.core.helpers import helper_thread_key
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.core.stores import agent_helpers, file_uploads, helper_asks
from daimon.core.stores.domain import HelperAskRow, Role, TenantRow, UploadedFile
from daimon.core.stores.mux_state import PostgresStateStore
from daimon.core.stores.thread_sessions import get_live_thread_session
from daimon.core.tool_safety import OPEN_TOOL_SAFETY
from daimon.core.turn.admission import admit
from daimon.core.turn.deps import TurnDeps
from daimon.core.turn.prepare import bind_session
from daimon.core.turn.run import run_prepared_turn
from daimon.core.turn_hold import RequesterOrg
from daimon.core.turn_origin import turn_origin
from daimon.testing.ma import resolved_agent_env_router
from daimon.testing.ma_models import ma_agent, ma_environment
from daimon.testing.ma_sessions import FakeSessionsState, make_fake_sessions_handler
from daimon.testing.turn_fakes import RecordingLifecycle
from daimon.testing.turn_router import build_turn_router
from mux.contracts.ids import ChannelRef, ThreadRef
from mux.state.lease import Slot
from mux.testing.drivers.anthropic.ma import (
    FakeMAState,
    MARouter,
    combine_handlers,
    make_fake_memory_store_handler,
)
from mux.testing.isolated_sdk import build_async_anthropic
from pydantic import HttpUrl
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from daimon.testing.factories import (  # isort: skip
    make_ledger_entry,
    make_tenant,
    make_tenant_config,
)

PARENT_ID = "ag_asker"
HELPER_ID = "ag_helper"
PARENT_TEXT = "The asker is done."
HELPER_TEXT = "Six times seven is 42."
QUESTION = "What is six times seven?"
THREAD = "thread"


@pytest.fixture
def db_session_factory(db_engine: AsyncEngine, db_clean: None) -> async_sessionmaker[AsyncSession]:
    """Sessions on their own connections, as in production: the asking turn and its
    helpers run at once, and one's write is seen by the other only once committed."""
    return async_sessionmaker(db_engine, expire_on_commit=False)


@dataclass
class Scene:
    """What a scenario's fake asker can reach while its stream is open."""

    sessionmaker: async_sessionmaker[AsyncSession]
    tenant: TenantRow
    origin_id: uuid.UUID | None = None
    ask_id: uuid.UUID | None = None
    streamed: bool = False
    session_agents: dict[str, str] = field(default_factory=dict[str, str])
    sent: dict[str, list[str]] = field(default_factory=dict[str, list[str]])

    async def ask(self, message: str = QUESTION, files: list[UploadedFile] | None = None) -> None:
        """Insert an ask for the running turn, as the `ask_helper` tool does."""
        assert self.origin_id is not None, "the stream opens inside the turn's origin"
        async with self.sessionmaker.begin() as db:
            row = await helper_asks.create_ask(
                db,
                tenant_id=self.tenant.id,
                origin_id=self.origin_id,
                helper_agent_id=HELPER_ID,
                helper_name="helper",
                message=message,
                now=datetime.now(UTC),
                files=files or [],
            )
        self.ask_id = row.id

    async def current(self) -> HelperAskRow:
        assert self.origin_id is not None and self.ask_id is not None, "an ask was made"
        async with self.sessionmaker() as db:
            [row] = await helper_asks.list_asks(
                db, tenant_id=self.tenant.id, origin_id=self.origin_id, ids=[self.ask_id]
            )
        return row

    async def wait_finished(self) -> HelperAskRow:
        """Hold the asking turn until its ask leaves queued and running."""
        async with asyncio.timeout(30):
            while (row := await self.current()).status in helper_asks.OPEN:
                await asyncio.sleep(0.02)
        return row


@dataclass
class Run:
    scene: Scene
    lifecycle: RecordingLifecycle
    parent_session: str
    helper_slot: Slot
    store: PostgresStateStore
    final_ask: HelperAskRow


async def run_asking_turn(
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    on_stream: Callable[[Scene], Awaitable[None]],
    *,
    poll_s: float = 0.02,
    public_url: str | None = None,
    on_helper_stream: Callable[[Scene], Awaitable[None]] | None = None,
) -> Run:
    """Run one chat turn of agent `daimon` whose stream calls `on_stream` before it answers."""
    monkeypatch.setattr(helpers, "POLL_S", poll_s)
    async with db_session_factory.begin() as db:
        tenant = await make_tenant(db)
        await make_tenant_config(db, tenant=tenant, agent_name="daimon", environment_name="default")
        await make_ledger_entry(db, tenant=tenant, delta_usd=Decimal(10))
        await agent_helpers.set_helpers(
            db, tenant_id=tenant.id, agent_id=PARENT_ID, helper_ids=[HELPER_ID]
        )

    parent = ma_agent(id=PARENT_ID, name="daimon", tenant_id=tenant.id)
    helper = ma_agent(id=HELPER_ID, name="helper", tenant_id=tenant.id)
    environment = ma_environment(id="env_helpers", name="default", tenant_id=tenant.id)
    ma_state = FakeMAState()
    for agent in (parent, helper):
        body = agent.model_dump(mode="json")
        ma_state.agents[agent.id] = body
        ma_state.agent_versions[agent.id] = {agent.version: body}
    sessions = FakeSessionsState(ma=ma_state)

    resolution = MARouter()
    resolution.add_agent_list(parent, helper)
    resolution.add_agent(helper)
    resolved_agent_env_router(parent, environment, router=resolution)
    turns = {
        agent_id: build_turn_router(
            str(tenant.id),
            agent_id=agent_id,
            env_id=environment.id,
            agent_text=text,
            usage_event_id=None,
            fresh_event_ids=True,
            router=resolution if agent_id == PARENT_ID else MARouter(),
        )
        for agent_id, text in ((PARENT_ID, PARENT_TEXT), (HELPER_ID, HELPER_TEXT))
    }
    scene = Scene(sessionmaker=db_session_factory, tenant=tenant)
    other = combine_handlers(
        make_fake_sessions_handler(sessions), make_fake_memory_store_handler(), resolution.dispatch
    )

    async def transport(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/events" not in path:
            response = other(request)
            if request.method == "POST" and path == "/v1/sessions":
                agent = json.loads(request.content)["agent"]
                created = json.loads(response.content)["id"]
                scene.session_agents[created] = agent if isinstance(agent, str) else agent["id"]
            return response
        session_id = path.split("/")[3]
        owner = scene.session_agents[session_id]
        if request.method == "POST" and path.endswith("/events"):
            scene.sent.setdefault(session_id, []).append(request.content.decode())
        stream = request.method == "GET" and path.endswith("/events/stream")
        # Once: a reconnecting stream must not ask again.
        if stream and owner == PARENT_ID and not scene.streamed:
            scene.streamed = True
            await on_stream(scene)
        if stream and owner == HELPER_ID and on_helper_stream is not None:
            await on_helper_stream(scene)
        return turns[owner].dispatch(request)

    deps = TurnDeps(
        anthropic=build_async_anthropic(
            api_key="offline",
            base_url="https://anthropic.invalid",
            transport=httpx.MockTransport(transport),
        ),
        sessionmaker=db_session_factory,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
        defaults_root=tmp_path,
        mcp=McpSettings(public_url=HttpUrl(public_url) if public_url else None),
        billing_config=None,
        markup=Decimal(1),
        fernet=None,
        github_fallback_pat=None,
        github_app_id=None,
        github_app_private_key=None,
        public_url=None,
        turn_path="mux",
        channel_backends=True,
        tool_safety=OPEN_TOOL_SAFETY,
    )
    admission = await admit(
        deps,
        tenant_id=tenant.id,
        requester_org=RequesterOrg.HOST,
        platform="slack",
        external_user_id="writer",
        channel_id="channel",
        thread_id=THREAD,
        now=datetime.now(UTC),
        origin="chat",
    )
    assert admission.agent.id == PARENT_ID, "the thread is answered by the asking agent"
    prepared = await bind_session(
        deps,
        admission,
        tenant_id=tenant.id,
        platform="slack",
        external_user_id="writer",
        thread_id=THREAD,
        session_account_id=admission.account_id,
        reuse_existing=True,
    )
    lifecycle = RecordingLifecycle()

    async def reseed() -> str:
        return QUESTION

    async with turn_origin(
        db_session_factory,
        tenant_id=tenant.id,
        account_id=admission.account_id,
        platform="slack",
        parent_channel_id="channel",
        thread_id=THREAD,
        responder_ma_agent_id=admission.agent.id,
        responder_name="daimon",
        role=Role.USER,
        sender_foreign=False,
        shared_channel=False,
        platform_channel_id="channel",
    ) as origin:
        scene.origin_id = origin.id
        outcome = await run_prepared_turn(
            deps,
            prepared,
            tenant_id=tenant.id,
            platform="slack",
            thread_id=THREAD,
            external_user_id="writer",
            user_message=QUESTION,
            lifecycle=lifecycle,
            cancel=asyncio.Event(),
            reseed_user_message=reseed,
            recovery_lifecycle=lambda _cancel: RecordingLifecycle(),
            render_interval_s=0.001,
            operation_key="helper-turn-test",
        )
        # The origin, and with it the asks, goes when this block ends.
        final_ask = await scene.current()
    assert outcome.state.error is None, f"the asking turn should finish: {outcome.state.error}"
    assert PARENT_TEXT in str(outcome.state.content), "the asker answers in its own words"
    channel = ChannelRef(tenant_id=str(tenant.id), platform="slack", channel_id="channel")
    return Run(
        scene=scene,
        lifecycle=lifecycle,
        parent_session=prepared.ma_session_id,
        helper_slot=Slot(
            thread=ThreadRef(channel=channel, thread_id=helper_thread_key(THREAD, HELPER_ID)),
            account_id=str(admission.account_id),
        ),
        store=PostgresStateStore(db_session_factory),
        final_ask=final_ask,
    )


async def test_helper_answers_an_ask_in_its_own_conversation_and_draws_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
    db_clean: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The asker waits on its ask; the helper's reply lands in it from the helper's own session."""
    seen_by_asker: list[str] = []

    async def ask_and_wait(scene: Scene) -> None:
        await scene.ask()
        seen_by_asker.append((await scene.wait_finished()).status)

    run = await run_asking_turn(db_session_factory, tmp_path, monkeypatch, ask_and_wait)

    assert seen_by_asker == ["done"], "the asker saw its ask finish while its own turn ran"
    assert run.final_ask.status == "done", "the ask ends done"
    assert run.final_ask.reply == HELPER_TEXT, "its reply is the helper's final message"
    assert run.final_ask.finished_at is not None, "a finished ask is stamped"

    helper_binding = await run.store.get_binding(run.helper_slot)
    assert helper_binding is not None, "the helper's session is bound on the helper thread key"
    helper_session = helper_binding.native_refs["session"]
    assert helper_session != run.parent_session, "the helper never shares the asker's session"
    assert run.scene.session_agents[helper_session] == HELPER_ID, "that session is the helper's"
    assert run.scene.session_agents[run.parent_session] == PARENT_ID

    async with db_session_factory() as db:
        keyed = await get_live_thread_session(
            db,
            tenant_id=run.scene.tenant.id,
            platform="slack",
            thread_id=helper_thread_key(THREAD, HELPER_ID),
            account_id=uuid.UUID(run.helper_slot.account_id or ""),
        )
    assert keyed is not None and keyed.ma_session_id == helper_session, (
        "the thread-session row sits under the helper key"
    )

    [helper_message] = run.scene.sent[helper_session]
    assert QUESTION in helper_message and f"{QUESTION}" in run.scene.sent[run.parent_session][0], (
        "the helper is sent the ask, the asker its own message"
    )
    assert "asks:" in helper_message, "the helper is told who is asking"

    shown = [str(state.content) for state in run.lifecycle.renders]
    shown += [str(state.content) for state in run.lifecycle.terminal_success]
    assert shown and all(HELPER_TEXT not in text for text in shown), (
        "nothing of the helper is drawn on the asker's lifecycle"
    )
    assert len(run.lifecycle.terminal_success) == 1, "only the asker's turn ended on its lifecycle"
    assert not run.lifecycle.terminal_failures, "and nothing failed there"


async def test_ask_left_open_when_the_asking_turn_ends_is_stopped(
    db_session_factory: async_sessionmaker[AsyncSession],
    db_clean: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ask made too late to be served is closed with the turn, not left queued."""

    async def ask_and_leave(scene: Scene) -> None:
        # The desk's one look at start has passed; its next is a minute away.
        await asyncio.sleep(0.3)
        await scene.ask()

    run = await run_asking_turn(
        db_session_factory, tmp_path, monkeypatch, ask_and_leave, poll_s=60.0
    )

    assert run.final_ask.status == "stopped", "an open ask is stopped when the asker ends"
    assert run.final_ask.reply is None, "a stopped, unserved ask has no reply"
    assert await run.store.get_binding(run.helper_slot) is None, "no helper session was started"


async def test_helper_refused_at_admission_fails_its_ask_in_plain_words(
    db_session_factory: async_sessionmaker[AsyncSession],
    db_clean: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A balance spent after the asker was admitted refuses the helper, and the asker hears why."""

    async def ask_after_balance_is_spent(scene: Scene) -> None:
        async with scene.sessionmaker.begin() as db:
            await make_ledger_entry(
                db, tenant=scene.tenant, delta_usd=Decimal(-10), reason="turn_debit"
            )
        await scene.ask()
        await scene.wait_finished()

    run = await run_asking_turn(
        db_session_factory, tmp_path, monkeypatch, ask_after_balance_is_spent
    )

    assert run.final_ask.status == "failed", "a refused helper fails its ask"
    assert run.final_ask.reply == (
        "helper can't run for this person: the person's balance is used up."
    ), "the asker is told why, without internals"
    assert await run.store.get_binding(run.helper_slot) is None, "a refused helper binds nothing"
    assert set(run.scene.sent) == {run.parent_session}, "only the asker's session saw a message"


async def _land(scene: Scene, name: str, data: bytes) -> UploadedFile:
    """A landed upload in the scene's tenant, as a sandbox's curl PUT leaves one."""
    now = datetime.now(UTC)
    async with scene.sessionmaker.begin() as db:
        row, token = await file_uploads.create_upload(
            db,
            tenant_id=scene.tenant.id,
            title=name,
            display_filename=name,
            content_type="application/octet-stream",
            now=now,
        )
        await file_uploads.store_upload_content(db, upload_token=token, data=data, now=now)
    return UploadedFile(id=row.id, name=name, size=len(data))


async def test_files_reach_the_helper_by_link_and_its_files_come_back_with_the_reply(
    db_session_factory: async_sessionmaker[AsyncSession],
    db_clean: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The helper is told how to fetch the asker's file and send its own; what it sends lands."""
    given: list[UploadedFile] = []
    sent: list[UploadedFile] = []

    async def ask_with_a_file(scene: Scene) -> None:
        given.append(await _land(scene, "data.csv", b"a,b"))
        await scene.ask(files=given)
        await scene.wait_finished()

    async def helper_uploads(scene: Scene) -> None:
        # What `PUT /helper-files/{token}/chart.png` does while the helper runs.
        if sent:
            return
        sent.append(await _land(scene, "chart.png", b"PNG"))
        async with scene.sessionmaker.begin() as db:
            await helper_asks.add_reply_file(
                db, ask_id=(await scene.current()).id, file=sent[0], max_files=10, max_bytes=100
            )

    run = await run_asking_turn(
        db_session_factory,
        tmp_path,
        monkeypatch,
        ask_with_a_file,
        public_url="https://daimon.example/mcp",
        on_helper_stream=helper_uploads,
    )

    ask = run.final_ask
    base = f"https://daimon.example/helper-files/{ask.files_token}"
    helper_session = next(s for s, a in run.scene.session_agents.items() if a == HELPER_ID)
    [helper_message] = run.scene.sent[helper_session]
    assert ask.status == "done" and ask.reply == HELPER_TEXT, "the helper still answers in text"
    assert f'curl -sS -o data.csv \\"{base}/{given[0].id}\\"' in helper_message, (
        "the helper is told how to download the asker's file"
    )
    assert f'curl -sS -T <file> \\"{base}/\\"' in helper_message, (
        "the helper is told how to send files back"
    )
    assert ask.reply_files == sent, "the helper's file comes back with its reply"
    shown = [str(state.content) for state in run.lifecycle.renders]
    assert all("chart.png" not in text for text in shown), "the helper's file is posted nowhere"


async def test_without_a_public_url_the_helper_gets_no_file_links(
    db_session_factory: async_sessionmaker[AsyncSession],
    db_clean: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No base URL, no links: the helper reads the plain ask."""

    async def ask_and_wait(scene: Scene) -> None:
        await scene.ask()
        await scene.wait_finished()

    run = await run_asking_turn(db_session_factory, tmp_path, monkeypatch, ask_and_wait)

    helper_session = next(s for s, a in run.scene.session_agents.items() if a == HELPER_ID)
    [helper_message] = run.scene.sent[helper_session]
    assert "helper-files" not in helper_message, "no link is offered without a public URL"
