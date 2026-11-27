"""Library skills: behind `library_skills`, never in chat turns, by scope and channel line."""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace
from typing import cast

import pytest
from daimon.adapters.mcp import data_access
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.middleware import mcp_identity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import skill_serving, skills
from daimon.core.capability_tools import CAPABILITY_TOOLS, LIBRARY_SKILLS, SERVING_TOOLS
from daimon.core.features.registry import FEATURES
from daimon.core.features.tool_gate import server_gate_key
from daimon.core.stores.domain import Role
from daimon.core.workspace_skills import record_published
from daimon.testing.factories import make_tenant, turn_on
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def test_serving_is_on_by_default_and_never_offered_to_chat_turns() -> None:
    assert FEATURES[LIBRARY_SKILLS].default == "on"
    assert LIBRARY_SKILLS not in CAPABILITY_TOOLS, "chat sessions never get these tools"
    assert {server_gate_key(t) for t in SERVING_TOOLS} == {LIBRARY_SKILLS}
    source = inspect.getsource(mcp_identity.IdentityMiddleware)
    assert "if identity.is_chat_execution:" in source
    assert "disable_components(fastmcp_ctx, names=set(SERVING_TOOLS))" in source


def test_every_skill_reader_hides_across_the_channel_line() -> None:
    for reader in (skills._hidden, skills._list_impl, skill_serving.visible_skills):  # pyright: ignore[reportPrivateUsage]
        assert "skill_hider(" in inspect.getsource(reader), reader.__name__


async def test_a_scoped_skill_shows_only_where_the_audience_holds_its_scope(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    await turn_on(db_session, tenant_id=tenant.id, key=LIBRARY_SKILLS)
    await db_session.commit()
    for skill_id, scope in (("open", None), ("team", "team"), ("other", "other")):
        await record_published(
            db_session_factory,
            tenant_id=tenant.id,
            skill_id=skill_id,
            name=skill_id,
            version="1",
            files={"SKILL.md": b"x"},
            scope=scope,
        )
    held: list[tuple[str, ...]] = [()]

    async def held_scopes(runtime: McpRuntime, auth: AuthIdentity) -> tuple[str, ...]:
        return held[0]

    monkeypatch.setattr(skills, "held_scopes", held_scopes)
    runtime = cast(McpRuntime, SimpleNamespace(session_factory=db_session_factory))
    auth = AuthIdentity(account_id=uuid.uuid4(), tenant_id=tenant.id, role=Role.USER)
    unheld = skills._unheld_scopes  # pyright: ignore[reportPrivateUsage]
    assert await unheld(runtime, auth) == {"team", "other"}, "a call refused data holds none"
    held[0] = ("team",)  # what everyone who can read the room holds
    assert await unheld(runtime, auth) == {"other"}


async def test_no_scope_is_held_where_the_audience_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Teams room with outsiders, or without the consent to list its members, refuses data
    (`data_audience_for` raises), so only unscoped skills show there."""

    async def refused(runtime: McpRuntime, auth: AuthIdentity) -> None:
        raise ToolError("People outside the team can read this conversation.")

    monkeypatch.setattr(data_access, "data_audience_for", refused)
    auth = AuthIdentity(account_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role=Role.USER)
    assert await data_access.held_scopes(cast(McpRuntime, SimpleNamespace()), auth) == ()
