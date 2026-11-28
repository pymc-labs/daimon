"""Workflow tools: drafts edit against a revision and an agent key cannot publish."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import daimon.adapters.mcp.tools.workflows as tools
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    DiscordSettings,
    Settings,
    WebSettings,
)
from daimon.core.posted_controls import PostedCard
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from daimon.core.stores.workflow_runs import get_run
from daimon.core.stores.workflows import get_workflow, merge_state
from daimon.core.web_links import WebLinks
from daimon.core.workflows.chat_cards import ChatPlace
from daimon.core.workflows.edits import AddNode, Connect
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.workflows import make_world
from fastmcp.exceptions import ToolError
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..harness import auth_identity

_create = tools._create_workflow_impl  # pyright: ignore[reportPrivateUsage]
_edit = tools._edit_workflow_draft_impl  # pyright: ignore[reportPrivateUsage]
_publish = tools._publish_workflow_impl  # pyright: ignore[reportPrivateUsage]
_validate = tools._validate_workflow_impl  # pyright: ignore[reportPrivateUsage]
_run = tools._run_workflow_impl  # pyright: ignore[reportPrivateUsage]
_clear_state = tools._clear_workflow_state_impl  # pyright: ignore[reportPrivateUsage]


def _runtime(sessionmaker: async_sessionmaker[AsyncSession]) -> McpRuntime:
    settings = Settings.model_validate(
        {
            "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
            "anthropic": AnthropicSettings(api_key=SecretStr("test")),
            "discord": DiscordSettings(bot_token=SecretStr("test")),
            "features": {"workflows": "on"},
        }
    )
    return McpRuntime(
        session_factory=sessionmaker,
        client=MagicMock(),  # type: ignore[arg-type]
        settings=settings,
        deployment_default=DeploymentDefault(),
    )


async def _person(
    sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession, **fields: Any
) -> tuple[McpRuntime, AuthIdentity]:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session)
    await db_session.commit()  # the tools open their own transactions
    auth = auth_identity(account.id, tenant.id, role=Role.USER, platform="discord", **fields)
    return _runtime(sessionmaker), auth


async def test_edit_workflow_draft_when_edits_add_and_connect_nodes_then_the_revision_moves(
    committing_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    runtime, auth = await _person(committing_sessionmaker, db_session)
    made = await _create(runtime, auth, name="Tidy", template=None, spec=None)
    revision = made["draft"]["revision"]

    saved = await _edit(
        runtime,
        auth,
        workflow_id=uuid.UUID(made["id"]),
        base_revision=revision,
        edit_list=[
            AddNode(op="add_node", id="step", node={"kind": "finish", "title": "Also done"}),
            Connect(op="connect", from_node="step", to="done"),
        ],
        spec=None,
    )

    assert saved["revision"] == revision + 1, "a saved edit is the next revision"
    check = await _validate(runtime, auth, uuid.UUID(made["id"]), None)
    assert isinstance(check["errors"], list), "the draft validates after the edit"


async def test_edit_workflow_draft_when_the_revision_is_stale_names_the_current_one(
    committing_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    runtime, auth = await _person(committing_sessionmaker, db_session)
    made = await _create(runtime, auth, name="Tidy", template=None, spec=None)
    spec: dict[str, Any] = {**made["draft"]["spec"], "goal": "Newer"}
    await _edit(
        runtime,
        auth,
        workflow_id=uuid.UUID(made["id"]),
        base_revision=made["draft"]["revision"],
        edit_list=None,
        spec=spec,
    )

    with pytest.raises(ToolError, match=r"revision 2"):
        await _edit(
            runtime,
            auth,
            workflow_id=uuid.UUID(made["id"]),
            base_revision=made["draft"]["revision"],
            edit_list=[],
            spec=None,
        )


async def test_publish_workflow_when_an_agent_key_asks_is_refused_for_a_person(
    committing_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    runtime, auth = await _person(committing_sessionmaker, db_session, agent_id=uuid.uuid4())
    made = await _create(runtime, auth, name="Tidy", template=None, spec=None)

    with pytest.raises(ToolError, match="needs a person"):
        await _publish(runtime, auth, uuid.UUID(made["id"]), "")


async def test_in_a_chat_turn_publish_and_runs_answer_with_cards(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await make_world(db_session, {"done": {"kind": "finish"}}, "done")
    runtime = McpRuntime(
        session_factory=world.sm,
        client=MagicMock(),  # type: ignore[arg-type]
        settings=world.settings,
        deployment_default=DeploymentDefault(),
    )
    auth = auth_identity(world.owner, world.tenant_id, role=Role.USER, platform="discord")
    origin = SimpleNamespace(platform="discord", thread_id="7", parent_channel_id="6")
    posted: list[PostedCard] = []

    async def chat_origin(*_: object) -> object:
        return origin

    async def post(*_: object, card: PostedCard, **__: object) -> str:
        posted.append(card)
        return "9"

    monkeypatch.setattr(tools, "_chat_origin", chat_origin)
    monkeypatch.setattr(tools, "post_own_card", post)

    waiting = await _publish(runtime, auth, world.workflow_id, "")
    assert waiting["status"] == "waiting" and posted[-1].headline.startswith("📝")
    asked = await _run(runtime, auth, workflow_id=world.workflow_id, inputs={}, test=False)
    assert asked["status"] == "waiting", "a live run from chat waits for the press on its card"
    assert [b.label for b in posted[-1].buttons][0] == "Run"
    ran = await _run(runtime, auth, workflow_id=world.workflow_id, inputs={}, test=True)
    assert "Test run 1" in posted[-1].headline, "a test run shows its run card"
    async with world.sm() as session:
        run = await get_run(session, uuid.UUID(ran["id"]))
    assert run is not None and run.triggered_by["card"] == ChatPlace("discord", "7", "9").ref()


async def test_in_a_chat_turn_a_card_links_the_web_only_where_the_turn_is_aware(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await make_world(db_session, {"done": {"kind": "finish"}}, "done")
    runtime = McpRuntime(
        session_factory=world.sm,
        client=MagicMock(),  # type: ignore[arg-type]
        settings=world.settings,
        deployment_default=DeploymentDefault(),
    )
    auth = auth_identity(world.owner, world.tenant_id, role=Role.USER, platform="discord")
    posted: list[PostedCard] = []
    on = WebLinks.from_settings(WebSettings(public_url="https://app.example.com"))  # type: ignore[call-arg]
    assert on is not None
    aware = [True]

    async def chat_origin(*_: object) -> object:
        return SimpleNamespace(platform="discord", thread_id="7", parent_channel_id="6")

    async def post(*_: object, card: PostedCard, **__: object) -> str:
        posted.append(card)
        return "9"

    async def links(*_: object) -> object:
        return on.for_tenant(world.tenant_id, admin=False) if aware[0] else None

    monkeypatch.setattr(tools, "_chat_origin", chat_origin)
    monkeypatch.setattr(tools, "post_own_card", post)
    monkeypatch.setattr(tools, "tenant_links", links)
    for aware[0], shown in ((True, True), (False, False)):
        await _publish(runtime, auth, world.workflow_id, "")
        await _run(runtime, auth, workflow_id=world.workflow_id, inputs={}, test=False)
        for card in posted[-2:]:
            assert ("Open in web" in [b.label for b in card.buttons]) is shown, (aware, card)


async def test_a_workflow_made_in_chat_records_where_and_an_unreadable_source_stays_unknown(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessionmaker = committing_sessionmaker
    runtime, auth = await _person(sessionmaker, db_session, chat_session_id="s1")
    origin: list[object] = [SimpleNamespace(thread_id="777", parent_channel_id="6")]

    async def chat_origin(*_: object) -> object:
        return origin[0]

    monkeypatch.setattr(tools, "_chat_origin", chat_origin)

    async def made_in(who: AuthIdentity) -> object:
        created = await _create(runtime, who, name="Flow", template=None, spec=None)
        async with sessionmaker() as session:
            row = await get_workflow(
                session, tenant_id=who.tenant_id, workflow_id=uuid.UUID(created["id"])
            )
        assert row is not None
        return row.made_in

    assert await made_in(auth) == {"known": True, "channel_id": "6", "thread_id": "777"}
    origin[0] = None
    assert await made_in(auth) == {"known": False, "channel_id": None, "thread_id": None}, (
        "a chat turn whose origin cannot be read posts nothing by DM"
    )
    outside = replace(auth, chat_session_id=None)
    assert await made_in(outside) == {"known": True, "channel_id": None, "thread_id": None}


async def test_clear_workflow_state_when_the_owner_asks_empties_what_runs_saved(
    committing_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    runtime, auth = await _person(committing_sessionmaker, db_session)
    made = await _create(runtime, auth, name="Tidy", template=None, spec=None)
    workflow_id = uuid.UUID(made["id"])
    async with committing_sessionmaker.begin() as session:
        await merge_state(
            session,
            tenant_id=auth.tenant_id,
            workflow_id=workflow_id,
            patch={"seen": 1},
            now=datetime.now(UTC),
        )

    cleared = await _clear_state(runtime, auth, workflow_id)

    assert cleared["state"] == {}, "what runs saved is gone"


async def test_clear_workflow_state_when_there_is_no_such_workflow_says_so_plainly(
    committing_sessionmaker: async_sessionmaker[AsyncSession], db_session: AsyncSession
) -> None:
    runtime, auth = await _person(committing_sessionmaker, db_session)

    with pytest.raises(ToolError, match="is gone"):
        await _clear_state(runtime, auth, uuid.uuid4())
