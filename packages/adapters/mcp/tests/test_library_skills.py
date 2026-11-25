"""Library skills: behind `library_skills`, never in chat turns, by scope and channel line."""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Callable
from types import SimpleNamespace
from typing import cast

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.middleware import mcp_identity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import skill_serving, skills
from daimon.core.capability_tools import CAPABILITY_TOOLS, LIBRARY_SKILLS, SERVING_TOOLS
from daimon.core.features.registry import FEATURES
from daimon.core.features.tool_gate import server_gate_key
from daimon.core.stores.domain import Role
from daimon.core.workspace_skills import record_published
from daimon.testing.factories import make_tenant
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def test_serving_is_off_by_default_and_never_offered_to_chat_turns() -> None:
    assert FEATURES[LIBRARY_SKILLS].default == "off"
    assert CAPABILITY_TOOLS[LIBRARY_SKILLS] == SERVING_TOOLS
    assert {server_gate_key(t) for t in SERVING_TOOLS} == {LIBRARY_SKILLS}
    source = inspect.getsource(mcp_identity.IdentityMiddleware)
    assert "if identity.is_chat_execution:" in source
    assert "disable_components(fastmcp_ctx, names=set(SERVING_TOOLS))" in source


def test_every_skill_reader_hides_across_the_channel_line() -> None:
    for reader in (skills._hidden, skills._list_impl, skill_serving.visible_skills):  # pyright: ignore[reportPrivateUsage]
        assert "skill_hider(" in inspect.getsource(reader), reader.__name__


async def test_a_caller_refused_data_sees_only_unscoped_skills_on_their_side(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    for skill_id, scope in (("open", None), ("team", "team"), ("across", None)):
        await record_published(
            db_session_factory,
            tenant_id=tenant.id,
            skill_id=skill_id,
            name=skill_id,
            version="1",
            files={"SKILL.md": b"x"},
            scope=scope,
        )

    async def refused(runtime: McpRuntime, auth: AuthIdentity) -> None:
        raise ToolError("refused")

    async def hider(
        runtime: McpRuntime, auth: AuthIdentity, origin_context_id: str | None = None
    ) -> Callable[[str, str], bool]:
        return lambda skill_id, name: skill_id == "across"

    monkeypatch.setattr(skill_serving, "data_audience_for", refused)
    monkeypatch.setattr(skill_serving, "skill_hider", hider)
    runtime = cast(McpRuntime, SimpleNamespace(session_factory=db_session_factory))
    auth = AuthIdentity(account_id=uuid.uuid4(), tenant_id=tenant.id, role=Role.USER)
    assert [r.skill_id for r in await skill_serving.visible_skills(runtime, auth)] == ["open"]
