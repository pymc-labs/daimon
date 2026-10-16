"""Replies only their sender sees in a channel or group chat: commands and notices.

Teams' targeted messages show in the thread to one member. Teams refuses one
it cannot deliver (400, 403 `BotNotInConversationRoster`, 404), so every
caller keeps a fallback: a command moves to the sender's 1:1 chat, a notice
posts in the thread as before. A timed-out send gets no fallback: it may
have landed. Commands whose flows live in the 1:1 chat (`setup`'s setup
conversations, `support`'s request) are answered there, as are messages read
from Graph, which carry no Bot Framework id to address. In a group chat,
whose one conversation `new` restarts, `new` is answered privately too.
A command that changes something before it answers (`memory forget-mine
<code>`) is never run twice: a refused answer gets a short notice instead.
"""

from __future__ import annotations

import dataclasses
import uuid
import warnings
from collections.abc import Awaitable, Callable, Mapping

import httpx
import structlog
from daimon.adapters.teams.commands import (
    ANSWERED_IN_CHAT,
    CHANNEL_POINTER,
    NEW_IN_CHANNEL,
    CommandContext,
    CommandHandler,
    parse_command,
)
from daimon.adapters.teams.direct_chats import DirectChats
from daimon.adapters.teams.identity import TeamsInbound
from daimon.adapters.teams.lifecycle import TEAMS_SEND_ERRORS, TeamsSender
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.adapters.teams.setup_conversation import route_to_setup
from daimon.core.memory_view import parse_memory_args
from daimon.core.turn.errors import AdmissionDenialReason
from microsoft_teams.api import Account, MessageActivity, MessageActivityInput, SentActivity
from microsoft_teams.apps import ActivityContext
from microsoft_teams.common import ExperimentalWarning

log = structlog.get_logger()

# Generally available in Teams; the SDK still warns on every targeted call.
warnings.filterwarnings(
    "ignore", message=".*ExperimentalTeamsTargeted", category=ExperimentalWarning
)

# Commands answered privately in the thread; the rest keep the 1:1 chat.
IN_THREAD = frozenset({"help", "here", "memory", "routines", "billing", "privacy"})
NOT_YET_PRIVATE = (
    "Private questions aren't supported yet. Mention {bot} here to ask openly, "
    "or ask me in our 1:1 chat."
)
UNSHOWN = "I ran `{name}`, but couldn't show you its result privately here."
# Admission refusals about the sender, not the thread: only they see them.
SENDER_ONLY: frozenset[AdmissionDenialReason] = frozenset(
    {"invoker_not_allowed", "external_participant", "cap_exceeded"}
)
_TIMEOUTS = (TimeoutError, httpx.TimeoutException)


class TargetedRefused(Exception):
    """Teams refused a command's first private reply: answer it in the 1:1 chat."""


def targeted(
    activity: MessageActivityInput, recipient: Account, *, prompt_id: str | None = None
) -> MessageActivityInput:
    """`activity` for `recipient` only, previewing their targeted prompt `prompt_id`."""
    activity.with_recipient(recipient, is_targeted=True)
    if prompt_id is not None and activity.id is None:
        activity.add_targeted_message_info(prompt_id)
    return activity


def _recipient(inbound: TeamsInbound) -> Account | None:
    if inbound.kind == "dm" or inbound.sender_id is None:
        return None
    return Account(id=inbound.sender_id, aad_object_id=inbound.user_id, name=inbound.user_name)


def _refused(event: str, exc: Exception) -> None:
    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
    log.info(event, reason=type(exc).__name__, status=status)


@dataclasses.dataclass
class _PrivateSend:
    """`CommandContext.send` into the thread for the sender only."""

    sender: TeamsSender
    inbound: TeamsInbound
    recipient: Account
    landed: bool = False

    async def __call__(self, activity: MessageActivityInput) -> SentActivity:
        inbound = self.inbound
        prompt = inbound.activity_id if inbound.is_targeted else None
        try:
            sent = await self.sender.send(
                inbound.conversation_id,
                targeted(activity, self.recipient, prompt_id=prompt),
                service_url=inbound.service_url,
            )
        except _TIMEOUTS:
            raise
        except TEAMS_SEND_ERRORS as exc:
            if self.landed:
                raise  # part of the answer is shown: no second copy elsewhere
            _refused("teams.command.targeted_refused", exc)
            raise TargetedRefused from exc
        self.landed = True
        return sent


def reply_privately(ctx: ActivityContext[MessageActivity]) -> Callable[[str], Awaitable[object]]:
    """A refusal's reply: in a channel or group chat for the sender only, else `ctx.reply`."""
    activity = ctx.activity

    async def reply(text: str) -> object:
        if activity.conversation.conversation_type not in ("channel", "groupChat"):
            return await ctx.reply(text)
        prompt = activity.id if activity.recipient.is_targeted else None
        try:
            return await ctx.send(
                targeted(MessageActivityInput(text=text), activity.from_, prompt_id=prompt)
            )
        except _TIMEOUTS:
            raise
        except TEAMS_SEND_ERRORS as exc:
            _refused("teams.refusal.targeted_refused", exc)
            # A plain reply to a targeted message is targeted by the SDK: name the sender openly.
            public = MessageActivityInput(text=text).with_recipient(activity.from_)
            return await ctx.reply(public)

    return reply


class PrivateReplies:
    """Commands and sender-only notices, answered where only the sender sees them."""

    def __init__(
        self,
        *,
        runtime: TeamsRuntime,
        sender: TeamsSender,
        commands: Mapping[str, CommandHandler],
        direct: DirectChats | None,
        is_admin: Callable[[TeamsInbound], Awaitable[bool]],
    ) -> None:
        self._runtime = runtime
        self._sender = sender
        self._commands = commands
        # Opens 1:1 chats for commands sent in a channel; None points the sender there.
        self._direct = direct
        self._is_admin = is_admin

    def command(self, inbound: TeamsInbound) -> tuple[str, str] | None:
        # Someone from another organisation gets no command: the agent hears their words,
        # and a command's 1:1 chat cannot be opened across organisations.
        return None if inbound.is_external else parse_command(inbound.text, self._commands)

    async def tell_sender(self, inbound: TeamsInbound, text: str) -> None:
        """`text` for `inbound`'s sender only; where Teams refuses that, posted as before."""
        recipient = _recipient(inbound)
        if recipient is not None:
            prompt = inbound.activity_id if inbound.is_targeted else None
            activity = targeted(MessageActivityInput(text=text), recipient, prompt_id=prompt)
            try:
                await self._sender.send(
                    inbound.conversation_id, activity, service_url=inbound.service_url
                )
                return
            except _TIMEOUTS:
                raise
            except TEAMS_SEND_ERRORS as exc:
                _refused("teams.notice.targeted_refused", exc)
        await self._sender.send(
            inbound.conversation_id,
            MessageActivityInput(text=text),
            service_url=inbound.service_url,
        )

    async def run(
        self, inbound: TeamsInbound, tenant_id: uuid.UUID, command: tuple[str, str] | None
    ) -> None:
        """Run `command`: in a 1:1 chat there, in a channel privately or in the 1:1 chat.

        None answers a targeted question, which no turn takes yet: it would need a
        session of its own, apart from the thread's.
        """
        if command is None:
            bot = inbound.bot_name or "daimon"
            await self.tell_sender(inbound, NOT_YET_PRIVATE.format(bot=bot))
            return
        name, args = command
        if inbound.kind == "dm":
            routed = await route_to_setup(self._runtime.sessionmaker, inbound, tenant_id)
            await self._invoke(name, args, routed, tenant_id, send=self._public(routed))
            return
        if name == "new" and inbound.kind == "channel":
            await self.tell_sender(inbound, NEW_IN_CHANNEL)
            return
        recipient = _recipient(inbound)
        private = IN_THREAD | {"new"} if inbound.kind == "group" else IN_THREAD
        if name in private and recipient is not None:
            try:
                send = _PrivateSend(self._sender, inbound, recipient)
                await self._invoke(name, args, inbound, tenant_id, send=send)
                return
            except TargetedRefused:
                if name == "new":  # it restarts this chat, so it is answered here
                    await self._invoke(name, args, inbound, tenant_id, send=self._public(inbound))
                    return
                if name == "memory" and parse_memory_args(args).kind == "forget_confirm":
                    # It already deleted: a second run would find nothing and say so.
                    await self.tell_sender(inbound, UNSHOWN.format(name=f"{name} {args}"))
                    return
                # Otherwise nothing shown yet: answered in full in the 1:1 chat.
        chat = await self._direct_chat(inbound)
        pointer = ANSWERED_IN_CHAT if chat else CHANNEL_POINTER
        await self.tell_sender(inbound, pointer.format(name=name))
        if chat is None:
            return
        routed = await route_to_setup(self._runtime.sessionmaker, chat, tenant_id)
        await self._invoke(name, args, routed, tenant_id, send=self._public(routed), asked=inbound)

    def _public(
        self, inbound: TeamsInbound
    ) -> Callable[[MessageActivityInput], Awaitable[SentActivity]]:
        async def send(activity: MessageActivityInput) -> SentActivity:
            return await self._sender.send(
                inbound.conversation_id, activity, service_url=inbound.service_url
            )

        return send

    async def _invoke(
        self,
        name: str,
        args: str,
        inbound: TeamsInbound,
        tenant_id: uuid.UUID,
        *,
        send: Callable[[MessageActivityInput], Awaitable[SentActivity]],
        asked: TeamsInbound | None = None,
    ) -> None:
        await self._commands[name](
            CommandContext(
                inbound=inbound,
                tenant_id=tenant_id,
                args=args,
                is_admin=await self._is_admin(inbound),
                runtime=self._runtime,
                send=send,
                asked_in=asked,
            )
        )

    async def _direct_chat(self, inbound: TeamsInbound) -> TeamsInbound | None:
        """`inbound` moved to its sender's 1:1 chat; None when Teams will not open one."""
        if self._direct is None:
            return None
        try:
            member = await self._direct.member(inbound.channel_id, inbound.user_id)
            chat = await self._direct.open_chat(member) if member else None
        except TEAMS_SEND_ERRORS as exc:
            log.info("teams.command.no_direct_chat", reason=type(exc).__name__)
            return None
        if chat is None:
            return None
        return dataclasses.replace(
            inbound,
            kind="dm",
            conversation_id=chat,
            channel_id=chat,
            team_id=None,
            team_group_id=None,
            channel_name=None,
            channel_type=None,
            team_name=None,
            is_targeted=False,
        )
