"""add_skill: preview, confirm, who may, and where an attachment may come from."""

from __future__ import annotations

import dataclasses
import io
import json
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from anthropic.types.beta import SkillListResponse
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import skill_uploads
from daimon.adapters.mcp.tools.skill_uploads import (
    _add_skill_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.skills import _list_impl  # pyright: ignore[reportPrivateUsage]
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import ChannelScopeRef, DeploymentDefault, TenantScopeRef
from daimon.core.slack_file_token import mint_file_token
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.core.stores.domain import Role
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.user_skills import load_user_skill
from daimon.testing import ma_agent
from daimon.testing.crypto import make_fernet
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import (
    FakeMAState,
    NotHandled,
    build_fake_anthropic,
    combine_handlers,
    list_response,
    make_fake_ma_handler,
)
from fastmcp.exceptions import ToolError
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ROOM = "111111111111111111"
USER = "444444444444444444"
_MD = "---\nname: notes\ndescription: Take meeting notes.\n---\nWrite them down.\n"


@dataclass
class _World:
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    runtime: McpRuntime
    state: FakeMAState
    created: list[str] = field(default_factory=list[str])

    def auth(
        self, *, admin: bool = True, platform: str | None = None, external_id: str | None = None
    ) -> AuthIdentity:
        return AuthIdentity(
            account_id=self.account_id,
            tenant_id=self.tenant_id,
            role=Role.ADMIN if admin else Role.USER,
            platform=platform,
            external_id=external_id,
            platform_user_id=USER if platform else None,
            is_admin=admin,
        )


async def _world(factory: async_sessionmaker[AsyncSession], *, managed: bool = False) -> _World:
    async with factory.begin() as session:
        tenant = await make_tenant(session)
        account = await make_account(session, tenant=tenant)
    metadata = {"daimon_account": str(account.id)} | ({"daimon_managed": "true"} if managed else {})
    state = FakeMAState()
    agent = ma_agent(id="agent_helper", name="helper", tenant_id=tenant.id, metadata=metadata)
    state.agents[agent.id] = agent.model_dump(mode="json")
    skills: list[dict[str, Any]] = []
    created: list[str] = []

    def skills_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1/skills":
            raise NotHandled
        if request.method == "GET":
            return list_response(skills)
        found = re.search(rb'name="display_title"\r\n\r\n([^\r]+)', request.content)
        assert found is not None
        created.append(found.group(1).decode())
        skill = SkillListResponse(
            id=f"skill_{len(skills)}",
            type="custom",
            display_title=created[-1],
            latest_version="1",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            source="custom",
        ).model_dump(mode="json")
        skills.append(skill)
        return httpx.Response(200, json=skill)

    settings = MagicMock()
    settings.mcp.public_url = None
    settings.mcp.app_root_url = "https://daimon.example"
    settings.mcp.jwt_secret = SecretStr("proxy-secret")
    runtime = McpRuntime(
        session_factory=factory,
        client=build_fake_anthropic(combine_handlers(skills_handler, make_fake_ma_handler(state))),
        settings=settings,  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(),
        fernet=make_fernet(),
    )
    return _World(tenant.id, account.id, runtime, state, created)


async def test_a_first_call_previews_and_changes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session_factory)

    result = await _add_skill_impl(
        world.runtime, world.auth(), agent_name="helper", expected_ma_agent_id=None, skill_md=_MD
    )

    assert result.status == "preview" and result.added is None
    assert (result.preview.name, result.preview.files) == ("notes", ["SKILL.md"])
    assert result.preview.content_hash in result.summary, "the model is told what to confirm"
    assert world.created == [], "a preview uploads nothing"
    assert world.state.agents["agent_helper"]["skills"] == []


async def test_the_confirmed_hash_uploads_the_agents_own_skill_and_records_who(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session_factory)
    preview = await _add_skill_impl(
        world.runtime, world.auth(), agent_name="helper", expected_ma_agent_id=None, skill_md=_MD
    )

    result = await _add_skill_impl(
        world.runtime,
        world.auth(),
        agent_name="helper",
        expected_ma_agent_id=None,
        skill_md=_MD,
        content_hash=preview.preview.content_hash,
    )

    assert result.status == "added" and result.added is not None
    assert world.created == [
        tenant_scoped_display_title(tenant_id=world.tenant_id, name="notes", agent_name="helper")
    ], "never the shared library"
    attached = world.state.agents["agent_helper"]["skills"]
    assert attached == [{"type": "custom", "skill_id": result.added.skill_id}]
    async with db_session_factory() as session:
        row = await load_user_skill(
            session,
            tenant_id=world.tenant_id,
            principal_id=derive_agent_uuid(tenant_id=world.tenant_id, ma_agent_id="agent_helper"),
            agent_name="helper",
            name="notes",
        )
    assert row is not None
    assert (row.source, row.origin, row.added_by_account_id) == (
        "upload",
        "pasted",
        world.account_id,
    )


async def test_a_skill_that_changed_since_its_preview_is_not_uploaded(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session_factory)
    with pytest.raises(ToolError, match="changed since its preview"):
        await _add_skill_impl(
            world.runtime,
            world.auth(),
            agent_name="helper",
            expected_ma_agent_id=None,
            skill_md=_MD,
            content_hash="0" * 64,
        )
    assert world.created == []


async def test_exactly_one_source_is_taken(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session_factory)
    for sources in ({}, {"skill_md": _MD, "repo_url": "https://github.com/o/r"}):
        with pytest.raises(ToolError, match="exactly one"):
            await _add_skill_impl(
                world.runtime,
                world.auth(),
                agent_name="helper",
                expected_ma_agent_id=None,
                **sources,
            )


async def test_a_built_in_agent_is_refused_even_for_an_admin(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session_factory, managed=True)
    with pytest.raises(ToolError, match="fork_agent"):
        await _add_skill_impl(
            world.runtime,
            world.auth(),
            agent_name="helper",
            expected_ma_agent_id=None,
            skill_md=_MD,
        )


async def test_a_member_may_change_an_agent_that_answers_nowhere_but_not_a_default(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session_factory)
    member = world.auth(admin=False)
    result = await _add_skill_impl(
        world.runtime, member, agent_name="helper", expected_ma_agent_id=None, skill_md=_MD
    )
    assert result.status == "preview"

    async with db_session_factory.begin() as session:
        await set_fields(
            session,
            scope=TenantScopeRef(tenant_id=world.tenant_id),
            tenant_id=world.tenant_id,
            agent_name="helper",
            mode="agent",
        )
    with pytest.raises(ToolError, match="needs a workspace or server admin"):
        await _add_skill_impl(
            world.runtime, member, agent_name="helper", expected_ma_agent_id=None, skill_md=_MD
        )


async def test_a_channel_admin_may_change_an_agent_local_to_their_channel(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session_factory)
    async with db_session_factory.begin() as session:
        await set_fields(
            session,
            scope=ChannelScopeRef(tenant_id=world.tenant_id, channel_id=ROOM),
            tenant_id=world.tenant_id,
            agent_name="helper",
            mode="agent",
        )
        await set_channel_admins(
            session,
            tenant_id=world.tenant_id,
            platform="discord",
            channel_id=ROOM,
            role_ids=[],
            user_ids=[USER],
            actor_account_id=None,
        )

    result = await _add_skill_impl(
        world.runtime,
        world.auth(admin=False, platform="discord"),
        agent_name="helper",
        expected_ma_agent_id="agent_helper",
        skill_md=_MD,
    )
    assert result.status == "preview"


@pytest.mark.parametrize(
    ("platform", "url", "why"),
    [
        ("discord", "https://evil.example/skill.zip", "Discord attachment link"),
        ("discord", "http://cdn.discordapp.com/a/b/skill.zip", "Discord attachment link"),
        (None, "https://cdn.discordapp.com/a/b/skill.zip", "only from Discord or Slack"),
        ("slack", "https://files.slack.com/F1/skill.zip", "link Daimon gave"),
    ],
)
async def test_attachments_come_only_from_the_callers_platform(
    db_session_factory: async_sessionmaker[AsyncSession], platform: str | None, url: str, why: str
) -> None:
    world = await _world(db_session_factory)
    with pytest.raises(ToolError, match=why):
        await _add_skill_impl(
            world.runtime,
            world.auth(platform=platform, external_id="T_MINE"),
            agent_name="helper",
            expected_ma_agent_id="agent_helper",
            attachment_url=url,
        )


async def test_a_slack_file_link_from_another_workspace_is_refused(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session_factory)
    token = mint_file_token(team_id="T_OTHER", file_id="F1", exp=2**40, secret="proxy-secret")
    with pytest.raises(ToolError, match="not from this workspace"):
        await _add_skill_impl(
            world.runtime,
            world.auth(platform="slack", external_id="T_MINE"),
            agent_name="helper",
            expected_ma_agent_id="agent_helper",
            attachment_url=f"https://daimon.example/slack/file/{token}",
        )


async def test_a_discord_attachment_zip_is_previewed(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _world(db_session_factory)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("notes/SKILL.md", _MD)
        archive.writestr("notes/run.sh", "#!/bin/sh\n")
    fetched: list[str] = []

    async def fake_fetch(_http: httpx.AsyncClient, url: str) -> bytes:
        fetched.append(url)
        return buffer.getvalue()

    monkeypatch.setattr(skill_uploads, "fetch_attachment", fake_fetch)
    url = "https://cdn.discordapp.com/attachments/1/2/notes.zip?ex=1"

    result = await _add_skill_impl(
        world.runtime,
        world.auth(platform="discord"),
        agent_name="helper",
        expected_ma_agent_id="agent_helper",
        attachment_url=url,
    )

    assert fetched == [url]
    assert (result.preview.files, result.preview.scripts) == (["SKILL.md", "run.sh"], ["run.sh"])
    assert json.loads(result.model_dump_json())["status"] == "preview"


async def test_an_isolated_channels_agent_keeps_its_added_skill_inside_the_channel(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session_factory)
    async with db_session_factory.begin() as session:
        await set_fields(
            session,
            scope=ChannelScopeRef(tenant_id=world.tenant_id, channel_id=ROOM),
            tenant_id=world.tenant_id,
            agent_name="helper",
            mode="agent",
        )
        await set_access_policy(
            session,
            tenant_id=world.tenant_id,
            policy=TenantAccessPolicy(isolated_channel_ids=(ROOM,)),
        )
    inside = dataclasses.replace(
        world.auth(),
        chat_agent_id=derive_agent_uuid(tenant_id=world.tenant_id, ma_agent_id="agent_helper"),
    )
    with pytest.raises(ToolError, match="not found|no agent"):
        await _add_skill_impl(
            world.runtime,
            world.auth(),
            agent_name="helper",
            expected_ma_agent_id=None,
            skill_md=_MD,
        )
    preview = await _add_skill_impl(
        world.runtime, inside, agent_name="helper", expected_ma_agent_id=None, skill_md=_MD
    )
    await _add_skill_impl(
        world.runtime,
        inside,
        agent_name="helper",
        expected_ma_agent_id=None,
        skill_md=_MD,
        content_hash=preview.preview.content_hash,
    )

    assert [s.name for s in await _list_impl(world.runtime, inside)] == ["helper/notes"]
    assert await _list_impl(world.runtime, world.auth()) == [], "hidden outside the channel"
