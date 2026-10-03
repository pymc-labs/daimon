"""Add skill on Details: who may open it, what the preview shows, what Add uploads."""

from __future__ import annotations

import io
import re
import uuid
import zipfile
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
import pytest
from anthropic.types.beta import SkillListResponse
from daimon.adapters.discord.agent_setup import add_skill as add_skill_mod
from daimon.adapters.discord.agent_setup.add_skill import (
    ADD_LABEL,
    ADD_SKILL_LABEL,
    BUILT_IN_MESSAGE,
    AddSkillModal,
    SkillPreviewView,
    preview_text,
)
from daimon.adapters.discord.agent_setup.details_view import DetailsView
from daimon.adapters.discord.agent_setup.state import PanelState
from daimon.adapters.discord.runtime import DiscordRuntime
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.agent_details import AgentDetails
from daimon.core.agent_pins import PIN_WRITE_REFUSAL
from daimon.core.defaults.metadata import tenant_scoped_display_title
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.notebooks._rate_limit import RateLimiter
from daimon.core.roster import RosterAgent
from daimon.core.scope import ChannelScopeRef, DeploymentDefault, TenantScopeRef
from daimon.core.skills.ingest import bundle_from_markdown
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.channel_admins import set_channel_admins
from daimon.core.stores.scoped_config_write import set_fields
from daimon.core.stores.user_skills import load_user_skill
from daimon.testing import ma_agent
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import (
    FakeMAState,
    NotHandled,
    build_fake_anthropic,
    combine_handlers,
    list_response,
    make_fake_ma_handler,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

GUILD_ID = 2001
CHANNEL_ID = 900000000000000001
THREAD_ID = 900000000000000009
USER_ID = 444444444444444444
_MD = "---\nname: notes\ndescription: Take meeting notes.\n---\nWrite them down.\n"


class _World:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, account_id: uuid.UUID
    ) -> None:
        self.factory, self.tenant_id, self.account_id = factory, tenant_id, account_id
        self.state = FakeMAState()
        self.created: list[str] = []
        self.skills: list[dict[str, Any]] = []

    def put_agent(self, *, managed: bool = False) -> RosterAgent:
        metadata = {"daimon_account": str(self.account_id)}
        if managed:
            metadata["daimon_managed"] = "true"
        agent = ma_agent(id="ag_helper", name="helper", tenant_id=self.tenant_id, metadata=metadata)
        self.state.agents[agent.id] = agent.model_dump(mode="json")
        return RosterAgent(
            name="helper", ma_agent_id="ag_helper", model_id="claude-sonnet-4-6", is_built_in=False
        )

    def _skills(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/v1/skills":
            raise NotHandled
        if request.method == "GET":
            return list_response(self.skills)
        found = re.search(rb'name="display_title"\r\n\r\n([^\r]+)', request.content)
        assert found is not None
        self.created.append(found.group(1).decode())
        skill = SkillListResponse(
            id=f"skill_{len(self.skills)}",
            type="custom",
            display_title=self.created[-1],
            latest_version="1",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            source="custom",
        ).model_dump(mode="json")
        self.skills.append(skill)
        return httpx.Response(200, json=skill)

    def runtime(self) -> DiscordRuntime:
        return DiscordRuntime(
            settings=MagicMock(),
            anthropic=build_fake_anthropic(
                combine_handlers(self._skills, make_fake_ma_handler(self.state))
            ),
            sessionmaker=self.factory,
            notebook_rate_limiter=RateLimiter(max_requests=999),
            billing_config=None,
            deployment_default=DeploymentDefault(),
            resolver_cache=new_resolver_cache(),
            turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # never runs a turn
        )


async def _world(factory: async_sessionmaker[AsyncSession]) -> _World:
    async with factory.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id=str(GUILD_ID))
        account = await make_account(session, tenant=tenant)
    return _World(factory, tenant.id, account.id)


def _details(name: str = "helper") -> AgentDetails:
    return AgentDetails(
        ma_agent_id="ag_helper",
        name=name,
        purpose=None,
        model_id="claude-sonnet-4-6",
        model_display_name="Sonnet 4.6",
        daimon_managed=False,
        created_by_is_workspace=True,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        answers_in=(),
        answers_here=False,
        repo=None,
        skills=(),
        skills_listing_truncated=False,
        mcp_servers=(),
        keys=(),
        applies_note=f"Changes to {name} apply from the next message to it.",
        unrouted_note=None,
    )


def _details_view(world: _World, agent: RosterAgent) -> DetailsView:
    state = PanelState(
        roster=[],
        selected=None,
        account_id=world.account_id,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        deployment_default=DeploymentDefault(),
        selected_agent=agent,
    )
    return DetailsView(
        state, runtime=world.runtime(), allowed_user_id=USER_ID, details=_details(), agent=agent
    )


def _interaction(
    *, admin: bool = False, channel_id: int = CHANNEL_ID, thread_of: int | None = None
) -> MagicMock:
    interaction = MagicMock()
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = USER_ID
    interaction.user.roles = []
    interaction.user.guild_permissions.administrator = admin
    interaction.user.guild_permissions.manage_guild = False
    interaction.user.guild.owner_id = 1
    interaction.guild_id = GUILD_ID
    interaction.channel = MagicMock(spec=discord.TextChannel)
    if thread_of is not None:
        interaction.channel = MagicMock(spec=discord.Thread)
        interaction.channel.id = channel_id
        interaction.channel.parent_id = thread_of
    interaction.channel_id = channel_id
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.response.defer = AsyncMock(
        side_effect=lambda: interaction.response.is_done.configure_mock(return_value=True)
    )
    interaction.followup.send = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    return interaction


def _button(view: discord.ui.LayoutView, label: str) -> discord.ui.Button[Any]:
    return next(
        item
        for item in view.walk_children()
        if isinstance(item, discord.ui.Button) and item.label == label
    )


def _shown(interaction: MagicMock) -> Any:
    return interaction.edit_original_response.await_args.kwargs["view"]


def _followups(interaction: MagicMock) -> list[str]:
    return [call.args[0] for call in interaction.followup.send.await_args_list]


async def _route(
    world: _World, scope: ChannelScopeRef | TenantScopeRef, *, by_admin: bool = False
) -> None:
    async with world.factory.begin() as session:
        await set_fields(
            session,
            scope=scope,
            tenant_id=world.tenant_id,
            agent_name="helper",
            mode="agent",
            set_by_admin=by_admin,
        )


async def test_a_member_opens_the_form_for_an_agent_that_answers_nowhere(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session_factory)
    view = _details_view(world, world.put_agent())
    interaction = _interaction()

    await _button(view, ADD_SKILL_LABEL).callback(interaction)

    (modal,) = interaction.response.send_modal.await_args.args
    assert isinstance(modal, AddSkillModal)
    assert len(modal.children) <= 5, "a modal holds at most five components"


async def test_a_built_in_agent_is_refused_with_the_fork_route(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session_factory)
    built_in = world.put_agent().model_copy(update={"is_built_in": True})
    interaction = _interaction(admin=True)

    await _button(_details_view(world, built_in), ADD_SKILL_LABEL).callback(interaction)

    interaction.response.send_modal.assert_not_awaited()
    assert interaction.response.send_message.await_args.args[0] == BUILT_IN_MESSAGE


async def test_a_server_default_needs_a_server_admin(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session_factory)
    agent = world.put_agent()
    await _route(world, TenantScopeRef(tenant_id=world.tenant_id))
    member = _interaction()
    await _button(_details_view(world, agent), ADD_SKILL_LABEL).callback(member)
    member.response.send_modal.assert_not_awaited()
    assert "needs someone with Manage Server" in member.response.send_message.await_args.args[0]

    admin = _interaction(admin=True)
    await _button(_details_view(world, agent), ADD_SKILL_LABEL).callback(admin)
    admin.response.send_modal.assert_awaited_once()


async def test_a_channel_admin_may_add_to_an_agent_local_to_their_channel(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Only once a server admin made it the channel's default; a member's binding is not enough."""
    world = await _world(db_session_factory)
    agent = world.put_agent()
    await _route(world, ChannelScopeRef(tenant_id=world.tenant_id, channel_id=str(CHANNEL_ID)))
    member = _interaction()
    await _button(_details_view(world, agent), ADD_SKILL_LABEL).callback(member)
    member.response.send_modal.assert_not_awaited()

    async with db_session_factory.begin() as session:
        await set_channel_admins(
            session,
            tenant_id=world.tenant_id,
            platform="discord",
            channel_id=str(CHANNEL_ID),
            role_ids=[],
            user_ids=[str(USER_ID)],
            actor_account_id=None,
        )
    member_bound = _interaction()
    await _button(_details_view(world, agent), ADD_SKILL_LABEL).callback(member_bound)
    member_bound.response.send_modal.assert_not_awaited()

    await _route(
        world, ChannelScopeRef(tenant_id=world.tenant_id, channel_id=str(CHANNEL_ID)), by_admin=True
    )
    channel_admin = _interaction()
    await _button(_details_view(world, agent), ADD_SKILL_LABEL).callback(channel_admin)
    channel_admin.response.send_modal.assert_awaited_once()


def _modal(view: DetailsView, agent: RosterAgent, *, paste: str = "", upload: Any = None) -> Any:
    modal = AddSkillModal(view, agent)
    modal.paste._value = paste  # pyright: ignore[reportPrivateUsage]  # set what Discord would submit
    modal.upload._values = [upload] if upload is not None else []  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    return modal


def _attachment(data: bytes, filename: str) -> MagicMock:
    attachment = MagicMock(spec=discord.Attachment)
    attachment.filename, attachment.size = filename, len(data)
    attachment.read = AsyncMock(return_value=data)
    return attachment


async def test_a_pasted_skill_is_previewed_and_nothing_is_uploaded(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session_factory)
    agent = world.put_agent()
    interaction = _interaction()

    await _modal(_details_view(world, agent), agent, paste=_MD).on_submit(interaction)

    shown = _shown(interaction)
    assert isinstance(shown, SkillPreviewView)
    text = "\n".join(
        item.content for item in shown.walk_children() if isinstance(item, discord.ui.TextDisplay)
    )
    assert "Add notes to helper?" in text and "`SKILL.md`" in text
    assert world.created == [], "the preview uploads nothing"


async def test_an_uploaded_zip_flags_its_scripts_and_an_oversized_file_is_never_read(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session_factory)
    agent = world.put_agent()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("notes/SKILL.md", _MD)
        archive.writestr("notes/run.sh", "#!/bin/sh\n")
    interaction = _interaction()
    upload = _attachment(buffer.getvalue(), "notes.zip")

    await _modal(_details_view(world, agent), agent, upload=upload).on_submit(interaction)

    shown = _shown(interaction)
    assert isinstance(shown, SkillPreviewView)
    assert shown.bundle.preview.scripts == ["run.sh"]
    assert shown.origin == "attachment notes.zip"

    huge = _attachment(b"", "big.zip")
    huge.size = 10**9
    refused = _interaction()
    await _modal(_details_view(world, agent), agent, upload=huge).on_submit(refused)
    huge.read.assert_not_awaited()
    assert "larger than a skill may be" in _followups(refused)[0]


@pytest.mark.parametrize("both", [True, False])
async def test_exactly_one_of_paste_or_file_is_taken(
    db_session_factory: async_sessionmaker[AsyncSession], both: bool
) -> None:
    world = await _world(db_session_factory)
    agent = world.put_agent()
    interaction = _interaction()
    upload = _attachment(_MD.encode(), "SKILL.md") if both else None

    await _modal(
        _details_view(world, agent), agent, paste=_MD if both else "", upload=upload
    ).on_submit(interaction)

    assert "not both or neither" in _followups(interaction)[0]
    interaction.edit_original_response.assert_not_awaited()


def test_the_preview_shows_the_description_as_plain_text() -> None:
    md = _MD.replace("Take meeting notes.", "Ping @everyone in **bold**.")
    text = preview_text(bundle_from_markdown(md).preview, agent_name="helper")
    assert "@\u200beveryone" in text and "\\*\\*bold\\*\\*" in text


async def test_a_file_of_the_wrong_kind_is_never_read(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session_factory)
    agent = world.put_agent()
    upload = _attachment(b"", "notes.tar")
    interaction = _interaction()

    await _modal(_details_view(world, agent), agent, upload=upload).on_submit(interaction)

    upload.read.assert_not_awaited()
    assert "upload a SKILL.md or a .zip" in _followups(interaction)[0]


async def test_a_failure_after_the_form_closes_is_reported(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _world(db_session_factory)
    agent = world.put_agent()
    monkeypatch.setattr(
        add_skill_mod, "skill_change_refusal", AsyncMock(side_effect=DaimonError("down"))
    )
    interaction = _interaction()

    await _modal(_details_view(world, agent), agent, paste=_MD).on_submit(interaction)

    assert _followups(interaction), "the deferred form gets an answer"
    interaction.edit_original_response.assert_not_awaited()


async def _preview(world: _World, agent: RosterAgent) -> SkillPreviewView:
    interaction = _interaction()
    await _modal(_details_view(world, agent), agent, paste=_MD).on_submit(interaction)
    return _shown(interaction)


async def test_add_uploads_the_agents_own_skill_and_records_who_added_it(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _world(db_session_factory)
    agent = world.put_agent()
    preview = await _preview(world, agent)
    monkeypatch.setattr(add_skill_mod, "load_details_for", AsyncMock(return_value=_details()))
    interaction = _interaction()

    await _button(preview, ADD_LABEL).callback(interaction)

    assert world.created == [
        tenant_scoped_display_title(tenant_id=world.tenant_id, name="notes", agent_name="helper")
    ], "never the shared library"
    assert world.state.agents["ag_helper"]["skills"] == [{"type": "custom", "skill_id": "skill_0"}]
    async with db_session_factory() as session:
        row = await load_user_skill(
            session,
            tenant_id=world.tenant_id,
            principal_id=derive_agent_uuid(tenant_id=world.tenant_id, ma_agent_id="ag_helper"),
            agent_name="helper",
            name="notes",
        )
    assert row is not None
    assert (row.source, row.origin, row.added_by_account_id) == (
        "upload",
        "pasted",
        world.account_id,
    )
    assert _followups(interaction) == ["helper now has the skill **notes**."]
    assert isinstance(_shown(interaction), DetailsView), "Add returns to a fresh Details"


async def test_add_re_reads_the_agent_and_refuses_one_that_became_built_in(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session_factory)
    agent = world.put_agent()
    preview = await _preview(world, agent)
    world.put_agent(managed=True)
    interaction = _interaction()

    await _button(preview, ADD_LABEL).callback(interaction)

    assert _followups(interaction) == [BUILT_IN_MESSAGE]
    assert world.created == []


OTHER_CHANNEL_ID = 900000000000000002


async def _pin(world: _World, *channels: int) -> None:
    async with world.factory.begin() as session:
        await set_access_policy(
            session,
            tenant_id=world.tenant_id,
            policy=TenantAccessPolicy(
                agent_channel_pins={"helper": tuple(str(c) for c in channels)}
            ),
        )


async def _make_channel_admin(world: _World, channel_id: int) -> None:
    async with world.factory.begin() as session:
        await set_channel_admins(
            session,
            tenant_id=world.tenant_id,
            platform="discord",
            channel_id=str(channel_id),
            role_ids=[],
            user_ids=[str(USER_ID)],
            actor_account_id=None,
        )


async def test_a_pinned_agent_opens_add_skill_only_inside_its_channels_or_for_an_admin(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The panel's channel is the place, as a chat add's origin is."""
    world = await _world(db_session_factory)
    agent = world.put_agent()
    await _pin(world, OTHER_CHANNEL_ID)

    outside = _interaction()
    await _button(_details_view(world, agent), ADD_SKILL_LABEL).callback(outside)
    outside.response.send_modal.assert_not_awaited()
    assert outside.response.send_message.await_args.args[0] == PIN_WRITE_REFUSAL

    for allowed in (_interaction(channel_id=OTHER_CHANNEL_ID), _interaction(admin=True)):
        await _button(_details_view(world, agent), ADD_SKILL_LABEL).callback(allowed)
        allowed.response.send_modal.assert_awaited_once()

    await _make_channel_admin(world, OTHER_CHANNEL_ID)
    channel_admin = _interaction()
    await _button(_details_view(world, agent), ADD_SKILL_LABEL).callback(channel_admin)
    # An admin of every pinned channel may add from anywhere.
    channel_admin.response.send_modal.assert_awaited_once()


async def test_a_member_in_a_thread_of_a_pinned_channel_may_add(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A thread is inside its parent channel's pin; a thread elsewhere is not."""
    world = await _world(db_session_factory)
    agent = world.put_agent()
    await _pin(world, OTHER_CHANNEL_ID)

    inside = _interaction(channel_id=THREAD_ID, thread_of=OTHER_CHANNEL_ID)
    await _button(_details_view(world, agent), ADD_SKILL_LABEL).callback(inside)
    inside.response.send_modal.assert_awaited_once()

    elsewhere = _interaction(channel_id=THREAD_ID, thread_of=CHANNEL_ID)
    await _button(_details_view(world, agent), ADD_SKILL_LABEL).callback(elsewhere)
    elsewhere.response.send_modal.assert_not_awaited()


async def test_a_pin_added_after_the_button_refuses_the_submit_and_the_add(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session_factory)
    agent = world.put_agent()
    preview = await _preview(world, agent)
    await _pin(world, OTHER_CHANNEL_ID)

    submit = _interaction()
    await _modal(_details_view(world, agent), agent, paste=_MD).on_submit(submit)
    assert _followups(submit) == [PIN_WRITE_REFUSAL]
    submit.edit_original_response.assert_not_awaited()

    add = _interaction()
    await _button(preview, ADD_LABEL).callback(add)
    assert _followups(add) == [PIN_WRITE_REFUSAL]
    assert world.created == [], "nothing was uploaded"


async def test_an_agent_shared_after_the_button_refuses_the_submit_and_the_add(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(db_session_factory)
    agent = world.put_agent()
    preview = await _preview(world, agent)
    await _route(world, TenantScopeRef(tenant_id=world.tenant_id))

    submit = _interaction()
    await _modal(_details_view(world, agent), agent, paste=_MD).on_submit(submit)
    assert "needs someone with Manage Server" in _followups(submit)[0]
    submit.edit_original_response.assert_not_awaited()

    add = _interaction()
    await _button(preview, ADD_LABEL).callback(add)
    assert "needs someone with Manage Server" in _followups(add)[0]
    assert world.created == [], "nothing was uploaded"


async def test_an_agent_that_turned_built_in_during_the_upload_is_left_unattached(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The attach re-checks the agent as it is then, not as it was at the click."""
    world = await _world(db_session_factory)
    agent = world.put_agent()
    preview = await _preview(world, agent)
    upload = world._skills  # pyright: ignore[reportPrivateUsage]

    def upload_then_pin(request: httpx.Request) -> httpx.Response:
        response = upload(request)
        if request.method == "POST":
            world.state.agents["ag_helper"]["metadata"] = {
                **world.state.agents["ag_helper"]["metadata"],  # pyright: ignore[reportGeneralTypeIssues]
                "daimon_managed": "true",
            }
        return response

    world._skills = upload_then_pin  # type: ignore[method-assign]  # swap the fake mid-run
    preview.runtime = world.runtime()
    interaction = _interaction()
    await _button(preview, ADD_LABEL).callback(interaction)

    assert len(world.created) == 1, "uploaded before the change"
    assert world.state.agents["ag_helper"]["skills"] == [], "but never attached"
    assert _followups(interaction) == [BUILT_IN_MESSAGE]
