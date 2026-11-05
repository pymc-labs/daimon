"""Teams tidy tools: an agent edits and deletes only what it posted itself.

Messages go out through the real ``send_message`` impl so the ownership
record is written as in production. Bot Framework is faked at the HTTP layer.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.teams._client import TeamsBotClient
from daimon.adapters.mcp.tools.teams._send import (  # pyright: ignore[reportPrivateUsage]
    _teams_send_message_impl,
)
from daimon.adapters.mcp.tools.teams._tidy import (  # pyright: ignore[reportPrivateUsage]
    _teams_delete_message_impl,
    _teams_delete_reply_impl,
    _teams_edit_message_impl,
)
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.channel_tidy import (
    PER_TURN_LIMIT,
    content_hash,
    derive_content_key,
    record_turn_post,
)
from daimon.core.config import (
    AnthropicSettings,
    CryptoSettings,
    DatabaseSettings,
    McpSettings,
    Settings,
    SupportSettings,
)
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.agent_posts import PostRole, get_post
from daimon.core.stores.domain import Role
from daimon.core.stores.security_audit import SecurityAuditRow, list_events
from daimon.core.stores.turn_card_intents import (
    create_turn_card_intent,
    record_turn_card_message,
    retire_turn_card_intent,
)
from daimon.core.stores.turn_origins import create_origin
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent
from fastmcp.exceptions import ToolError
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic, list_response
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_ENTRA = "99999999-8888-7777-6666-555555555555"
_CALLER = "11111111-2222-3333-4444-555555555555"
_CHANNEL = "19:abc@thread.tacv2"
_THREAD = f"{_CHANNEL};messageid=1700000000000"
_BASE = "https://smba.trafficmanager.net/teams/v3/conversations"
_CHAT = "a:1on1-chat"
_AGENT = "ag_acme"
_OTHER_AGENT = "ag_other"


class _Teams:
    """Bot Framework: every roster check passes; posts get ids act-1, act-2, ...

    Deleting an activity in `gone` answers 404, as for one deleted by hand.
    """

    def __init__(self) -> None:
        self.writes: list[httpx.Request] = []
        self.gone: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://login.microsoftonline.com/"):
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if "/members/" in url:
            return httpx.Response(200, json={"aadObjectId": _CALLER})
        self.writes.append(request)
        if request.method == "DELETE" and url.rsplit("/", 1)[-1] in self.gone:
            return httpx.Response(404, json={})
        return httpx.Response(200, json={"id": f"act-{len(self.writes)}"})

    def edits_and_deletes(self) -> list[tuple[str, str]]:
        return [
            (r.method, str(r.url).removeprefix(_BASE))
            for r in self.writes
            if r.method in ("PUT", "DELETE")
        ]


@dataclass
class _World:
    runtime: McpRuntime
    teams: _Teams
    tenant_id: uuid.UUID
    account_id: uuid.UUID
    sessionmaker: async_sessionmaker[AsyncSession]
    content_key: bytes

    async def turn(self, *, agent: str = _AGENT) -> tuple[AuthIdentity, str]:
        now = datetime.now(UTC)
        async with self.sessionmaker.begin() as s:
            origin = await create_origin(
                s,
                tenant_id=self.tenant_id,
                account_id=self.account_id,
                platform="teams",
                parent_channel_id=_CHANNEL,
                thread_id=_THREAD,
                responder_ma_agent_id=agent,
                responder_name=agent,
                configuration_target_ma_agent_id=None,
                configuration_target_name=None,
                role=Role.USER,
                sender_foreign=False,
                shared_channel=False,
                expires_at=now + timedelta(hours=1),
                now=now,
                chat_session_id="test-session",
            )
        auth = AuthIdentity(
            account_id=self.account_id,
            tenant_id=self.tenant_id,
            role=Role.USER,
            platform="teams",
            external_id=_ENTRA,
            platform_user_id=_CALLER,
            chat_agent_id=derive_agent_uuid(tenant_id=self.tenant_id, ma_agent_id=agent),
            chat_session_id="test-session",
        )
        return auth, str(origin.id)

    async def post(self, auth: AuthIdentity, content: str = "first draft") -> str:
        row = await _teams_send_message_impl(
            self.runtime, auth, channel_id=_THREAD, content=content
        )
        return row.activity_id

    async def audit(self) -> list[SecurityAuditRow]:
        async with self.sessionmaker() as s:
            return await list_events(s, tenant_id=self.tenant_id)


async def _world(sessionmaker: async_sessionmaker[AsyncSession]) -> _World:
    fernet_key = SecretStr(Fernet.generate_key().decode("ascii"))
    async with sessionmaker.begin() as s:
        tenant = await make_tenant(s, platform="teams", workspace_id=_ENTRA)
        account = await make_account(s, tenant=tenant)
    router = MARouter()
    agents: list[dict[str, Any]] = [
        ma_agent(
            id=agent_id,
            name=agent_id,
            metadata={"daimon_tenant": str(tenant.id), "daimon_name": agent_id},
        ).model_dump(mode="json")
        for agent_id in (_AGENT, _OTHER_AGENT)
    ]
    router.add("GET", r"/v1/agents", lambda _r, _m: list_response(agents))
    teams = _Teams()
    runtime = McpRuntime(
        session_factory=sessionmaker,
        client=build_fake_anthropic(router.dispatch),  # type: ignore[arg-type]
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            crypto=CryptoSettings(keys=(fernet_key,)),
            mcp=McpSettings(),
        ),
        deployment_default=DeploymentDefault(),
        teams_client=TeamsBotClient(
            httpx.AsyncClient(transport=httpx.MockTransport(teams)),
            client_id="app-id",
            client_secret="secret",
            tenant_id=_ENTRA,
        ),
    )
    return _World(
        runtime,
        teams,
        tenant.id,
        account.id,
        sessionmaker,
        derive_content_key(fernet_key.get_secret_value()),
    )


async def test_an_agent_edits_then_deletes_its_own_teams_message(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Edit and delete reach the posted activity; each is audited with a hash, never text."""
    world = await _world(committing_sessionmaker)
    auth, origin = await world.turn()
    activity = await world.post(auth)

    edited = await _teams_edit_message_impl(
        world.runtime,
        auth,
        channel_id=_THREAD,
        message_id=activity,
        content="second draft",
        origin_context_id=origin,
    )
    deleted = await _teams_delete_message_impl(
        world.runtime, auth, channel_id=_THREAD, message_id=activity, origin_context_id=origin
    )

    assert (edited.action, deleted.action) == ("edited", "deleted"), "both actions report back"
    path = f"/{_THREAD}/activities/{activity}"
    assert world.teams.edits_and_deletes() == [("PUT", path), ("DELETE", path)], (
        "the edit replaces the posted activity in place, then the delete removes it"
    )
    put = next(r for r in world.teams.writes if r.method == "PUT")
    assert json.loads(put.content)["text"] == "second draft", "the edit carries the new text"
    rows = [r for r in await world.audit() if r.target_message_id == activity]
    assert [(r.tool_name, r.outcome) for r in rows] == [
        ("edit_message", "allowed"),
        ("delete_message", "allowed"),
    ], "each action writes one allowed audit row"
    assert rows[0].content_hmac == content_hash("first draft", world.content_key), (
        "the edit row records a hash of the text it replaced"
    )


async def test_teams_messages_not_posted_by_the_agent_are_refused_without_a_call(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A person's message has no record and another agent's is theirs: both are refused."""
    world = await _world(committing_sessionmaker)
    other, _ = await world.turn(agent=_OTHER_AGENT)
    auth, origin = await world.turn()
    theirs = await world.post(other)

    with pytest.raises(ToolError, match="not posted by you"):
        await _teams_delete_message_impl(
            world.runtime, auth, channel_id=_THREAD, message_id="act-99", origin_context_id=origin
        )
    with pytest.raises(ToolError, match="another agent posted this message"):
        await _teams_edit_message_impl(
            world.runtime,
            auth,
            channel_id=_THREAD,
            message_id=theirs,
            content="rewritten",
            origin_context_id=origin,
        )
    assert world.teams.edits_and_deletes() == [], "a refused call never reaches Teams"
    reasons = {r.reason for r in await world.audit() if r.outcome == "denied"}
    assert reasons == {"not_posted_by_agent", "other_agent"}, "each refusal is audited"


async def test_a_protected_teams_channel_refuses_tidying(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Protection set after the post still stops the edit: the policy is read at call time."""
    world = await _world(committing_sessionmaker)
    auth, origin = await world.turn()
    activity = await world.post(auth)
    async with committing_sessionmaker.begin() as s:
        await set_access_policy(
            s,
            tenant_id=world.tenant_id,
            policy=TenantAccessPolicy(protected_channel_ids=(_CHANNEL,)),
        )

    with pytest.raises(ToolError, match="writers to none"):
        await _teams_edit_message_impl(
            world.runtime,
            auth,
            channel_id=_THREAD,
            message_id=activity,
            content="second draft",
            origin_context_id=origin,
        )
    assert world.teams.edits_and_deletes() == [], "nothing reaches Teams"


async def test_the_teams_escalation_channel_and_its_threads_are_never_tidied(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(committing_sessionmaker)
    auth, origin = await world.turn()
    activity = await world.post(auth)
    world.runtime.settings.support = SupportSettings(teams_escalation_channel_id=_CHANNEL)

    with pytest.raises(ToolError, match="support-escalation channel"):
        await _teams_delete_message_impl(
            world.runtime, auth, channel_id=_THREAD, message_id=activity, origin_context_id=origin
        )
    assert world.teams.edits_and_deletes() == [], "nothing reaches Teams"


async def _turn_post(
    world: _World, conversation: str, *, running: bool, requester: str = _CALLER
) -> str:
    """A turn's answer in `conversation`, recorded as the Teams lifecycle does."""
    activity = f"act-turn-{uuid.uuid4().hex[:6]}"
    async with world.sessionmaker.begin() as s:
        intent = await create_turn_card_intent(
            s,
            tenant_id=world.tenant_id,
            platform="teams",
            thread_id=conversation,
            turn_token=uuid.uuid4(),
        )
        await record_turn_card_message(s, id=intent.id, message_id=activity)
        if not running:
            await retire_turn_card_intent(s, id=intent.id, expected_message_id=activity)
    await record_turn_post(
        world.sessionmaker,
        tenant_id=world.tenant_id,
        platform="teams",
        ma_agent_id=_AGENT,
        channel_id=conversation,
        message_id=activity,
        requester_platform_user_id=requester,
        source="turn",
        turn_card_intent_id=intent.id,
        parent_channel_id=None if conversation == _CHAT else _CHANNEL,
    )
    return activity


@pytest.mark.parametrize("conversation", [_CHAT, _THREAD])
async def test_the_requester_tidies_a_finished_turns_reply(
    committing_sessionmaker: async_sessionmaker[AsyncSession], conversation: str
) -> None:
    """A turn's reply, in a 1:1 chat or a thread, is the agent's to tidy once the turn is over."""
    world = await _world(committing_sessionmaker)
    auth, origin = await world.turn()
    activity = await _turn_post(world, conversation, running=False)

    deleted = await _teams_delete_message_impl(
        world.runtime, auth, channel_id=conversation, message_id=activity, origin_context_id=origin
    )

    assert deleted.action == "deleted", "the requester's finished turn reply is deleted"
    assert world.teams.edits_and_deletes() == [
        ("DELETE", f"/{conversation}/activities/{activity}")
    ], "the delete reaches the turn's activity"


async def test_a_running_turn_or_another_persons_turn_is_not_tidied(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The turn must be over, and the caller must be who asked for it (or a Teams admin)."""
    world = await _world(committing_sessionmaker)
    auth, origin = await world.turn()
    running = await _turn_post(world, _CHAT, running=True)
    theirs = await _turn_post(world, _CHAT, running=False, requester="someone-else")

    with pytest.raises(ToolError, match="still running"):
        await _teams_delete_message_impl(
            world.runtime, auth, channel_id=_CHAT, message_id=running, origin_context_id=origin
        )
    with pytest.raises(ToolError, match="only the person who started"):
        await _teams_edit_message_impl(
            world.runtime,
            auth,
            channel_id=_CHAT,
            message_id=theirs,
            content="rewritten",
            origin_context_id=origin,
        )
    assert world.teams.edits_and_deletes() == [], "a refused call never reaches Teams"
    reasons = {r.reason for r in await world.audit() if r.outcome == "denied"}
    assert reasons == {"turn_in_progress", "not_conversation_owner"}, "each refusal is audited"


async def _routine_post(world: _World, *, requester: str = _CALLER) -> str:
    """A routine's result posted to the channel, recorded as Teams routine delivery does."""
    activity = f"act-routine-{uuid.uuid4().hex[:6]}"
    await record_turn_post(
        world.sessionmaker,
        tenant_id=world.tenant_id,
        platform="teams",
        ma_agent_id=_AGENT,
        channel_id=f"{_CHANNEL};messageid={activity}",
        message_id=activity,
        requester_platform_user_id=requester,
        source="routine",
        parent_channel_id=_CHANNEL,
    )
    return activity


@pytest.mark.parametrize("named", ["thread", "channel"])
async def test_a_threads_root_is_found_under_either_form_of_its_channel(
    committing_sessionmaker: async_sessionmaker[AsyncSession], named: str
) -> None:
    """send_message to a channel records the root under the channel, a routine under
    its thread; either is tidied whichever form names it, acting on the recorded one."""
    world = await _world(committing_sessionmaker)
    auth, origin = await world.turn()
    sent = await _teams_send_message_impl(world.runtime, auth, channel_id=_CHANNEL, content="hi")
    routine = await _routine_post(world)

    for activity, recorded in [(sent.activity_id, _CHANNEL), (routine, None)]:
        recorded = recorded or f"{_CHANNEL};messageid={activity}"
        channel_id = f"{_CHANNEL};messageid={activity}" if named == "thread" else _CHANNEL
        await _teams_delete_message_impl(
            world.runtime,
            auth,
            channel_id=channel_id,
            message_id=activity,
            origin_context_id=origin,
        )
        assert world.teams.edits_and_deletes()[-1] == (
            "DELETE",
            f"/{recorded}/activities/{activity}",
        ), "the delete acts on the form the post is recorded under"


@pytest.mark.parametrize("admin", [True, False])
async def test_a_teams_admin_tidies_another_persons_finished_turn(
    committing_sessionmaker: async_sessionmaker[AsyncSession], admin: bool
) -> None:
    """A Teams admin may tidy anyone's finished turn post; a member only their own."""
    world = await _world(committing_sessionmaker)
    auth, origin = await world.turn()
    auth = replace(auth, is_platform_admin=admin)
    theirs = await _turn_post(world, _THREAD, running=False, requester="someone-else")

    async def delete() -> str:
        result = await _teams_delete_message_impl(
            world.runtime, auth, channel_id=_THREAD, message_id=theirs, origin_context_id=origin
        )
        return result.action

    if admin:
        assert await delete() == "deleted", "an admin tidies another person's turn"
    else:
        with pytest.raises(ToolError, match="only the person who started"):
            await delete()
        assert world.teams.edits_and_deletes() == [], "a refused call never reaches Teams"


@pytest.mark.parametrize("creator", [True, False])
async def test_a_routines_result_is_tidied_only_for_its_creator(
    committing_sessionmaker: async_sessionmaker[AsyncSession], creator: bool
) -> None:
    world = await _world(committing_sessionmaker)
    auth, origin = await world.turn()
    routine = await _routine_post(world, requester=_CALLER if creator else "someone-else")
    thread = f"{_CHANNEL};messageid={routine}"

    async def edit() -> str:
        result = await _teams_edit_message_impl(
            world.runtime,
            auth,
            channel_id=thread,
            message_id=routine,
            content="rewritten",
            origin_context_id=origin,
        )
        return result.action

    if creator:
        assert await edit() == "edited", "the routine's creator tidies its result"
    else:
        with pytest.raises(ToolError, match="only the person who started"):
            await edit()
        assert world.teams.edits_and_deletes() == [], "a refused call never reaches Teams"


async def _turn_reply(
    world: _World,
    conversation: str,
    roles: tuple[PostRole, ...] = ("status", "answer", "answer", "controls"),
    *,
    running: bool = False,
    requester: str = _CALLER,
) -> tuple[uuid.UUID, list[str]]:
    """One turn's messages in `conversation`, oldest first, as the lifecycle records them."""
    ids = [f"act-{role}-{uuid.uuid4().hex[:6]}" for role in roles]
    async with world.sessionmaker.begin() as s:
        intent = await create_turn_card_intent(
            s,
            tenant_id=world.tenant_id,
            platform="teams",
            thread_id=conversation,
            turn_token=uuid.uuid4(),
        )
        await record_turn_card_message(s, id=intent.id, message_id=ids[0])
        if not running:
            await retire_turn_card_intent(s, id=intent.id, expected_message_id=ids[0])
    for activity, role in zip(ids, roles, strict=True):
        await record_turn_post(
            world.sessionmaker,
            tenant_id=world.tenant_id,
            platform="teams",
            ma_agent_id=_AGENT,
            channel_id=conversation,
            message_id=activity,
            requester_platform_user_id=requester,
            source="turn",
            turn_card_intent_id=intent.id,
            parent_channel_id=None if conversation == _CHAT else _CHANNEL,
            role=role,
        )
    return intent.id, ids


def _deleted(world: _World) -> list[str]:
    return [
        path.rsplit("/", 1)[-1]
        for method, path in world.teams.edits_and_deletes()
        if method == "DELETE"
    ]


@pytest.mark.parametrize("conversation", [_CHAT, _THREAD])
async def test_delete_reply_removes_every_part_of_one_reply_and_nothing_else(
    committing_sessionmaker: async_sessionmaker[AsyncSession], conversation: str
) -> None:
    """Any part names the reply; its text and cards go, newest first, each audited."""
    world = await _world(committing_sessionmaker)
    auth, origin = await world.turn()
    intent, parts = await _turn_reply(world, conversation)
    _, other_reply = await _turn_reply(world, conversation, ("answer",))
    await record_turn_post(  # another agent's post under the same turn stays
        world.sessionmaker,
        tenant_id=world.tenant_id,
        platform="teams",
        ma_agent_id=_OTHER_AGENT,
        channel_id=conversation,
        message_id="act-other-agent",
        requester_platform_user_id=_CALLER,
        source="turn",
        turn_card_intent_id=intent,
    )

    result = await _teams_delete_reply_impl(
        world.runtime,
        auth,
        channel_id=conversation,
        message_id=parts[1],
        origin_context_id=origin,
    )

    assert (result.action, result.messages_deleted) == ("deleted", len(parts)), (
        "every part of the reply is reported deleted"
    )
    assert _deleted(world) == parts[::-1], "each part is deleted, newest first, and only those"
    assert other_reply[0] not in _deleted(world), "another reply of the agent stays"
    rows = [r for r in await world.audit() if r.tool_name == "delete_reply"]
    assert sorted(r.target_message_id or "" for r in rows if r.outcome == "allowed") == sorted(
        parts
    ), "each deletion is audited on its own"


@pytest.mark.parametrize("running", [True, False])
async def test_a_refused_reply_deletes_nothing(
    committing_sessionmaker: async_sessionmaker[AsyncSession], running: bool
) -> None:
    """A running turn, or another person's, is refused before the first deletion."""
    world = await _world(committing_sessionmaker)
    auth, origin = await world.turn()
    _, parts = await _turn_reply(
        world, _CHAT, running=running, requester=_CALLER if running else "someone-else"
    )

    with pytest.raises(ToolError, match="still running" if running else "only the person"):
        await _teams_delete_reply_impl(
            world.runtime, auth, channel_id=_CHAT, message_id=parts[-1], origin_context_id=origin
        )
    assert world.teams.edits_and_deletes() == [], "no part of a refused reply is deleted"


async def test_a_limit_hit_mid_reply_says_how_many_went_and_how_many_are_left(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(committing_sessionmaker)
    auth, origin = await world.turn()
    _, parts = await _turn_reply(world, _CHAT, ("answer",) * (PER_TURN_LIMIT + 2))

    with pytest.raises(ToolError, match=f"{PER_TURN_LIMIT} of {len(parts)} .* 2 left") as err:
        await _teams_delete_reply_impl(
            world.runtime, auth, channel_id=_CHAT, message_id=parts[0], origin_context_id=origin
        )
    assert "tidy limit" in str(err.value), "the stop names the limit"
    assert len(_deleted(world)) == PER_TURN_LIMIT, "deletion stops at the limit"


async def test_a_part_already_gone_is_skipped(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(committing_sessionmaker)
    auth, origin = await world.turn()
    _, parts = await _turn_reply(world, _CHAT)
    world.teams.gone.add(parts[2])

    result = await _teams_delete_reply_impl(
        world.runtime, auth, channel_id=_CHAT, message_id=parts[0], origin_context_id=origin
    )

    assert result.messages_deleted == len(parts) - 1, "the gone part is not counted"
    assert _deleted(world) == parts[::-1], "the rest are still deleted"
    async with world.sessionmaker() as s:
        row = await get_post(
            s, tenant_id=world.tenant_id, platform="teams", channel_id=_CHAT, message_id=parts[2]
        )
    assert row is None, "the gone part's row is marked deleted, so it is never listed again"


async def test_a_routine_result_is_one_reply_across_its_chunks_threads(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Chunks posted to a channel each root a thread; their delivery groups them."""
    world = await _world(committing_sessionmaker)
    auth, origin = await world.turn()
    delivery, other = uuid.uuid4(), uuid.uuid4()
    chunks = {"act-chunk-1": delivery, "act-chunk-2": delivery, "act-next-run": other}
    for activity, group in chunks.items():
        await record_turn_post(
            world.sessionmaker,
            tenant_id=world.tenant_id,
            platform="teams",
            ma_agent_id=_AGENT,
            channel_id=f"{_CHANNEL};messageid={activity}",
            message_id=activity,
            requester_platform_user_id=_CALLER,
            source="routine",
            parent_channel_id=_CHANNEL,
            role="routine",
            delivery_id=group,
        )

    result = await _teams_delete_reply_impl(
        world.runtime, auth, channel_id=_CHANNEL, message_id="act-chunk-1", origin_context_id=origin
    )

    assert result.messages_deleted == 2, "both chunks of the delivery are deleted"
    assert world.teams.edits_and_deletes() == [
        ("DELETE", f"/{_CHANNEL};messageid=act-chunk-2/activities/act-chunk-2"),
        ("DELETE", f"/{_CHANNEL};messageid=act-chunk-1/activities/act-chunk-1"),
    ], "each chunk is deleted in its own thread; the next run's result stays"


async def test_a_routine_part_in_a_protected_channel_stops_the_reply_before_any_delete(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Every part's place is checked first: one chunk's channel gone protected spares them all."""
    world = await _world(committing_sessionmaker)
    auth, origin = await world.turn()
    delivery, other_channel = uuid.uuid4(), "19:other@thread.tacv2"
    for activity, channel in (("act-chunk-1", _CHANNEL), ("act-chunk-2", other_channel)):
        await record_turn_post(
            world.sessionmaker,
            tenant_id=world.tenant_id,
            platform="teams",
            ma_agent_id=_AGENT,
            channel_id=f"{channel};messageid={activity}",
            message_id=activity,
            requester_platform_user_id=_CALLER,
            source="routine",
            parent_channel_id=channel,
            role="routine",
            delivery_id=delivery,
        )
    async with committing_sessionmaker.begin() as s:
        await set_access_policy(
            s,
            tenant_id=world.tenant_id,
            policy=TenantAccessPolicy(protected_channel_ids=(other_channel,)),
        )

    with pytest.raises(ToolError, match="writers to none"):
        await _teams_delete_reply_impl(
            world.runtime,
            auth,
            channel_id=_CHANNEL,
            message_id="act-chunk-1",
            origin_context_id=origin,
        )

    assert world.teams.edits_and_deletes() == [], "the unprotected chunk is not deleted either"


async def test_a_send_message_post_is_a_reply_of_one_message(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(committing_sessionmaker)
    auth, origin = await world.turn()
    first, second = await world.post(auth), await world.post(auth, "second")

    result = await _teams_delete_reply_impl(
        world.runtime, auth, channel_id=_THREAD, message_id=first, origin_context_id=origin
    )

    assert result.messages_deleted == 1, "a tool post is its own reply"
    assert _deleted(world) == [first], f"{second} stays"
