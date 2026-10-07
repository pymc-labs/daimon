"""Discord tidy tools on a turn's own posts: replies, status cards and the auto-opened thread.

Carlos's scenario: a mention opens a thread, turns post status cards (some
left empty) and replies, then "tidy this thread". The rows are written the
way the Discord adapter writes them (`record_turn_post`), with the turn's
card intent retired or still running, and tidied through the tidy impls
against the same faked Discord REST surface as the #383 tests.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from daimon.adapters.mcp.tools.discord._tidy import (
    _archive_thread_impl,  # pyright: ignore[reportPrivateUsage]
    _delete_message_impl,  # pyright: ignore[reportPrivateUsage]
    _delete_thread_impl,  # pyright: ignore[reportPrivateUsage]
    _edit_message_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.channel_tidy import record_turn_post
from daimon.core.stores.agent_posts import get_post
from daimon.core.stores.turn_card_intents import (
    create_turn_card_intent,
    record_turn_card_message,
    retire_turn_card_intent,
)
from fastmcp.exceptions import ToolError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_base_path = Path(__file__).parent / "test_channel_tidy_discord.py"
_spec = importlib.util.spec_from_file_location("_tidy_discord_base", _base_path)
assert _spec is not None and _spec.loader is not None
_base = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _base  # its dataclasses resolve their module through sys.modules
_spec.loader.exec_module(_base)

_FakeDiscord: Any = _base._FakeDiscord  # pyright: ignore[reportPrivateUsage]
_World: Any = _base._World  # pyright: ignore[reportPrivateUsage]
_world = _base._world  # pyright: ignore[reportPrivateUsage]
patch_discord_http = _base.patch_discord_http
_CHANNEL: str = _base._CHANNEL  # pyright: ignore[reportPrivateUsage]
_THREAD: str = _base._THREAD  # pyright: ignore[reportPrivateUsage]
_CALLER: str = _base._CALLER  # pyright: ignore[reportPrivateUsage]
_AGENT: str = _base._AGENT  # pyright: ignore[reportPrivateUsage]
_OTHER_AGENT: str = _base._OTHER_AGENT  # pyright: ignore[reportPrivateUsage]
_ALL_PERMS: int = _base._ALL_PERMS  # pyright: ignore[reportPrivateUsage]

_SOMEONE_ELSE = "43"
_ADMINISTRATOR = 1 << 3
_VIEW_CHANNEL = 1 << 10

_CARD_EMBED = {"type": "rich", "description": "Thinking…"}


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Any:
    state = _FakeDiscord()
    patch_discord_http(monkeypatch, state.handle)
    return state


async def _intent(world: Any, *, running: bool, message_id: str) -> uuid.UUID:
    """A turn's card intent, still running or already retired."""
    async with world.sessionmaker.begin() as s:
        intent = await create_turn_card_intent(
            s,
            tenant_id=world.tenant_id,
            platform="discord",
            thread_id=_THREAD,
            turn_token=uuid.uuid4(),
        )
        await record_turn_card_message(s, id=intent.id, message_id=message_id)
        if not running:
            await retire_turn_card_intent(s, id=intent.id, expected_message_id=message_id)
    return intent.id


async def _auto_thread(
    world: Any,
    *,
    agent: str = _AGENT,
    opener: str = _CALLER,
) -> None:
    await record_turn_post(
        world.sessionmaker,
        tenant_id=world.tenant_id,
        platform="discord",
        ma_agent_id=agent,
        channel_id=_CHANNEL,
        message_id=_THREAD,
        requester_platform_user_id=opener,
        source="auto_thread",
    )


async def _turn_post(
    world: Any,
    fake: Any,
    *,
    agent: str = _AGENT,
    requester: str = _CALLER,
    running: bool = False,
    card: bool = False,
    content: str = "an answer",
    channel_id: str = _THREAD,
) -> str:
    """A message a turn posted, recorded the way the Discord adapter records it."""
    message_id = fake.add(channel_id, content="" if card else content)
    if card:
        fake.messages[message_id]["embeds"] = [_CARD_EMBED]
    intent_id = await _intent(world, running=running, message_id=message_id)
    await record_turn_post(
        world.sessionmaker,
        tenant_id=world.tenant_id,
        platform="discord",
        ma_agent_id=agent,
        channel_id=channel_id,
        message_id=message_id,
        requester_platform_user_id=requester,
        source="turn",
        turn_card_intent_id=intent_id,
        parent_channel_id=_CHANNEL if channel_id == _THREAD else None,
    )
    return message_id


# ---------------------------------------------------------------------------
# What a turn posted is the agent's to tidy
# ---------------------------------------------------------------------------


async def test_an_empty_status_card_from_a_finished_turn_is_deleted(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    world = await _world(committing_sessionmaker)
    await _auto_thread(world)
    card = await _turn_post(world, fake, card=True)
    auth, origin = await world.turn(thread=_THREAD)

    result = await _delete_message_impl(
        world.runtime, auth, channel_id=_THREAD, message_id=card, origin_context_id=origin
    )

    assert result.action == "deleted", "the agent may delete its own empty status card"
    assert card not in fake.messages, "discord deleted the card"
    rows = [r for r in await world.audit() if r.target_message_id == card]
    assert [(r.tool_name, r.outcome) for r in rows] == [("delete_message", "allowed")], (
        "the delete is audited like any other tidy action"
    )


async def test_editing_a_turn_reply_replaces_the_text_and_clears_the_card(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    world = await _world(committing_sessionmaker)
    await _auto_thread(world)
    card = await _turn_post(world, fake, card=True)
    auth, origin = await world.turn(thread=_THREAD)

    await _edit_message_impl(
        world.runtime,
        auth,
        channel_id=_THREAD,
        message_id=card,
        content="Summary: all items closed.",
        origin_context_id=origin,
    )

    assert fake.messages[card]["content"] == "Summary: all items closed.", "the new text is set"
    assert fake.messages[card]["embeds"] == [], "the stale card embed does not linger under it"


async def test_the_person_who_opened_the_thread_may_tidy_replies_given_to_others_in_it(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    world = await _world(committing_sessionmaker)
    await _auto_thread(world, opener=_CALLER)
    reply = await _turn_post(world, fake, requester=_SOMEONE_ELSE)
    auth, origin = await world.turn(thread=_THREAD)

    await _delete_message_impl(
        world.runtime, auth, channel_id=_THREAD, message_id=reply, origin_context_id=origin
    )
    assert reply not in fake.messages, "the conversation's opener speaks for the thread"


async def test_a_server_admin_may_tidy_a_turn_they_did_not_start(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    world = await _world(committing_sessionmaker)
    fake.everyone_perms = _ALL_PERMS | _ADMINISTRATOR
    await _auto_thread(world, opener=_SOMEONE_ELSE)
    reply = await _turn_post(world, fake, requester=_SOMEONE_ELSE)
    auth, origin = await world.turn(thread=_THREAD)

    await _delete_message_impl(
        world.runtime, auth, channel_id=_THREAD, message_id=reply, origin_context_id=origin
    )
    assert reply not in fake.messages, "an admin may have any of the agent's replies tidied"


async def test_the_opener_archives_the_thread_opened_from_their_mention(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    world = await _world(committing_sessionmaker)
    await _auto_thread(world, opener=_CALLER)
    auth, origin = await world.turn(thread=_THREAD)

    result = await _archive_thread_impl(
        world.runtime, auth, thread_id=_THREAD, origin_context_id=origin
    )
    assert result.action == "archived", "the opener may archive their conversation"
    assert fake.thread_archived, "discord archived the thread"


async def test_an_admin_archives_an_auto_opened_thread_someone_else_opened(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    world = await _world(committing_sessionmaker)
    fake.everyone_perms = _ALL_PERMS | _ADMINISTRATOR
    await _auto_thread(world, opener=_SOMEONE_ELSE)
    auth, origin = await world.turn(thread=_THREAD)

    await _archive_thread_impl(world.runtime, auth, thread_id=_THREAD, origin_context_id=origin)
    assert fake.thread_archived, "a server admin may archive it"


async def test_tidy_this_thread_removes_the_agents_settled_posts_and_nothing_else(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    """Carlos's thread: two empty cards, two answers, his messages, and the running turn."""
    world = await _world(committing_sessionmaker)
    await _auto_thread(world, opener=_CALLER)
    empty_card_1 = await _turn_post(world, fake, card=True)
    answer = await _turn_post(world, fake, content="Outstanding items: …")
    empty_card_2 = await _turn_post(world, fake, card=True)
    human = fake.add(_THREAD, author_id=_CALLER, bot=False, content="tidy this thread @Daimon")
    other_agent = await _turn_post(world, fake, agent=_OTHER_AGENT, content="other agent")
    running_card = await _turn_post(world, fake, card=True, running=True)
    auth, origin = await world.turn(thread=_THREAD)

    result = await _delete_thread_impl(
        world.runtime, auth, thread_id=_THREAD, origin_context_id=origin
    )

    assert result.messages_deleted == 3, "both empty cards and the old answer go"
    for gone in (empty_card_1, answer, empty_card_2):
        assert gone not in fake.messages, "the agent's settled posts are deleted"
    assert human in fake.messages, "the person's message stays"
    assert other_agent in fake.messages, "another agent's reply stays"
    assert running_card in fake.messages, "the running turn's own card stays"
    assert not fake.thread_deleted, "the thread itself is never deleted"


# ---------------------------------------------------------------------------
# Required refusals
# ---------------------------------------------------------------------------


async def test_another_agents_turn_reply_and_card_are_refused(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    world = await _world(committing_sessionmaker)
    await _auto_thread(world, agent=_OTHER_AGENT)
    theirs = await _turn_post(world, fake, agent=_OTHER_AGENT, card=True)
    auth, origin = await world.turn(thread=_THREAD)

    with pytest.raises(ToolError, match="another agent posted this message"):
        await _delete_message_impl(
            world.runtime, auth, channel_id=_THREAD, message_id=theirs, origin_context_id=origin
        )
    with pytest.raises(ToolError, match="another agent posted this message"):
        await _edit_message_impl(
            world.runtime,
            auth,
            channel_id=_THREAD,
            message_id=theirs,
            content="rewritten",
            origin_context_id=origin,
        )
    assert theirs in fake.messages, "the other agent's card is untouched"


async def test_a_humans_message_in_the_auto_thread_is_refused(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    world = await _world(committing_sessionmaker)
    await _auto_thread(world)
    human = fake.add(_THREAD, author_id=_CALLER, bot=False, content="tidy this thread @Daimon")
    auth, origin = await world.turn(thread=_THREAD)

    with pytest.raises(ToolError, match="not posted by you"):
        await _delete_message_impl(
            world.runtime, auth, channel_id=_THREAD, message_id=human, origin_context_id=origin
        )
    assert human in fake.messages, "even the opener's own message is not the agent's"


async def test_a_thread_another_agent_opened_from_a_mention_is_refused(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    world = await _world(committing_sessionmaker)
    await _auto_thread(world, agent=_OTHER_AGENT)
    auth, origin = await world.turn(thread=_THREAD)

    with pytest.raises(ToolError, match="another agent posted this message"):
        await _archive_thread_impl(world.runtime, auth, thread_id=_THREAD, origin_context_id=origin)
    with pytest.raises(ToolError, match="another agent posted this message"):
        await _delete_thread_impl(world.runtime, auth, thread_id=_THREAD, origin_context_id=origin)
    assert not fake.thread_archived, "the other agent's thread stays open"


async def test_a_thread_nobody_recorded_opening_is_refused(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    """A bot-owned thread with no auto_thread row, like threads from before this change."""
    world = await _world(committing_sessionmaker)
    auth, origin = await world.turn(thread=_THREAD)

    with pytest.raises(ToolError, match="not posted by you"):
        await _archive_thread_impl(world.runtime, auth, thread_id=_THREAD, origin_context_id=origin)
    assert not fake.thread_archived, "an unrecorded thread is not the agent's"


async def test_a_post_in_a_channel_the_requester_cannot_see_is_refused(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    world = await _world(committing_sessionmaker)
    fake.everyone_perms = _ALL_PERMS & ~_VIEW_CHANNEL
    await _auto_thread(world)
    reply = await _turn_post(world, fake)
    auth, origin = await world.turn(thread=_THREAD)

    with pytest.raises(ToolError, match="missing view_channel permission"):
        await _delete_message_impl(
            world.runtime, auth, channel_id=_THREAD, message_id=reply, origin_context_id=origin
        )
    with pytest.raises(ToolError, match="missing view_channel permission"):
        await _archive_thread_impl(world.runtime, auth, thread_id=_THREAD, origin_context_id=origin)
    assert reply in fake.messages, "the reply is untouched"
    assert not fake.thread_archived, "the thread is untouched"


# ---------------------------------------------------------------------------
# Conversation rules
# ---------------------------------------------------------------------------


async def test_a_running_turns_card_is_refused(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    world = await _world(committing_sessionmaker)
    await _auto_thread(world)
    card = await _turn_post(world, fake, card=True, running=True)
    auth, origin = await world.turn(thread=_THREAD)

    with pytest.raises(ToolError, match="still running"):
        await _delete_message_impl(
            world.runtime, auth, channel_id=_THREAD, message_id=card, origin_context_id=origin
        )
    assert card in fake.messages, "the live card stays for its turn to finish"
    rows = [r for r in await world.audit() if r.target_message_id == card]
    assert {(r.outcome, r.reason) for r in rows} == {("denied", "turn_in_progress")}, (
        "the refusal is audited"
    )


async def test_someone_who_did_not_start_the_conversation_cannot_have_it_tidied(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    world = await _world(committing_sessionmaker)
    await _auto_thread(world, opener=_SOMEONE_ELSE)
    reply = await _turn_post(world, fake, requester=_SOMEONE_ELSE)
    auth, origin = await world.turn(thread=_THREAD)

    with pytest.raises(ToolError, match="only the person who started this conversation"):
        await _delete_message_impl(
            world.runtime, auth, channel_id=_THREAD, message_id=reply, origin_context_id=origin
        )
    with pytest.raises(ToolError, match="only the person who started this conversation"):
        await _archive_thread_impl(world.runtime, auth, thread_id=_THREAD, origin_context_id=origin)
    with pytest.raises(ToolError, match="only the person who started this conversation"):
        await _delete_thread_impl(world.runtime, auth, thread_id=_THREAD, origin_context_id=origin)
    assert reply in fake.messages, "someone else's answer stays"
    assert not fake.thread_archived, "and so does their thread"


async def test_a_turn_reply_outside_an_auto_thread_needs_its_own_requester(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    """A turn in a thread the agent opened with create_thread has no opener to defer to."""
    world = await _world(committing_sessionmaker)
    mine = await _turn_post(world, fake, requester=_CALLER, channel_id=_CHANNEL)
    theirs = await _turn_post(world, fake, requester=_SOMEONE_ELSE, channel_id=_CHANNEL)
    auth, origin = await world.turn()

    await _delete_message_impl(
        world.runtime, auth, channel_id=_CHANNEL, message_id=mine, origin_context_id=origin
    )
    with pytest.raises(ToolError, match="only the person who started this conversation"):
        await _delete_message_impl(
            world.runtime, auth, channel_id=_CHANNEL, message_id=theirs, origin_context_id=origin
        )
    assert mine not in fake.messages and theirs in fake.messages, "only the caller's own turn"


async def test_a_turn_row_names_the_turns_agent_and_requester(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    world = await _world(committing_sessionmaker)
    reply = await _turn_post(world, fake)
    auth, _ = await world.turn(thread=_THREAD)

    async with committing_sessionmaker() as s:
        post = await get_post(
            s, tenant_id=world.tenant_id, platform="discord", channel_id=_THREAD, message_id=reply
        )
    assert post is not None, "the turn's reply is recorded"
    assert post.agent_id == auth.chat_agent_id, "keyed by the agent the turn's MCP token carries"
    assert (post.source, post.kind, post.requester_platform_user_id) == ("turn", "message", _CALLER)
    assert post.content_hmac is None, "a turn row keeps no content hash"


async def test_an_edit_of_someone_elses_turn_is_refused(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    world = await _world(committing_sessionmaker)
    await _auto_thread(world, opener=_SOMEONE_ELSE)
    reply = await _turn_post(world, fake, requester=_SOMEONE_ELSE, content="their answer")
    auth, origin = await world.turn(thread=_THREAD)

    with pytest.raises(ToolError, match="only the person who started this conversation"):
        await _edit_message_impl(
            world.runtime,
            auth,
            channel_id=_THREAD,
            message_id=reply,
            content="rewritten",
            origin_context_id=origin,
        )
    assert fake.messages[reply]["content"] == "their answer", "the answer is unedited"
    rows = [r for r in await world.audit() if r.target_message_id == reply]
    assert {(r.outcome, r.reason) for r in rows} == {("denied", "not_conversation_owner")}


async def test_a_person_may_tidy_their_own_turn_in_someone_elses_thread(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    world = await _world(committing_sessionmaker)
    await _auto_thread(world, opener=_SOMEONE_ELSE)
    mine = await _turn_post(world, fake, requester=_CALLER)
    auth, origin = await world.turn(thread=_THREAD)

    await _delete_message_impl(
        world.runtime, auth, channel_id=_THREAD, message_id=mine, origin_context_id=origin
    )
    assert mine not in fake.messages, "the answer to the caller's own question is theirs to tidy"


async def test_opening_a_thread_with_one_agent_gives_no_say_over_another_agents_replies(
    committing_sessionmaker: async_sessionmaker[AsyncSession], fake: Any
) -> None:
    """The caller opened the thread with the other agent; this agent answered someone else."""
    world = await _world(committing_sessionmaker)
    await _auto_thread(world, agent=_OTHER_AGENT, opener=_CALLER)
    reply = await _turn_post(world, fake, requester=_SOMEONE_ELSE)
    auth, origin = await world.turn(thread=_THREAD)

    with pytest.raises(ToolError, match="only the person who started this conversation"):
        await _delete_message_impl(
            world.runtime, auth, channel_id=_THREAD, message_id=reply, origin_context_id=origin
        )
    assert reply in fake.messages, "the other person's answer stays"
