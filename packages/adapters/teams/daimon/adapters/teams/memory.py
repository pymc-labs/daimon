"""The `memory` command: what the agent where it was typed remembers.

`memory` lists the shared memory's paths; `memory <path>` shows one memory.
`memory mine`, `memory mine <path>` and `memory forget-mine [code]` act on the
sender's own personal memory only, never anyone else's
(`daimon.core.memory_view`). Mirrors Slack's
`/memory`, resolved for the place it was typed the way a turn there resolves
its agent: a channel post's thread, or the 1:1 chat. Typed in a channel it is
answered there for the sender only, as Slack does. When Teams refuses that, it
is answered in the 1:1 chat, and a channel whose readers are limited gets a
refusal instead: Slack's `/dm-me` likewise refuses to carry such a channel's work
outside it.
"""

from __future__ import annotations

import anthropic
import structlog
from daimon.adapters.teams.card import TEAMS_LIMIT
from daimon.adapters.teams.card_actions import clip, heading
from daimon.adapters.teams.commands import CommandContext
from daimon.adapters.teams.identity import TeamsInbound
from daimon.core.errors import DaimonError
from daimon.core.memory_view import (
    get_channel_memory_store,
    get_memory_content,
    get_own_personal_memory_store,
    held_place,
    list_memory_paths,
    parse_memory_args,
    personal_memory_reply,
    resolve_channel_agent,
)
from daimon.core.message_split import split_fenced
from daimon.core.mux_backend import resource_scope
from daimon.core.permissions import readers_limited_at
from daimon.core.stores.access_policy import load_access_policy
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.turn_hold import RequesterOrg
from microsoft_teams.cards import AdaptiveCard, CardElement, TextBlock

log = structlog.get_logger()

EMPTY = "No memory to show here."
KEPT_INSIDE = (
    "This channel's rules keep its agent's memory out of this chat.\n\n"
    "Ask the agent in that channel."
)
HELD_INSIDE = (
    "This channel is shared with another organisation, so its memory stays in the channel.\n\n"
    "Ask the agent in that channel."
)
_FAILED = "Couldn't load the memory.\n\nTry again in a minute."
_TITLE_MAX_CHARS = 300


def _truncated(text: str) -> str:
    return text if len(text) <= TEAMS_LIMIT else text[:TEAMS_LIMIT] + "\n… (truncated)"


def _card(title: str, text: str | None = None, hint: str | None = None) -> AdaptiveCard:
    title = clip(title, _TITLE_MAX_CHARS)  # A title can carry the path someone typed.
    body: list[CardElement] = [heading(title)]
    if text is not None:
        body.append(TextBlock(text=_truncated(text), font_type="Monospace", wrap=True))
    if hint is not None:
        body.append(TextBlock(text=hint, is_subtle=True, spacing="Medium", wrap=True))
    return AdaptiveCard(body=body, fallback_text=title)


def _requester_org(asked: TeamsInbound) -> RequesterOrg:
    """Teams keeps its existing behaviour for now: the asker is passed as a member.

    Teams turns are not held yet, so /memory decides a held place from what was
    recorded (a foreign principal or a shared channel), as admission does.
    """
    del asked
    return RequesterOrg.HOST


def _channel_shared(asked: TeamsInbound) -> bool | None:
    """No live signal: only the shared-channel registry holds a Teams place for now."""
    del asked
    return None


async def _place_is_held(context: CommandContext, asked: TeamsInbound) -> bool:
    async with context.runtime.sessionmaker() as session:
        principal = await get_or_create_platform_principal(
            session, tenant_id=context.tenant_id, platform="teams", external_id=asked.user_id
        )
        return (
            await held_place(
                session,
                tenant_id=context.tenant_id,
                platform="teams",
                channel_id=asked.channel_id,
                foreign_home_id=principal.foreign_home_id,
                requester_org=_requester_org(asked),
                channel_shared=_channel_shared(asked),
            )
            is not None
        )


async def _kept_inside(context: CommandContext, asked: TeamsInbound) -> str | None:
    """Why the answer must not be carried to the 1:1 chat, None when it may be.

    A limited-readers channel's memory stays in it, and so does a held channel's,
    host members included: it is shared with people from another organisation.
    A private answer in the channel itself (`asked_in` None) stays inside it."""
    if context.asked_in is None:
        return None
    async with context.runtime.sessionmaker() as session:
        policy = await load_access_policy(session, tenant_id=context.tenant_id)
    if readers_limited_at(policy, channel_id=asked.channel_id, thread_id=asked.thread_id):
        return KEPT_INSIDE
    return HELD_INSIDE if await _place_is_held(context, asked) else None


def _list_cards(title: str, paths: tuple[str, ...], hint: str | None) -> list[AdaptiveCard]:
    """Every path, over as many cards as it takes, with the hint on the last.

    A `forget-mine` preview's hint carries the code that deletes exactly the
    listed files, so the list is never truncated: each card stays under the
    limit instead.
    """
    chunks = split_fenced("\n".join(paths), TEAMS_LIMIT)
    return [
        _card(
            title if n == 0 else f"{title} (continued)",
            chunk,
            hint if n == len(chunks) - 1 else None,
        )
        for n, chunk in enumerate(chunks)
    ]


async def _personal_cards(context: CommandContext, asked: TeamsInbound) -> list[AdaptiveCard]:
    """The sender's own personal memory: resolved from their account, never an argument."""
    agent = await resolve_channel_agent(
        context.runtime.sessionmaker,
        context.runtime.anthropic,
        tenant_id=context.tenant_id,
        platform="teams",
        user_id=asked.user_id,
        channel_id=asked.channel_id,
        default=context.runtime.deployment_default,
        requester_org=_requester_org(asked),
        channel_shared=_channel_shared(asked),
        thread_id=asked.thread_id if asked.thread_id != asked.channel_id else None,
    )
    if agent is None:
        return [_card(EMPTY)]
    reply = await personal_memory_reply(
        context.runtime.anthropic,
        agent_name=agent.agent_name,
        store_id=await get_own_personal_memory_store(context.runtime.sessionmaker, agent),
        request=parse_memory_args(context.args),
        command="memory",
        scope=resource_scope(tenant_id=str(agent.tenant_id)),
        held=agent.hold is not None,
    )
    if reply.content is not None:
        return [_card(reply.title, reply.content)]
    if not reply.paths:
        return [_card(reply.title, None, reply.hint)]
    return _list_cards(reply.title, reply.paths, reply.hint)


async def _memory_card(context: CommandContext) -> AdaptiveCard | list[AdaptiveCard]:
    runtime, path = context.runtime, context.args
    asked = context.asked_in or context.inbound
    if (kept := await _kept_inside(context, asked)) is not None:
        return _card(kept)
    if parse_memory_args(path).kind not in ("shared_list", "shared_show"):
        return await _personal_cards(context, asked)
    resolved = await get_channel_memory_store(
        runtime.sessionmaker,
        runtime.anthropic,
        tenant_id=context.tenant_id,
        platform="teams",
        user_id=asked.user_id,
        channel_id=asked.channel_id,
        default=runtime.deployment_default,
        requester_org=_requester_org(asked),
        channel_shared=_channel_shared(asked),
        # A plain 1:1 chat is its own thread; only a post or a setup conversation is one.
        thread_id=asked.thread_id if asked.thread_id != asked.channel_id else None,
    )
    if resolved is None:
        return _card(EMPTY)
    agent_name, store_id = resolved
    if not path:
        paths = await list_memory_paths(runtime.anthropic, store_id)
        if not paths:
            return _card(EMPTY)
        title = f"{agent_name}'s memory ({len(paths)} files)"
        return _card(title, "\n".join(paths), "Send memory <path> to read one.")
    content = await get_memory_content(runtime.anthropic, store_id, path)
    if content is None:
        return _card(f"No memory file called {path}.", hint="Send memory to see them all.")
    return _card(path, content)


async def show_memory(context: CommandContext) -> None:
    try:
        cards = await _memory_card(context)
    except (DaimonError, anthropic.APIError) as exc:
        log.warning("teams.memory.failed", exc_info=exc)
        cards = _card(str(exc) if isinstance(exc, DaimonError) else _FAILED)
    for card in cards if isinstance(cards, list) else [cards]:
        await context.send_card(card)
