"""Workflow tools: drafts edit against a revision and an agent key cannot publish."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock

import daimon.adapters.mcp.tools.workflows as tools
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.core.config import AnthropicSettings, DatabaseSettings, DiscordSettings, Settings
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from daimon.core.workflows.edits import AddNode, Connect
from daimon.testing.factories import make_account, make_tenant
from fastmcp.exceptions import ToolError
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..harness import auth_identity

_create = tools._create_workflow_impl  # pyright: ignore[reportPrivateUsage]
_edit = tools._edit_workflow_draft_impl  # pyright: ignore[reportPrivateUsage]
_publish = tools._publish_workflow_impl  # pyright: ignore[reportPrivateUsage]
_validate = tools._validate_workflow_impl  # pyright: ignore[reportPrivateUsage]


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
