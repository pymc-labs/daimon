"""add_skill: preview, confirm, who may, and where an attachment may come from."""

from __future__ import annotations

import dataclasses
import io
import json
import re
import uuid
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import anthropic
import httpx
import pytest
from aioresponses import aioresponses
from anthropic.types.beta import BetaManagedAgentsAgent, SkillListResponse
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import skill_uploads
from daimon.adapters.mcp.tools.skill_uploads import (
    AddSkillResult,
    _add_skill_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.skills import _list_impl  # pyright: ignore[reportPrivateUsage]
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.github_credentials import encrypt_token
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import ChannelScopeRef, DeploymentDefault, TenantScopeRef
from daimon.core.session_snapshot import SessionSnapshot, desired_snapshot
from daimon.core.skills.ingest import bundle_from_markdown
from daimon.core.slack_file_token import mint_file_token
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.agent_creation_channels import record_creation_channel
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.core.stores.domain import Role
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.core.stores.thread_agent_bindings import create_binding
from daimon.core.stores.thread_sessions import create_thread_session
from daimon.core.stores.turn_origins import create_origin
from daimon.core.stores.user_skills import load_user_skill
from daimon.core.tool_safety import ToolSafetyPolicy
from daimon.core.turn_origin import turn_origin
from daimon.testing import ma_agent, ma_session, ma_session_agent
from daimon.testing.crypto import make_fernet
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import (
    FakeMAState,
    NotHandled,
    build_fake_anthropic,
    combine_handlers,
    list_response,
    make_fake_ma_handler,
    not_found_response,
)
from fastmcp.exceptions import ToolError
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

ROOM = "111111111111111111"
USER = "444444444444444444"
CHAT_THREAD = "555555555555555555"
SETUP_THREAD = "333333333333333333"
_MD = "---\nname: notes\ndescription: Take meeting notes.\n---\nWrite them down.\n"


@dataclass
class _World:
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    runtime: McpRuntime
    state: FakeMAState
    created: list[str] = field(default_factory=list[str])
    sessions: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])

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

    sessions: dict[str, dict[str, Any]] = {}

    def sessions_handler(request: httpx.Request) -> httpx.Response:
        found = re.fullmatch(r"/v1/sessions/(?P<id>[^/]+)", request.url.path)
        if request.method != "GET" or found is None:
            raise NotHandled
        if found["id"] not in sessions:
            return not_found_response(f"no such session: {found['id']}")
        return httpx.Response(200, json=sessions[found["id"]])

    settings = MagicMock()
    settings.tool_safety = ToolSafetyPolicy(enabled=True)
    settings.mcp.public_url = None
    settings.mcp.app_root_url = "https://daimon.example"
    settings.mcp.jwt_secret = SecretStr("proxy-secret")
    runtime = McpRuntime(
        session_factory=factory,
        client=build_fake_anthropic(
            combine_handlers(skills_handler, sessions_handler, make_fake_ma_handler(state))
        ),
        settings=settings,  # type: ignore[arg-type]
        deployment_default=DeploymentDefault(),
        fernet=make_fernet(),
    )
    return _World(tenant.id, account.id, runtime, state, created, sessions)


def _daimon_toolset(*, gated: bool) -> dict[str, Any]:
    """Daimon's toolset as a session froze it: add_skill on always_ask once gated."""
    policy = {"type": "always_ask" if gated else "always_allow"}
    return {
        "type": "mcp_toolset",
        "mcp_server_name": "daimon-mcp",
        "default_config": {"enabled": True, "permission_policy": {"type": "always_allow"}},
        "configs": [{"name": "add_skill", "enabled": True, "permission_policy": policy}],
    }


async def _live_session(
    world: _World,
    *,
    thread_id: str,
    responder: str,
    gated: bool = True,
    recorded: SessionSnapshot | None = None,
) -> None:
    """The chat thread's live session, as MA reports its frozen tools."""
    session_id = f"sesn_{thread_id}"
    async with world.runtime.session_factory.begin() as session:
        await create_thread_session(
            session,
            tenant_id=world.tenant_id,
            platform="discord",
            thread_id=thread_id,
            account_id=world.account_id,
            ma_session_id=session_id,
            ma_agent_id=responder,
            effective_config=recorded,
        )
    frozen = ma_session_agent(id=responder, tools=[_daimon_toolset(gated=gated)])
    world.sessions[session_id] = ma_session(id=session_id, agent=frozen).model_dump(mode="json")


async def _chat_turn(
    world: _World,
    *,
    admin: bool = True,
    gated: bool = True,
    live: bool = True,
    responder: str = "agent_helper",
) -> tuple[AuthIdentity, str]:
    """A Discord chat turn in ROOM: its caller and verified origin, with a live session."""
    now = datetime.now(UTC)
    async with world.runtime.session_factory.begin() as session:
        origin = await create_origin(
            session,
            tenant_id=world.tenant_id,
            account_id=world.account_id,
            platform="discord",
            parent_channel_id=ROOM,
            thread_id=CHAT_THREAD,
            responder_ma_agent_id=responder,
            responder_name=responder.removeprefix("agent_"),
            configuration_target_ma_agent_id=None,
            configuration_target_name=None,
            role=Role.ADMIN if admin else Role.USER,
            expires_at=now + timedelta(minutes=10),
            now=now,
        )
    if live:
        await _live_session(world, thread_id=CHAT_THREAD, responder=responder, gated=gated)
    auth = dataclasses.replace(
        world.auth(admin=admin, platform="discord"),
        chat_agent_id=derive_agent_uuid(tenant_id=world.tenant_id, ma_agent_id=responder),
    )
    return auth, str(origin.id)


async def test_a_first_call_previews_and_changes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session_factory)
    auth, origin = await _chat_turn(world)

    result = await _add_skill_impl(
        world.runtime,
        auth,
        agent_name="helper",
        expected_ma_agent_id="agent_helper",
        skill_md=_MD,
        origin_context_id=origin,
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

    result = await _preview_then_confirm(world, skill_md=_MD)

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
    auth, origin = await _chat_turn(world)
    with pytest.raises(ToolError, match="changed since its preview"):
        await _add_skill_impl(
            world.runtime,
            auth,
            agent_name="helper",
            expected_ma_agent_id="agent_helper",
            skill_md=_MD,
            content_hash="0" * 64,
            origin_context_id=origin,
        )
    assert world.created == []


#: What a preview says for each reason a chat turn can't show the card.
_NO_CARD_REASONS = {
    "no_origin_context": "did not pass this turn's origin_context_id",
    "unverified_origin": "is not this turn's",
    "not_a_chat_turn": "does not come from a chat turn",
    "agent_key": "runs on an agent key",
    "no_live_session": "has no live session",
    "other_agents_session": "runs a different agent",
    "session_not_gated": "does not ask before add_skill yet",
    "session_unreadable": "could not read this thread's session",
}


@pytest.mark.parametrize("where", sorted(_NO_CARD_REASONS))
async def test_a_confirm_adds_only_from_a_chat_session_that_asks_the_person_first(
    db_session_factory: async_sessionmaker[AsyncSession], where: str
) -> None:
    """With tool safety on, a call from outside a verified chat turn, a session created
    before the gate, or an origin whose live session is not its own adds nothing, and
    the preview and the refusal both say which it was."""
    world = await _world(db_session_factory)
    auth, origin = await _chat_turn(
        world, gated=where != "session_not_gated", live=where != "no_live_session"
    )
    if where == "other_agents_session":
        frozen = ma_session_agent(id="agent_other", tools=[_daimon_toolset(gated=True)])
        world.sessions[f"sesn_{CHAT_THREAD}"] = ma_session(
            id=f"sesn_{CHAT_THREAD}", agent=frozen
        ).model_dump(mode="json")
    if where == "session_unreadable":
        del world.sessions[f"sesn_{CHAT_THREAD}"]
    if where == "not_a_chat_turn":
        auth = world.auth()
    if where == "agent_key":
        auth = dataclasses.replace(auth, agent_id=auth.chat_agent_id, chat_agent_id=None)
    context: str | None = {
        "no_origin_context": None,
        "unverified_origin": str(uuid.uuid4()),
    }.get(where, origin)

    async def add(**extra: Any) -> AddSkillResult:
        return await _add_skill_impl(
            world.runtime,
            auth,
            agent_name="helper",
            expected_ma_agent_id="agent_helper",
            skill_md=_MD,
            origin_context_id=context,
            **extra,
        )

    preview = await add()
    summary = preview.summary
    assert "content_hash=" not in summary and "/agent-setup" in summary
    assert "this conversation can't show one" in summary
    assert _NO_CARD_REASONS[where] in summary, "the preview names why no card can show"
    assert "turned off on this deployment" not in summary, "the deployment has cards on"
    with pytest.raises(ToolError, match="this conversation can't show one") as refused:
        await add(content_hash=preview.preview.content_hash)
    assert _NO_CARD_REASONS[where] in str(refused.value)
    assert world.created == [], "nothing was uploaded"


_PUBLIC_URL = "https://daimon.example/mcp"


@pytest.mark.parametrize("public_url", [None, _PUBLIC_URL], ids=["no-public-url", "public-url"])
@pytest.mark.parametrize("recorded_policy", ["gated", "open"])
async def test_a_session_reported_without_its_overrides_confirms_from_the_recorded_tools(
    db_session_factory: async_sessionmaker[AsyncSession],
    recorded_policy: str,
    public_url: str | None,
) -> None:
    """When MA reports the agent's own always_allow tools, the bind's record decides:
    the gated tools it sent confirm, the agent's ungated ones do not. With a public
    URL the agent's `daimon-mcp` is the trusted toolset, gated on add_skill alone,
    as production runs it."""
    world = await _world(db_session_factory)
    world.runtime.settings.mcp.public_url = public_url
    world.state.agents["agent_helper"]["tools"] = [_daimon_toolset(gated=False)]
    if public_url is not None:
        world.state.agents["agent_helper"]["mcp_servers"] = [
            {"type": "url", "name": "daimon-mcp", "url": public_url}
        ]
    agent = BetaManagedAgentsAgent.model_validate(world.state.agents["agent_helper"])
    recorded = desired_snapshot(
        agent,
        hidden_mcp_server_names=frozenset(),
        environment_id="env_1",
        env_sha256=None,
        repo_url=None,
        repo_branch=None,
        memory_store_id=None,
        vault_id=None,
        tool_safety=ToolSafetyPolicy(enabled=recorded_policy == "gated"),
        public_url=public_url,
    )
    auth, origin = await _chat_turn(world, live=False)
    await _live_session(
        world, thread_id=CHAT_THREAD, responder="agent_helper", gated=False, recorded=recorded
    )

    async def add(**extra: Any) -> AddSkillResult:
        return await _add_skill_impl(
            world.runtime,
            auth,
            agent_name="helper",
            expected_ma_agent_id="agent_helper",
            skill_md=_MD,
            origin_context_id=origin,
            **extra,
        )

    preview = await add()
    if recorded_policy == "open":
        with pytest.raises(ToolError, match="this conversation can't show one"):
            await add(content_hash=preview.preview.content_hash)
        assert world.created == [], "a session recorded without the gate never confirms"
        return
    result = await add(content_hash=preview.preview.content_hash)
    assert result.status == "added", "the recorded gated tools are the server's own evidence"


async def test_a_session_reported_with_other_tools_is_taken_at_its_word(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """MA reporting tools that are neither the agent's own nor gated (a session
    switched to always_allow out of band) refuses the confirm, whatever the bind
    recorded sending it."""
    world = await _world(db_session_factory)
    world.state.agents["agent_helper"]["tools"] = [_daimon_toolset(gated=False)]
    agent = BetaManagedAgentsAgent.model_validate(world.state.agents["agent_helper"])
    recorded = desired_snapshot(
        agent,
        hidden_mcp_server_names=frozenset(),
        environment_id="env_1",
        env_sha256=None,
        repo_url=None,
        repo_branch=None,
        memory_store_id=None,
        vault_id=None,
        tool_safety=ToolSafetyPolicy(enabled=True),
    )
    auth, origin = await _chat_turn(world, live=False)
    await _live_session(
        world, thread_id=CHAT_THREAD, responder="agent_helper", gated=False, recorded=recorded
    )
    reported = world.sessions[f"sesn_{CHAT_THREAD}"]["agent"]["tools"][0]
    reported["configs"].append(
        {"name": "send_message", "enabled": True, "permission_policy": {"type": "always_allow"}}
    )

    async def add(**extra: Any) -> AddSkillResult:
        return await _add_skill_impl(
            world.runtime,
            auth,
            agent_name="helper",
            expected_ma_agent_id="agent_helper",
            skill_md=_MD,
            origin_context_id=origin,
            **extra,
        )

    preview = await add()
    with pytest.raises(ToolError, match="this conversation can't show one"):
        await add(content_hash=preview.preview.content_hash)
    assert world.created == [], "nothing was uploaded"


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
    """Once it is theirs: a member's binding alone leaves it a server admin's."""
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

    async def add() -> object:
        return await _add_skill_impl(
            world.runtime,
            world.auth(admin=False, platform="discord"),
            agent_name="helper",
            expected_ma_agent_id="agent_helper",
            skill_md=_MD,
        )

    with pytest.raises(ToolError, match="not made for, limited by its rule to or given to"):
        await add()
    async with db_session_factory.begin() as session:
        await record_creation_channel(
            session,
            tenant_id=world.tenant_id,
            ma_agent_id="agent_helper",
            platform="discord",
            channel_id=ROOM,
        )
    result = await add()
    assert getattr(result, "status", None) == "preview", "one made for their channel is theirs"


@pytest.mark.parametrize(
    ("platform", "url", "why"),
    [
        ("discord", "https://evil.example/skill.zip", "Discord attachment link"),
        ("discord", "http://cdn.discordapp.com/a/b/skill.zip", "Discord attachment link"),
        (None, "https://cdn.discordapp.com/a/b/skill.zip", "only from Discord, Slack or Teams"),
        ("teams", "https://cdn.discordapp.com/a/b/skill.zip", "SharePoint or OneDrive link"),
        ("teams", "http://contoso.sharepoint.com/s/skill.zip", "SharePoint or OneDrive link"),
        ("teams", "https://contoso.sharepoint.com.evil.example/x", "SharePoint or OneDrive link"),
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


def _mock_slack_file_shared_in(m: aioresponses, shares: dict[str, list[dict[str, str]]]) -> None:
    m.get(  # pyright: ignore[reportUnknownMemberType]
        re.compile(r"https://slack\.com/api/files\.info.*"),
        payload={"ok": True, "file": {"id": "F1", "shares": {"public": shares}}},
    )


async def _slack_skill_world(factory: async_sessionmaker[AsyncSession]) -> _World:
    world = await _world(factory)
    assert world.runtime.fernet is not None
    async with factory.begin() as session:
        await upsert_slack_bot_token(
            session, team_id="T_MINE", encrypted_token=encrypt_token(world.runtime.fernet, "xoxb")
        )
    return world


async def test_a_slack_file_link_is_refused_unless_the_caller_can_read_the_file(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link is a bearer token: another turn's link to a DM file proves nothing here."""
    world = await _slack_skill_world(db_session_factory)
    fetched: list[str] = []

    async def fake_fetch(*_: object, file_id: str, **__: object) -> tuple[bytes, str, str]:
        fetched.append(file_id)
        return _MD.encode(), "text/markdown", "SKILL.md"

    monkeypatch.setattr(skill_uploads, "fetch_slack_file", fake_fetch)
    token = mint_file_token(team_id="T_MINE", file_id="F1", exp=2**40, secret="proxy-secret")
    with aioresponses() as m:
        _mock_slack_file_shared_in(m, {"D_SOMEONE": [{"ts": "1.0"}]})
        with pytest.raises(ToolError, match="not shared anywhere the requester can read"):
            await _add_skill_impl(
                world.runtime,
                world.auth(platform="slack", external_id="T_MINE"),
                agent_name="helper",
                expected_ma_agent_id="agent_helper",
                attachment_url=f"https://daimon.example/slack/file/{token}",
            )
    assert fetched == [], "a refused file must never be downloaded"


async def test_a_slack_file_link_shared_in_a_channel_the_caller_reads_is_fetched(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _slack_skill_world(db_session_factory)
    fetched: list[str] = []

    async def fake_fetch(*_: object, file_id: str, **__: object) -> tuple[bytes, str, str]:
        fetched.append(file_id)
        return _MD.encode(), "text/markdown", "SKILL.md"

    monkeypatch.setattr(skill_uploads, "fetch_slack_file", fake_fetch)
    token = mint_file_token(team_id="T_MINE", file_id="F1", exp=2**40, secret="proxy-secret")
    with aioresponses() as m:
        _mock_slack_file_shared_in(m, {"C1": [{"ts": "1.0"}]})
        m.get(  # pyright: ignore[reportUnknownMemberType]
            re.compile(r"https://slack\.com/api/conversations\.info.*"),
            payload={"ok": True, "channel": {"id": "C1", "is_private": False}},
        )
        m.get(  # pyright: ignore[reportUnknownMemberType]
            re.compile(r"https://slack\.com/api/users\.info.*"),
            payload={"ok": True, "user": {"id": USER, "is_restricted": False}},
        )
        result = await _add_skill_impl(
            world.runtime,
            world.auth(platform="slack", external_id="T_MINE"),
            agent_name="helper",
            expected_ma_agent_id="agent_helper",
            attachment_url=f"https://daimon.example/slack/file/{token}",
        )
    assert fetched == ["F1"], "a file shared in a channel the caller reads is downloaded"
    assert result.status == "preview"


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


async def test_a_teams_shared_file_is_previewed_from_its_sharepoint_link(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _world(db_session_factory)
    fetched: list[str] = []

    async def fake_fetch(_http: httpx.AsyncClient, url: str) -> tuple[bytes, str]:
        fetched.append(url)
        return _MD.encode(), "SKILL.md"

    monkeypatch.setattr(skill_uploads, "fetch_teams_attachment", fake_fetch)
    url = "https://contoso-my.sharepoint.com/personal/a/_layouts/15/download.aspx?UniqueId=1"

    result = await _add_skill_impl(
        world.runtime,
        world.auth(platform="teams"),
        agent_name="helper",
        expected_ma_agent_id="agent_helper",
        attachment_url=url,
    )

    assert fetched == [url]
    assert result.status == "preview" and result.preview.files == ["SKILL.md"]


async def _setup_thread_origin(world: _World, factory: async_sessionmaker[AsyncSession]) -> str:
    """A verified setup-thread origin in ROOM, answered by the built-in ``shared``."""
    now = datetime.now(UTC)
    async with factory.begin() as session:
        origin = await create_origin(
            session,
            tenant_id=world.tenant_id,
            account_id=world.account_id,
            platform="discord",
            parent_channel_id=ROOM,
            thread_id=SETUP_THREAD,
            responder_ma_agent_id="agent_shared",
            responder_name="shared",
            configuration_target_ma_agent_id="agent_helper",
            configuration_target_name="helper",
            role=Role.USER,
            expires_at=now + timedelta(minutes=10),
            now=now,
            is_setup=True,
        )
    await _live_session(world, thread_id=SETUP_THREAD, responder="agent_shared")
    return str(origin.id)


async def test_an_isolated_channels_agent_takes_skills_only_from_inside_the_channel(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """C's setup thread adds to C's own agent; nothing outside C reaches it, it reaches
    nothing outside C, and its skill is listed only inside C."""
    world = await _world(db_session_factory)
    for agent_id, name, metadata in (
        ("agent_other", "other", {"daimon_account": str(world.account_id)}),
        ("agent_shared", "shared", {"daimon_managed": "true"}),
    ):
        agent = ma_agent(id=agent_id, name=name, tenant_id=world.tenant_id, metadata=metadata)
        world.state.agents[agent_id] = agent.model_dump(mode="json")
    async with db_session_factory.begin() as session:
        await set_access_policy(
            session,
            tenant_id=world.tenant_id,
            policy=TenantAccessPolicy(
                isolated_channel_ids=(ROOM,),
                sealed_channel_ids=(ROOM,),
                agent_channel_pins={"helper": (ROOM,)},
            ),
        )
    origin = await _setup_thread_origin(world, db_session_factory)

    def member_run_by(agent_id: str) -> AuthIdentity:
        return dataclasses.replace(
            world.auth(admin=False, platform="discord"),
            chat_agent_id=derive_agent_uuid(tenant_id=world.tenant_id, ma_agent_id=agent_id),
        )

    async def add(auth: AuthIdentity, agent_name: str, **extra: Any) -> AddSkillResult:
        return await _add_skill_impl(
            world.runtime,
            auth,
            agent_name=agent_name,
            expected_ma_agent_id=f"agent_{agent_name}",
            skill_md=_MD,
            **extra,
        )

    setup = member_run_by("agent_shared")
    preview = await add(setup, "helper", origin_context_id=origin)
    added = await add(
        setup, "helper", origin_context_id=origin, content_hash=preview.preview.content_hash
    )
    assert added.status == "added", "a member in C's setup thread adds to C's own agent"

    with pytest.raises(ToolError, match="missing or changed"):
        await add(setup, "helper")  # no origin: outside C
    with pytest.raises(ToolError, match="missing or changed"):
        await add(member_run_by("agent_other"), "helper")  # an agent outside C
    with pytest.raises(ToolError, match="missing or changed"):
        await add(member_run_by("agent_helper"), "other")  # C's agent reaching out

    inside = dataclasses.replace(
        world.auth(),
        chat_agent_id=derive_agent_uuid(tenant_id=world.tenant_id, ma_agent_id="agent_helper"),
    )
    assert [s.name for s in await _list_impl(world.runtime, inside)] == ["helper/notes"]
    assert await _list_impl(world.runtime, world.auth()) == [], "hidden outside the channel"


async def test_a_pinned_agent_takes_a_chat_add_only_from_its_own_channels(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The card's verified origin counts for the pin; without one a member is outside it."""
    world = await _world(db_session_factory)
    shared = ma_agent(
        id="agent_shared",
        name="shared",
        tenant_id=world.tenant_id,
        metadata={"daimon_managed": "true"},
    )
    world.state.agents["agent_shared"] = shared.model_dump(mode="json")
    async with db_session_factory.begin() as session:
        await set_access_policy(
            session,
            tenant_id=world.tenant_id,
            policy=TenantAccessPolicy(agent_channel_pins={"helper": (ROOM,)}),
        )
    member = dataclasses.replace(
        world.auth(admin=False, platform="discord"),
        chat_agent_id=derive_agent_uuid(tenant_id=world.tenant_id, ma_agent_id="agent_shared"),
    )

    async def preview(**extra: Any) -> AddSkillResult:
        return await _add_skill_impl(
            world.runtime,
            member,
            agent_name="helper",
            expected_ma_agent_id="agent_helper",
            skill_md=_MD,
            **extra,
        )

    with pytest.raises(ToolError, match="No card was posted"):
        await preview()
    origin = await _setup_thread_origin(world, db_session_factory)
    assert (await preview(origin_context_id=origin)).status == "preview", "inside the pin"


async def _preview_then_confirm(
    world: _World, *, admin: bool = True, gated: bool = True, **source: Any
) -> AddSkillResult:
    """Preview, then confirm, from one chat turn whose session is gated (or not)."""
    auth, origin = await _chat_turn(world, admin=admin, gated=gated)
    preview = await _add_skill_impl(
        world.runtime,
        auth,
        agent_name="helper",
        expected_ma_agent_id="agent_helper",
        origin_context_id=origin,
        **source,
    )
    return await _add_skill_impl(
        world.runtime,
        auth,
        agent_name="helper",
        expected_ma_agent_id="agent_helper",
        content_hash=preview.preview.content_hash,
        origin_context_id=origin,
        **source,
    )


@pytest.mark.parametrize("change", ["pinned_elsewhere", "made_the_default"])
async def test_a_pin_or_share_added_during_the_fetch_still_refuses_the_upload(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """The gates run again on the fresh agent after the (slow) fetch, before any write."""
    world = await _world(db_session_factory)
    load = skill_uploads._load_bundle  # pyright: ignore[reportPrivateUsage]
    loads: list[str] = []

    async def load_then_change(*args: Any, **kwargs: Any) -> Any:
        loaded = await load(*args, **kwargs)
        loads.append(change)
        if len(loads) == 2:  # the confirm's fetch, after its first check passed
            async with db_session_factory.begin() as session:
                if change == "pinned_elsewhere":
                    await set_access_policy(
                        session,
                        tenant_id=world.tenant_id,
                        policy=TenantAccessPolicy(agent_channel_pins={"helper": ("C_ELSEWHERE",)}),
                    )
                else:
                    await set_fields(
                        session,
                        scope=TenantScopeRef(tenant_id=world.tenant_id),
                        tenant_id=world.tenant_id,
                        agent_name="helper",
                        mode="agent",
                    )
        return loaded

    monkeypatch.setattr(skill_uploads, "_load_bundle", load_then_change)
    with pytest.raises(ToolError, match="No card was posted|used beyond this caller"):
        await _preview_then_confirm(world, admin=False, skill_md=_MD)
    assert world.created == [], "nothing was uploaded"
    assert world.state.agents["agent_helper"]["skills"] == []


async def test_with_approval_cards_off_the_preview_says_the_deployment_turned_them_off(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Outside a chat turn, tool safety off means no card and no reply to confirm with,
    and the preview and refusal blame the deployment, not the conversation."""
    world = await _world(db_session_factory)
    world.runtime.settings.tool_safety = ToolSafetyPolicy(enabled=False)
    auth, origin = world.auth(), None

    async def add(**extra: Any) -> AddSkillResult:
        return await _add_skill_impl(
            world.runtime,
            auth,
            agent_name="helper",
            expected_ma_agent_id="agent_helper",
            skill_md=_MD,
            origin_context_id=origin,
            **extra,
        )

    preview = await add()
    summary = preview.summary
    assert "/agent-setup" in summary and "content_hash=" not in summary
    assert "approval cards are turned off on this deployment" in summary
    assert "tool_safety.enabled" in summary, "the operator is told which setting"
    assert "this conversation can't show one" not in summary
    with pytest.raises(ToolError, match="approval cards are turned off on this deployment"):
        await add(content_hash=preview.preview.content_hash)
    assert world.created == [], "the confirm staged and uploaded nothing"


async def test_a_preview_confirms_only_for_the_agent_it_was_made_for(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session_factory)
    auth, origin = await _chat_turn(world)
    preview = await _add_skill_impl(
        world.runtime,
        auth,
        agent_name="helper",
        expected_ma_agent_id="agent_helper",
        skill_md=_MD,
        origin_context_id=origin,
    )
    raw = bundle_from_markdown(_MD).preview.content_hash
    assert preview.preview.content_hash != raw, "the hash to confirm is bound to agent_helper"
    with pytest.raises(ToolError, match="changed since its preview"):
        await _add_skill_impl(
            world.runtime,
            auth,
            agent_name="helper",
            expected_ma_agent_id="agent_helper",
            skill_md=_MD,
            content_hash=raw,
            origin_context_id=origin,
        )
    assert world.created == []


async def test_a_member_cannot_version_a_skill_a_default_fork_shares(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session_factory)
    await _preview_then_confirm(world, skill_md=_MD)
    helper = world.state.agents["agent_helper"]
    stamp = ma_agent(name="helper-fork", tenant_id=world.tenant_id).metadata
    world.state.agents["agent_fork"] = helper | {
        "id": "agent_fork",
        "name": "helper-fork",
        "metadata": helper["metadata"] | stamp,
    }
    async with db_session_factory.begin() as session:
        await set_fields(
            session,
            scope=TenantScopeRef(tenant_id=world.tenant_id),
            tenant_id=world.tenant_id,
            agent_name="helper-fork",
            mode="agent",
        )

    with pytest.raises(ToolError, match="also attached to another agent"):
        await _preview_then_confirm(world, admin=False, skill_md=_MD + "More.\n")
    assert len(world.created) == 1, "nothing new was uploaded"


async def test_a_member_cannot_change_an_agent_only_a_thread_uses(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session_factory)
    async with db_session_factory.begin() as session:
        await create_binding(
            session,
            tenant_id=world.tenant_id,
            platform="discord",
            parent_channel_id=ROOM,
            thread_id="222222222222222222",
            responder_ma_agent_id="agent_helper",
            responder_name="helper",
            kind="handoff",
        )
    with pytest.raises(ToolError, match="a bound thread"):
        await _add_skill_impl(
            world.runtime,
            world.auth(admin=False, platform="discord"),
            agent_name="helper",
            expected_ma_agent_id="agent_helper",
            skill_md=_MD,
        )


async def test_a_repo_skill_uses_stored_github_access_only_for_an_admin(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _world(db_session_factory)
    resolved: list[str] = []
    tokens: list[str | None] = []

    async def fake_resolve(_rt: object, _auth: object, url: str, _http: object) -> str:
        resolved.append(url)
        return "stored-token"

    async def fake_fetch(_http: object, *, token: str | None, **_kwargs: object):
        tokens.append(token)
        return bundle_from_markdown(_MD)

    monkeypatch.setattr(skill_uploads, "_resolve_sync_token", fake_resolve)
    monkeypatch.setattr(skill_uploads, "fetch_repo_skill", fake_fetch)
    url = "https://someone:ghp_secret@github.com/o/r.git?x=1"
    source = {"repo_url": url, "path": "skills/notes"}

    await _add_skill_impl(
        world.runtime,
        world.auth(admin=False),
        agent_name="helper",
        expected_ma_agent_id=None,
        **source,
    )
    assert (resolved, tokens) == ([], [None]), "a member fetches without the stored access"

    await _preview_then_confirm(world, **source)
    assert tokens[1:] == ["stored-token", "stored-token"]
    async with db_session_factory() as session:
        row = await load_user_skill(
            session,
            tenant_id=world.tenant_id,
            principal_id=derive_agent_uuid(tenant_id=world.tenant_id, ma_agent_id="agent_helper"),
            agent_name="helper",
            name="notes",
        )
    assert row is not None and row.origin == "o/r/skills/notes@main", "no credentials stored"


async def test_an_upstream_failure_is_a_tool_error(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _world(db_session_factory)

    async def failing(*_args: object, **_kwargs: object) -> None:
        response = httpx.Response(500, request=httpx.Request("POST", "https://api.example"))
        raise anthropic.APIStatusError("boom", response=response, body=None)

    monkeypatch.setattr(skill_uploads, "add_agent_skill", failing)
    with pytest.raises(ToolError, match=r"failed upstream \(HTTP 500\)"):
        await _preview_then_confirm(world, skill_md=_MD)


async def test_an_attachment_of_the_wrong_kind_is_refused_before_download(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _world(db_session_factory)
    fetched: list[str] = []

    async def fake_fetch(_http: httpx.AsyncClient, url: str) -> bytes:
        fetched.append(url)
        return b""

    monkeypatch.setattr(skill_uploads, "fetch_attachment", fake_fetch)
    with pytest.raises(ToolError, match="upload a SKILL.md or a .zip"):
        await _add_skill_impl(
            world.runtime,
            world.auth(platform="discord"),
            agent_name="helper",
            expected_ma_agent_id="agent_helper",
            attachment_url="https://cdn.discordapp.com/attachments/1/2/huge.iso",
        )
    assert fetched == []


@asynccontextmanager
async def _reply(
    world: _World, auth: AuthIdentity, text: str, *, thread_id: str = CHAT_THREAD
) -> AsyncIterator[str]:
    """The person's next message in a thread, as the adapter starts its turn with it."""
    async with turn_origin(
        world.runtime.session_factory,
        tenant_id=world.tenant_id,
        account_id=auth.account_id,
        platform="discord",
        parent_channel_id=ROOM,
        thread_id=thread_id,
        responder_ma_agent_id="agent_helper",
        responder_name="helper",
        role=auth.role,
        message_text=text,
    ) as origin:
        yield str(origin.id)


async def _cards_off_turn(world: _World) -> tuple[AuthIdentity, str]:
    """A member's chat turn in ROOM on a deployment with approval cards off."""
    world.runtime.settings.tool_safety = ToolSafetyPolicy(enabled=False)
    return await _chat_turn(world, admin=False)


async def _add_from(
    world: _World, auth: AuthIdentity, origin: str | None, **extra: Any
) -> AddSkillResult:
    return await _add_skill_impl(
        world.runtime,
        auth,
        agent_name="helper",
        expected_ma_agent_id="agent_helper",
        skill_md=extra.pop("skill_md", _MD),
        origin_context_id=origin,
        **extra,
    )


@pytest.mark.parametrize("text", ["yes", "<@1530628070405308456> Yes!", " CONFIRM ", "y."])
async def test_with_cards_off_a_yes_as_the_next_message_confirms_once(
    db_session_factory: async_sessionmaker[AsyncSession], text: str
) -> None:
    world = await _world(db_session_factory)
    auth, origin = await _cards_off_turn(world)

    preview = await _add_from(world, auth, origin)
    content_hash = preview.preview.content_hash
    assert f"content_hash='{content_hash}'" in preview.summary
    assert "`yes`" in preview.summary and "/agent-setup" not in preview.summary

    async with _reply(world, auth, text) as reply:
        added = await _add_from(world, auth, reply, content_hash=content_hash)
        assert added.status == "added" and len(world.created) == 1
        with pytest.raises(ToolError, match="already used"):
            await _add_from(world, auth, reply, content_hash=content_hash)


@pytest.mark.parametrize(
    "text", ["no", "what does it do?", "yes but change the name first", "sure", ""]
)
async def test_with_cards_off_any_other_next_message_cancels(
    db_session_factory: async_sessionmaker[AsyncSession], text: str
) -> None:
    world = await _world(db_session_factory)
    auth, origin = await _cards_off_turn(world)
    preview = await _add_from(world, auth, origin)

    async with _reply(world, auth, text) as reply:
        with pytest.raises(ToolError, match="did not start with the person replying"):
            await _add_from(world, auth, reply, content_hash=preview.preview.content_hash)
    async with _reply(world, auth, "yes") as late_yes:
        with pytest.raises(ToolError, match="did not start with the person replying"):
            await _add_from(world, auth, late_yes, content_hash=preview.preview.content_hash)
    assert world.created == []


async def test_with_cards_off_the_model_cannot_confirm_without_the_persons_yes(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """In the previewing turn, or with no origin, a model-supplied hash adds nothing."""
    world = await _world(db_session_factory)
    auth, origin = await _cards_off_turn(world)
    preview = await _add_from(world, auth, origin)

    with pytest.raises(ToolError, match="did not start with the person replying"):
        await _add_from(world, auth, origin, content_hash=preview.preview.content_hash)
    with pytest.raises(ToolError, match="approval cards are turned off"):
        await _add_from(world, auth, None, content_hash=preview.preview.content_hash)
    assert world.created == []


async def test_with_cards_off_nobody_else_confirms_and_nowhere_else(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Another person's yes (even an admin's), or the same person's yes in another
    thread, approves nothing."""
    world = await _world(db_session_factory)
    auth, origin = await _cards_off_turn(world)
    preview = await _add_from(world, auth, origin)
    content_hash = preview.preview.content_hash

    async with world.runtime.session_factory.begin() as session:
        other = await make_account(session)
    someone_else = dataclasses.replace(
        auth, account_id=other.id, role=Role.ADMIN, is_admin=True, platform_user_id="777"
    )
    async with _reply(world, someone_else, "yes") as theirs:
        with pytest.raises(ToolError, match="did not start with the person replying"):
            await _add_from(world, someone_else, theirs, content_hash=content_hash)
    async with _reply(world, auth, "yes", thread_id="666666666666666666") as elsewhere:
        with pytest.raises(ToolError, match="did not start with the person replying"):
            await _add_from(world, auth, elsewhere, content_hash=content_hash)
    assert world.created == []


async def test_with_cards_off_only_the_latest_preview_can_be_confirmed(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Preview A, then B in the same turn: the person's yes answers B, never A."""
    world = await _world(db_session_factory)
    auth, origin = await _cards_off_turn(world)
    first = await _add_from(world, auth, origin)
    other_md = _MD.replace("name: notes", "name: minutes")
    second = await _add_from(world, auth, origin, skill_md=other_md)

    async with _reply(world, auth, "yes") as reply:
        with pytest.raises(ToolError, match="did not start with the person replying"):
            await _add_from(world, auth, reply, content_hash=first.preview.content_hash)
        assert world.created == []
        added = await _add_from(
            world, auth, reply, skill_md=other_md, content_hash=second.preview.content_hash
        )
    assert added.status == "added" and len(world.created) == 1


async def test_with_cards_off_a_changed_skill_needs_a_fresh_preview(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session_factory)
    auth, origin = await _cards_off_turn(world)
    preview = await _add_from(world, auth, origin)

    changed = _MD.replace("Write them down.", "Email them to everyone.")
    async with _reply(world, auth, "yes") as reply:
        with pytest.raises(ToolError, match="changed since its preview"):
            await _add_from(
                world, auth, reply, skill_md=changed, content_hash=preview.preview.content_hash
            )
    assert world.created == []


async def test_with_cards_off_a_yes_after_the_ttl_confirms_nothing(
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = await _world(db_session_factory)
    auth, origin = await _cards_off_turn(world)
    monkeypatch.setattr("daimon.core.stores.pending_skill_adds.PENDING_SKILL_ADD_TTL", timedelta(0))
    preview = await _add_from(world, auth, origin)

    async with _reply(world, auth, "yes") as reply:
        with pytest.raises(ToolError, match="did not start with the person replying"):
            await _add_from(world, auth, reply, content_hash=preview.preview.content_hash)
    assert world.created == []


async def test_with_cards_off_a_member_still_cannot_add_to_a_shared_agent(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The yes changes who approves, not who may change the agent."""
    world = await _world(db_session_factory)
    async with db_session_factory.begin() as session:
        await set_fields(
            session,
            scope=ChannelScopeRef(tenant_id=world.tenant_id, channel_id=ROOM),
            tenant_id=world.tenant_id,
            agent_name="helper",
            mode="agent",
        )
    auth, origin = await _cards_off_turn(world)

    with pytest.raises(ToolError, match="needs a workspace or server admin"):
        await _add_from(world, auth, origin)
    assert world.created == []
