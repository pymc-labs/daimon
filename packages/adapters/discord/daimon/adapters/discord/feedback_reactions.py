"""FeedbackReactionCog -- listens for the two vote emoji and records votes.

Discord routes `on_raw_reaction_add` through the gateway, not through the
interaction/dispatcher machinery `credential_button.py` and
`feedback_button.py` document -- but the same "adapter boundary" reasoning
applies: discord.py's event dispatcher (`Client._run_event`) catches and
logs anything a listener raises with `on_error`, which by default just
prints the traceback and moves on. Anything unhandled here vanishes with
nothing in OUR OWN structured logs, so `on_raw_reaction_add` wraps its whole
body in one broad catch-all -- the single outermost boundary in this
module. Nothing below it catches broadly.

This is a listener-only Cog, not a slash-command family, so it lives at the
adapter's top level rather than under `commands/`; `bot.py` is already well
past the size where new concerns belong inline. Local import inside
`setup_hook` avoids a cycle: this module imports `DaimonBot` at module
level, the same shape `credential_button.py` and `feedback_button.py` use.

Pipeline, cheapest and most certain gates first: (1) bot not connected yet
-- bail; (2) `vote_for_reaction`, one pure call covering the bot-self-
reaction, direct-message, custom-emoji, and emoji-match gates with zero
I/O; (3) `is_bot_authored`, tri-state -- `True`/`False` resolve
immediately, `None` means the gateway omitted the undocumented, possibly-
absent `message_author_id` field and this module falls back to one bounded
message fetch, memoized per message id (see `_AUTHOR_CACHE_MAX_ENTRIES`),
logging its use since that field could stop arriving without notice, where
a failure means the author is unverifiable and records NOTHING -- never
"assume ours"; (4) derive the tenant id (pure)
and open one transaction to read the tenant, resolve a best-effort
session/account attribution, and upsert the vote; (5) on a genuinely new
down-vote, open the private follow-up (`_send_feedback_prompt`).
"""

from __future__ import annotations

import uuid
from typing import cast

import structlog
from daimon.adapters.discord.bot import DaimonBot
from daimon.adapters.discord.feedback_button import FeedbackButton
from daimon.adapters.discord.support_escalation import SupportEscalateButton
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.message_feedback import (
    Vote,
    is_bot_authored,
    should_trigger_feedback_dm,
    vote_for_reaction,
)
from daimon.core.stores.identity import find_platform_principal
from daimon.core.stores.message_feedback import record_vote
from daimon.core.stores.support_escalation import count_escalations_for_user
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.thread_sessions import get_latest_thread_session
from daimon.core.support_escalation import has_credit, is_escalation_reaction, remaining_credits

import discord
from discord.ext import commands

log = structlog.get_logger()

# Cap on the per-process memo of resolved author verdicts. The fallback is
# rare today, but if `message_author_id` ever stops arriving then EVERY
# thumbs reaction bot-wide -- overwhelmingly on human-authored messages --
# costs a REST fetch competing with turn posting under discord.py's shared
# rate limiter, and one person cycling add/remove on a single message
# repeats it. Eviction is by insertion order, not recency: the access
# pattern is a burst of reactions on a message that was just posted, so an
# LRU's bookkeeping would buy nothing over a plain bounded dict.
_AUTHOR_CACHE_MAX_ENTRIES = 1024


class FeedbackReactionCog(commands.Cog):
    """Listens for a thumbs-up/down reaction on a bot message and records it.

    Registered ONCE via `bot.add_cog` in `setup_hook`. See the module
    docstring for why `on_raw_reaction_add` is the one place in this module
    that catches broadly.
    """

    def __init__(self, bot: DaimonBot) -> None:
        self._bot = bot
        # message id -> "the bot wrote it", populated only by a fetch that
        # actually resolved an author. Per-process and deliberately not
        # persisted: it is a REST-amplification bound, not a source of truth.
        self._author_is_bot: dict[int, bool] = {}

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        try:
            await self._record_vote_from_reaction(payload)
        except Exception as err:  # noqa: BLE001 -- listener dispatch is an adapter boundary (see module docstring); discord.py's own event dispatcher swallows anything raised here
            log.exception("feedback_reactions.handler_failed", err_type=type(err).__name__)

    async def _record_vote_from_reaction(self, payload: discord.RawReactionActionEvent) -> None:
        if self._bot.user is None:
            return  # not connected yet
        bot_user_id = self._bot.user.id

        if is_escalation_reaction(
            emoji_name=payload.emoji.name,
            emoji_is_custom=payload.emoji.id is not None,
            reactor_id=payload.user_id,
            bot_user_id=bot_user_id,
            guild_id=payload.guild_id,
        ):
            # Escalation shares this listener because it shares the seeded
            # reactions, and nothing else. It never touches message_feedback.
            verdict = is_bot_authored(
                message_author_id=payload.message_author_id, bot_user_id=bot_user_id
            )
            if verdict is False:
                return
            if verdict is None and not await self._is_bot_message_via_fetch(
                payload, bot_user_id=bot_user_id
            ):
                return
            await self._offer_support(payload)
            return

        candidate_vote = vote_for_reaction(
            emoji_name=payload.emoji.name,
            emoji_is_custom=payload.emoji.id is not None,
            reactor_id=payload.user_id,
            bot_user_id=bot_user_id,
            guild_id=payload.guild_id,
        )
        if candidate_vote is None:
            return
        verdict = is_bot_authored(
            message_author_id=payload.message_author_id, bot_user_id=bot_user_id
        )
        if verdict is False:
            return
        if verdict is None and not await self._is_bot_message_via_fetch(
            payload, bot_user_id=bot_user_id
        ):
            return

        # guild_id is guaranteed non-None: vote_for_reaction already returned
        # None above for any reaction with no guild (a DM reaction).
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(payload.guild_id))

        async with self._bot.runtime.sessionmaker() as session, session.begin():
            tenant = await get_tenant(session, tenant_id)
            if tenant is None:
                log.info("feedback.tenant_missing", tenant_id=str(tenant_id))
                return

            # Best-effort attribution hint only (get_latest_thread_session's
            # docstring) -- never used to bind or resume a session.
            thread_row = await get_latest_thread_session(
                session,
                tenant_id=tenant_id,
                platform="discord",
                thread_id=str(payload.channel_id),
            )
            ma_session_id = thread_row.ma_session_id if thread_row is not None else None

            # Read-only, deliberately NOT get_or_create: a bystander's
            # reaction must not mint an identity record for a stranger.
            principal = await find_platform_principal(
                session,
                tenant_id=tenant_id,
                platform="discord",
                external_id=str(payload.user_id),
            )
            account_id = principal.account_id if principal is not None else None

            result = await record_vote(
                session,
                tenant_id=tenant_id,
                platform="discord",
                message_id=str(payload.message_id),
                channel_id=str(payload.channel_id),
                platform_user_id=str(payload.user_id),
                account_id=account_id,
                ma_session_id=ma_session_id,
                vote=candidate_vote,
            )

        # `RecordVoteResult.previous_vote` is a bare `str | None` at the store
        # boundary -- every row is written through this same record_vote call
        # with a validated `Vote`, so this narrowing reflects a real invariant.
        previous_vote = cast("Vote | None", result.previous_vote)

        log.info(
            "feedback.vote_recorded",
            message_id=str(payload.message_id),
            vote=candidate_vote,
            is_new_vote=previous_vote != candidate_vote,
        )

        if should_trigger_feedback_dm(previous_vote=previous_vote, new_vote=candidate_vote):
            await self._send_feedback_prompt(payload, feedback_id=result.row.id)

    async def _is_bot_message_via_fetch(
        self, payload: discord.RawReactionActionEvent, *, bot_user_id: int
    ) -> bool:
        """Resolve the undecidable case by fetching the message once per message.

        The gateway field this falls back from (`message_author_id`) is
        undocumented by Discord and may stop arriving without notice, so
        every use of this fallback is logged with its own distinct event so
        a production rollout can tell whether this path is rare or common --
        the event fires on a memo hit too, so it keeps measuring how often
        the field is missing rather than how often we fetch.

        Returns False (record nothing) whenever the author cannot be
        verified: a channel that cannot be resolved, or a fetch that raises.
        An unverifiable author must never resolve to "assume ours". Those
        outcomes are deliberately NOT memoized -- an unresolvable channel or
        a transient fetch failure would otherwise permanently suppress every
        later vote on that message.
        """
        memoized = self._author_is_bot.get(payload.message_id)
        log.info(
            "feedback.author_fallback_used",
            message_id=str(payload.message_id),
            memoized=memoized is not None,
        )
        if memoized is not None:
            return memoized
        try:
            channel = self._bot.get_channel(payload.channel_id)
            if channel is None:
                channel = await self._bot.fetch_channel(payload.channel_id)
            if not isinstance(channel, discord.abc.Messageable):
                return False
            message = await channel.fetch_message(payload.message_id)
        except discord.HTTPException:
            log.info("feedback.author_unverifiable", message_id=str(payload.message_id))
            return False
        is_bot_message = message.author.id == bot_user_id
        self._memoize_author(payload.message_id, is_bot_message)
        return is_bot_message

    def _memoize_author(self, message_id: int, is_bot_message: bool) -> None:
        """Record one resolved verdict, dropping the oldest entry past the cap."""
        if len(self._author_is_bot) >= _AUTHOR_CACHE_MAX_ENTRIES:
            del self._author_is_bot[next(iter(self._author_is_bot))]
        self._author_is_bot[message_id] = is_bot_message

    async def _offer_support(self, payload: discord.RawReactionActionEvent) -> None:
        """Bridge an escalate reaction into the private note form.

        Reacting spends NOTHING. This only reads the person's remaining
        credits to decide which of two direct messages to send, and the
        authoritative gate runs again inside the write transaction when the
        modal is submitted -- someone can react, wait, and submit after
        spending their last credit elsewhere, and only the write decides.

        An unset escalation channel disables the affordance rather than
        recording requests nobody will read: an escalate path that reaches no
        one is worse than none, because the person believes they have asked
        for help.
        """
        settings = self._bot.runtime.settings
        allowance = settings.support.credits_per_user
        if settings.support.escalation_channel_id is None or allowance <= 0:
            log.info("support.disabled", message_id=str(payload.message_id))
            return

        # guild_id is guaranteed non-None: is_escalation_reaction already
        # returned False for any reaction lacking a guild.
        tenant_id = derive_tenant_uuid(platform="discord", workspace_id=str(payload.guild_id))
        async with self._bot.runtime.sessionmaker() as session:
            used = await count_escalations_for_user(
                session, tenant_id=tenant_id, platform_user_id=str(payload.user_id)
            )

        try:
            user = self._bot.get_user(payload.user_id)
            if user is None:
                user = await self._bot.fetch_user(payload.user_id)
            if not has_credit(allowance=allowance, used=used):
                await user.send(
                    "You've used all your human-support requests. "
                    "Contact us if you'd like more added to your account."
                )
                return
            left = remaining_credits(allowance=allowance, used=used)
            view: discord.ui.View = discord.ui.View(timeout=None)
            view.add_item(
                SupportEscalateButton(
                    guild_id=str(payload.guild_id),
                    channel_id=str(payload.channel_id),
                    message_id=str(payload.message_id),
                )
            )
            await user.send(
                content=(
                    f"You asked for a human on that answer. "
                    f"You have {left} support request(s) left -- "
                    "tell us what you need and we'll pick it up."
                ),
                view=view,
            )
        except discord.HTTPException as exc:
            # Closed DMs are the common case and there is no other channel to
            # reach this person on. Nothing has been spent, so this costs them
            # only the prompt.
            log.info(
                "support.prompt_undeliverable",
                message_id=str(payload.message_id),
                error=str(exc),
            )

    async def _send_feedback_prompt(
        self, payload: discord.RawReactionActionEvent, *, feedback_id: uuid.UUID
    ) -> None:
        """Open the private free-text path for a genuinely new down-vote.

        One message per new down-vote -- no batching, no re-pointing an
        earlier message: each one names a different answer, so collapsing
        them would lose which answer the text belongs to. The vote itself
        is already committed by the time this runs, so a delivery failure
        here (most commonly `discord.Forbidden` -- direct messages closed)
        costs the reactor nothing but the follow-up prompt.
        """
        # guild_id is guaranteed non-None: the pure gate in vote_for_reaction
        # already dropped any reaction lacking a guild.
        link = (
            f"https://discord.com/channels/{payload.guild_id}"
            f"/{payload.channel_id}/{payload.message_id}"
        )
        # A live View exists only to carry the item onto the wire -- dispatch
        # happens via class registration (FeedbackButton), never this instance.
        view: discord.ui.View = discord.ui.View(timeout=None)
        view.add_item(FeedbackButton(feedback_id=str(feedback_id)))
        try:
            user = self._bot.get_user(payload.user_id)
            if user is None:
                user = await self._bot.fetch_user(payload.user_id)
            await user.send(
                content=f"You flagged {link} -- want to tell us what went wrong?", view=view
            )
        except discord.HTTPException as exc:
            # discord.Forbidden (closed DMs) is the common case -- there is no
            # other channel to reach this person on, so this must never raise.
            log.info(
                "feedback.prompt_undeliverable", message_id=str(payload.message_id), error=str(exc)
            )
