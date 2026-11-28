"""Discord's `Poster`: post a routine's result (a `DeliveryJob`) to its destination.

Run by the bot's delivery poller (`daimon.core.routine_delivery`). The
destination is resolved here — a channel, or a thread, which on Discord is a
channel too — and must belong to the routine's own, live guild. A thread's
parent is fetched when Discord's cache does not hold it, because the parent's
category is what category protection is checked against; if it cannot be
resolved, nothing is posted there (fail closed). The tenant's access policy
then decides with the parent channel and category in hand.

The creator is checked first, before anything is resolved or sent: if the
policy cannot be read, or the creator may no longer invoke the agent, nothing
is sent anywhere. Only a cleared result may then fall back: when the
destination cannot be used — protected, gone, or outside the guild — it goes
to the creator by direct message instead, if the tenant's direct-message
policy allows them, so it never silently goes nowhere.
Mentions are disabled on every post: the text is the agent's, and a routine
must not ping anyone on its own.

With agent identity on, the destination post goes through
`DiscordPostTransport`, so it carries the agent's name and face (or, where
webhooks are unavailable, the bot with the agent's name label) and drops the
"from <agent>" wording. The fallback direct message stays Daimon's own notice.
Each destination post is recorded as the agent's (`source='routine'`, or `'workflow'`, the
creator as requester), so a reply to it starts a turn like a reply to an
answer, and only the creator or an admin can have the agent tidy it.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

import structlog
from anthropic import AsyncAnthropic
from daimon.adapters.discord.membership import GatewayMembershipResolver
from daimon.adapters.discord.output_delivery import upload_limit, upload_output_file
from daimon.adapters.discord.post_transport import DiscordPostTransport
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.agent_identity import AgentIdentity
from daimon.core.agent_post_identity import fallback_name_prefix
from daimon.core.authz import Action, AgentRef, Place, Subject, Surface, authorize
from daimon.core.channel_tidy import record_turn_post
from daimon.core.config import DirectMessagePolicy
from daimon.core.delivery import RoutinePoster
from daimon.core.dm_access import roles_by_membership, sender_admits
from daimon.core.fire.delivery import DeliveryJob, Poster, routine_poster
from daimon.core.membership import MembershipResolver
from daimon.core.membership_subject import resolve_subject
from daimon.core.message_split import split_fenced
from daimon.core.outbound.opt_out import untag_opted_out
from daimon.core.output_delivery import DeliverableFile
from daimon.core.permissions import any_agent_rules, any_own_readers
from daimon.core.routine_delivery import (
    DeliveryOutcome,
    clear_creator,
    delivery_target,
    discord_creator_may_post,
    private_delivery_refusal,
    render_fallback_dm,
    render_fallback_post,
    routine_agent_name,
    tenant_dm_still_open,
)
from daimon.core.routine_files import deliver_routine_files, payload_for
from daimon.core.routine_hold import DeliveryGate, delivery_gate, result_is_held
from daimon.core.rule_views import keeps_routine_inside
from daimon.core.stores.direct_messages import dm_enabled
from daimon.core.stores.tenants import get_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord

__all__ = ["make_discord_poster", "make_discord_routine_poster"]

log = structlog.get_logger(__name__)

_DISCORD_MAX_CHARS = 2000

ChannelFetcher = Callable[[int], Awaitable[object]]
#: Open a DM with a member of a guild: `(guild_id, user_id)` → something with
#: `send`. Raises `discord.HTTPException` (or `LookupError`) when it cannot.
DmOpener = Callable[[int, int], Awaitable[discord.abc.Messageable]]
DmPolicyLookup = Callable[[DeliveryJob], DirectMessagePolicy]


async def _agent_admits(
    sessionmaker: async_sessionmaker[AsyncSession],
    resolver: MembershipResolver | None,
    job: DeliveryJob,
    user_id: str,
) -> bool:
    """The routine's agent grant admits the person, by their current server roles."""
    async with sessionmaker() as session:
        tenant = await get_tenant(session, job.tenant_id)
    if tenant is None:
        return False
    return await sender_admits(
        sessionmaker,
        tenant_id=job.tenant_id,
        agent_name=job.agent_name,
        user_id=user_id,
        groups=roles_by_membership(resolver, tenant_id=job.tenant_id, guild_id=tenant.external_id),
    )


#: The identity a routine's result posts under: `(job, guild_id)` → identity.
IdentityResolver = Callable[[DeliveryJob, str], Awaitable[AgentIdentity]]
AgentNamesResolver = Callable[[DeliveryJob], Awaitable[tuple[str | None, ...]]]

_UNRESOLVED = object()


async def _untagged(
    sessionmaker: async_sessionmaker[AsyncSession], job: DeliveryJob, text: str
) -> str:
    """``text`` with anyone who opted out of tags shown by plain name."""
    return await untag_opted_out(
        sessionmaker, tenant_id=job.tenant_id, platform="discord", text=text
    )


async def channel_placement(
    channel: object, fetch_channel: ChannelFetcher
) -> tuple[str | None, str | None, int | None, object] | None:
    """(parent_channel_id, category_id, guild_id, permission_source) of a
    postable channel. `permission_source` is the channel whose permission
    overwrites apply: the channel itself, or a thread's parent.

    `None` when the channel is not a text channel or thread, or when a
    thread's parent cannot be resolved (so its category is unknown).
    """
    if isinstance(channel, discord.Thread):
        parent: object = channel.parent
        if parent is None and channel.parent_id:
            try:
                parent = await fetch_channel(channel.parent_id)
            except discord.HTTPException:
                parent = _UNRESOLVED
        if parent is _UNRESOLVED or parent is None:
            return None
        category = getattr(parent, "category_id", None)
        return (
            str(channel.parent_id) if channel.parent_id else None,
            str(category) if category else None,
            channel.guild.id,
            parent,
        )
    if isinstance(channel, discord.TextChannel):
        return (
            None,
            str(channel.category_id) if channel.category_id else None,
            channel.guild.id,
            channel,
        )
    return None


async def creator_may_post(channel: object, permission_source: object, user_id: int) -> bool:
    """Whether the routine's creator, as a member of the channel's guild, may
    post in `channel` — the rule `send_message` applies to a caller.

    Permissions come from `permission_source` (a thread's parent), so an
    uncached parent never breaks the check. A creator who has left the guild,
    or whose membership cannot be read, may not post.
    """
    if not isinstance(channel, discord.Thread | discord.TextChannel) or not isinstance(
        permission_source, discord.TextChannel | discord.ForumChannel
    ):
        return False
    guild = channel.guild
    try:
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
    except discord.HTTPException:
        return False
    perms = permission_source.permissions_for(member)
    is_thread = isinstance(channel, discord.Thread)
    is_private_thread = is_thread and channel.type is discord.ChannelType.private_thread
    is_thread_member = False
    if is_private_thread and not (member.guild_permissions.administrator or perms.manage_threads):
        assert isinstance(channel, discord.Thread)
        try:
            await channel.fetch_member(user_id)
            is_thread_member = True
        except discord.HTTPException:
            is_thread_member = False
    return discord_creator_may_post(
        administrator=member.guild_permissions.administrator,
        view_channel=perms.view_channel,
        send_messages=perms.send_messages,
        is_thread=is_thread,
        send_messages_in_threads=perms.send_messages_in_threads,
        is_private_thread=is_private_thread,
        manage_threads=perms.manage_threads,
        is_thread_member=is_thread_member,
    )


def make_discord_routine_poster(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    fetch_channel: ChannelFetcher,
    open_dm: DmOpener,
    dm_policy: DmPolicyLookup,
    client: discord.Client | None = None,
    output_client: AsyncAnthropic | None = None,
    resolve_identity: IdentityResolver | None = None,
    membership_resolver: MembershipResolver | None = None,
    resolve_agent_names: AgentNamesResolver | None = None,
) -> RoutinePoster:
    """The routine outbox's poster, over `make_discord_poster`."""
    return routine_poster(
        sessionmaker,
        make_discord_poster(
            sessionmaker,
            fetch_channel=fetch_channel,
            open_dm=open_dm,
            dm_policy=dm_policy,
            client=client,
            output_client=output_client,
            resolve_identity=resolve_identity,
            membership_resolver=membership_resolver,
            resolve_agent_names=resolve_agent_names,
        ),
        platform="discord",
    )


def make_discord_poster(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    fetch_channel: ChannelFetcher,
    open_dm: DmOpener,
    dm_policy: DmPolicyLookup,
    client: discord.Client | None = None,
    output_client: AsyncAnthropic | None = None,
    resolve_identity: IdentityResolver | None = None,
    membership_resolver: MembershipResolver | None = None,
    resolve_agent_names: AgentNamesResolver | None = None,
) -> Poster:
    """A poster bound to the bot's channel lookup (cache first, then REST).

    Without `client` and `resolve_identity` every result posts as the bot.
    """

    membership_resolver = membership_resolver or (
        GatewayMembershipResolver(client) if client is not None else None
    )

    def refusal(job: DeliveryJob, reason: str) -> DeliveryOutcome:
        private_reasons = {
            "invoker_not_allowed": "The owner is no longer a verified, allowed server member.",
            "access_policy_unreadable": "The server access settings could not be read.",
            "routine_dms_disabled": "Routine DMs are disabled for this server.",
            "restricted_source": "A restricted source cannot deliver by DM.",
            "agent_not_allowed": "The agent is not allowed to run privately.",
            "owner_outside_team": "The owner holds no internal role, so the result can't go by DM.",
            "destination_unavailable": "The routine server or recipient is unavailable.",
        }
        return DeliveryOutcome(
            status="skipped",
            note=private_reasons.get(reason, reason) if job.destination_kind == "dm" else reason,
        )

    async def creator_still_cleared(job: DeliveryJob) -> DeliveryOutcome | None:
        if (stale := await job.still_current()) is not None:
            return DeliveryOutcome(status="skipped", note=stale)
        if job.destination_kind == "dm":
            async with sessionmaker() as session:
                if not await dm_enabled(session, tenant_id=job.tenant_id):
                    return DeliveryOutcome(
                        status="skipped", note="Routine DMs are disabled for this server."
                    )
        if job.destination_kind == "dm" and not (
            dm_policy(job).allows(job.created_by_user_id or "")
            and await _agent_admits(
                sessionmaker, membership_resolver, job, job.created_by_user_id or ""
            )
        ):
            return DeliveryOutcome(
                status="skipped", note="The owner is not allowed to receive DMs."
            )
        cleared = await clear_creator(
            sessionmaker, job, platform="discord", membership_resolver=membership_resolver
        )
        if not isinstance(cleared, TenantAccessPolicy):
            return refusal(job, cleared)
        if job.destination_kind == "dm" and (any_agent_rules(cleared) or any_own_readers(cleared)):
            if resolve_agent_names is None:
                return DeliveryOutcome(
                    status="skipped", note="The routine agent could not be verified."
                )
            names = await resolve_agent_names(job)
            # The owner, freshly resolved: a role-gated agent delivers only for
            # an owner who holds its roles. A routine with no owner holds none.
            owner = (
                Subject()
                if job.created_by_user_id is None
                else await resolve_subject(
                    sessionmaker,
                    membership_resolver,
                    tenant_id=job.tenant_id,
                    user_id=job.created_by_user_id,
                    require_fresh=True,
                )
            )
            if not names or not authorize(
                cleared,
                subject=owner,
                action=Action.RUN_AGENT,
                surface=Surface.ROUTINE,
                agent=AgentRef.of(*names),
                place=Place(),
            ):
                return DeliveryOutcome(
                    status="skipped", note="The agent is not allowed to deliver privately."
                )
        if job.destination_kind == "dm" and keeps_routine_inside(cleared, job):
            return DeliveryOutcome(
                status="skipped", note="A restricted source cannot deliver by DM."
            )
        return None

    async def attach(
        job: DeliveryJob,
        destination: discord.abc.Messageable,
        fallback_guard: Callable[[], Awaitable[bool]] | None = None,
        gate: DeliveryGate | None = None,
    ) -> None:
        async def may_post() -> bool:
            if fallback_guard is not None and not await fallback_guard():
                return False
            if await creator_still_cleared(job) is not None:
                return False
            if isinstance(destination, discord.TextChannel | discord.Thread):
                placement = await channel_placement(destination, fetch_channel)
                if placement is None or job.created_by_user_id is None:
                    return False
                parent, category, _guild, permissions = placement
                policy = await clear_creator(
                    sessionmaker, job, platform="discord", membership_resolver=membership_resolver
                )
                try:
                    audience = (
                        await gate.audience(str(destination.id), parent)
                        if gate is not None
                        else None
                    )
                except Exception as exc:
                    log.warning(
                        "routine.delivery_audience_unreadable", error_type=type(exc).__name__
                    )
                    return False
                return bool(
                    isinstance(policy, TenantAccessPolicy)
                    and authorize(
                        policy,
                        subject=Subject(),
                        action=Action.POST,
                        surface=Surface.ROUTINE,
                        place=Place(
                            channel_id=str(destination.id),
                            parent_channel_id=parent,
                            category_id=category,
                        ),
                        hold=gate.post_hold if gate is not None else None,
                        audience=audience,
                    )
                    and await creator_may_post(
                        destination, permissions, int(job.created_by_user_id)
                    )
                )
            return True

        async def notice(text: str) -> None:
            await destination.send(content=text, allowed_mentions=discord.AllowedMentions.none())

        async def upload(file: DeliverableFile) -> None:
            await upload_output_file(destination, file)

        await deliver_routine_files(
            output_client,
            payload_for(job),
            max_bytes=upload_limit(destination),
            post=upload,
            notice=notice,
            may_post=may_post,
            sessions=sessionmaker,
            tenant_id=job.tenant_id,
        )

    async def _dm_fallback(
        job: DeliveryJob,
        reason: str,
        guild_id: str,
        may_post: Callable[[], Awaitable[bool]],
    ) -> DeliveryOutcome:
        async with sessionmaker() as session:
            if not await dm_enabled(session, tenant_id=job.tenant_id):
                return DeliveryOutcome(
                    status="skipped", note="Routine DMs are disabled for this server."
                )
        creator = job.created_by_user_id
        if (
            creator is None
            or not creator.isdigit()
            or not guild_id.isdigit()
            or not dm_policy(job).allows(creator)
            or not await _agent_admits(sessionmaker, membership_resolver, job, creator)
        ):
            return DeliveryOutcome(status="skipped", note=reason)
        try:
            if not await may_post():
                return DeliveryOutcome(status="skipped", note="Fallback DM access changed.")
            dm = await open_dm(int(guild_id), int(creator))
            for chunk in split_fenced(
                await _untagged(
                    sessionmaker,
                    job,
                    render_fallback_dm(job, reason, await routine_agent_name(sessionmaker, job)),
                ),
                _DISCORD_MAX_CHARS,
                word_boundary=True,
            ):
                async with sessionmaker() as session:
                    if not await dm_enabled(session, tenant_id=job.tenant_id):
                        return DeliveryOutcome(
                            status="skipped", note="Routine DMs are disabled for this server."
                        )
                if (refused := await creator_still_cleared(job)) is not None:
                    return refused
                if not await may_post():
                    return DeliveryOutcome(status="skipped", note="Fallback DM access changed.")
                await dm.send(content=chunk, allowed_mentions=discord.AllowedMentions.none())
            await attach(job, dm, fallback_guard=may_post)
        except (discord.HTTPException, LookupError) as err:
            log.warning(
                "routine.delivery_fallback_failed",
                routine_id=str(job.id),
                error_type=type(err).__name__,
            )
            # Closed DMs or a gone member repeat on every run: a skip, not a failure that pages.
            closed = isinstance(err, (discord.Forbidden, discord.NotFound))
            return DeliveryOutcome(
                status="skipped", note=reason, failed=None if closed else type(err).__name__
            )
        return DeliveryOutcome(status="delivered", note=f"dm_fallback:{reason}")

    async def _post(job: DeliveryJob) -> DeliveryOutcome:
        async with sessionmaker() as session:
            tenant = await get_tenant(session, job.tenant_id)
        if tenant is None or tenant.archived_at is not None:
            return DeliveryOutcome(status="skipped", note="tenant_archived")
        # Creator first, before resolving or sending anything: a creator who
        # may no longer invoke the agent, or an unreadable policy, gets
        # nothing — no destination post and no direct message.
        cleared = await clear_creator(
            sessionmaker, job, platform="discord", membership_resolver=membership_resolver
        )
        if not isinstance(cleared, TenantAccessPolicy):
            log.info("routine.delivery_refused", routine_id=str(job.id), reason=cleared)
            return refusal(job, cleared)
        policy = cleared
        # The hold the result's fire ran under, and the audience mode (`audience_hold`). A
        # failed read refuses the delivery: it never falls back to a mode.
        try:
            gate = await delivery_gate(sessionmaker, job, platform="discord")
        except Exception as exc:
            log.warning("routine.delivery_audience_unreadable", error_type=type(exc).__name__)
            return DeliveryOutcome(status="skipped", note="audience_unreadable")
        # An isolated or shared channel's result never leaves it, not even by DM.
        held = await result_is_held(sessionmaker, job, platform="discord")
        keep_inside = keeps_routine_inside(policy, job) or held or gate.keeps_inside(held)
        parent_channel_id: str | None = None

        async def fallback_guard() -> bool:
            held_now = await result_is_held(sessionmaker, job, platform="discord")
            if held_now or gate.keeps_inside(held_now):
                return False
            if await creator_still_cleared(job) is not None:
                return False
            # Every DM send of a channel routine (the fallback and its attachments) asks
            # again whether the owner is still inside the team (`on` only).
            if (
                await private_delivery_refusal(
                    sessionmaker, job, membership_resolver=membership_resolver
                )
                is not None
            ):
                return False
            if not await tenant_dm_still_open(sessionmaker, job, tenant.external_id):
                return False
            creator = job.created_by_user_id
            if (
                creator is None
                or not dm_policy(job).allows(creator)
                or not await _agent_admits(sessionmaker, membership_resolver, job, creator)
            ):
                return False
            current_policy = await clear_creator(
                sessionmaker, job, platform="discord", membership_resolver=membership_resolver
            )
            return isinstance(current_policy, TenantAccessPolicy) and not keeps_routine_inside(
                current_policy, job, parent_channel_id=parent_channel_id
            )

        async def fallback(reason: str) -> DeliveryOutcome:
            if keep_inside:
                return DeliveryOutcome(status="skipped", note=reason)
            return await _dm_fallback(job, reason, tenant.external_id, fallback_guard)

        if job.destination_kind == "dm":
            if job.destination_id is None or job.destination_id != job.created_by_user_id:
                return DeliveryOutcome(status="skipped", note="DM recipient must be the owner.")
            if (refused := await creator_still_cleared(job)) is not None:
                return refused
            try:
                dm = await open_dm(int(tenant.external_id), int(job.destination_id))
                text = (
                    f"Routine {job.trigger_message[:60]}\n"
                    f"Run {job.id} / {job.last_fired_at}\n\n"
                    + await _untagged(
                        sessionmaker,
                        job,
                        render_fallback_post(
                            job,
                            agent_name=await routine_agent_name(sessionmaker, job),
                            header=job.header,
                        ),
                    )
                )
                for chunk in split_fenced(text, _DISCORD_MAX_CHARS, word_boundary=True):
                    if (refused := await creator_still_cleared(job)) is not None:
                        return refused
                    await dm.send(content=chunk, allowed_mentions=discord.AllowedMentions.none())
                await attach(job, dm)
            except (discord.HTTPException, LookupError, ValueError, TypeError) as err:
                log.info(
                    "routine.delivery_dm_failed",
                    routine_id=str(job.id),
                    error_type=type(err).__name__,
                )
                return DeliveryOutcome(
                    status="skipped", note="The DM could not be delivered; it will not be retried."
                )
            return DeliveryOutcome(status="delivered")

        target = delivery_target(job, platform="discord")
        if target is None or not target.channel_id.isdigit():
            return await fallback("destination_unavailable")
        try:
            channel = await fetch_channel(int(target.channel_id))
        except discord.HTTPException:
            return await fallback("destination_unavailable")
        placement = await channel_placement(channel, fetch_channel)
        if placement is None or str(placement[2]) != tenant.external_id:
            # Not a text channel or thread, its parent is unknown, or it is in
            # another guild.
            return await fallback("destination_unavailable")
        parent_channel_id, category_id, _guild_id, permission_source = placement
        # The parent is known now, also for a thread saved without it.
        held = held or await result_is_held(
            sessionmaker, job, platform="discord", parent_channel_id=parent_channel_id
        )
        keep_inside = (
            keeps_routine_inside(policy, job, parent_channel_id=parent_channel_id)
            or held
            or gate.keeps_inside(held)
        )
        creator = job.created_by_user_id
        if (
            creator is None
            or not creator.isdigit()
            or not await creator_may_post(channel, permission_source, int(creator))
        ):
            # A routine posts on its creator's behalf: never somewhere they
            # could not post themselves. Their own DM is still fine.
            log.info(
                "routine.delivery_refused", routine_id=str(job.id), reason="creator_cannot_post"
            )
            return await fallback("creator_cannot_post")
        try:
            audience = await gate.audience(target.channel_id, parent_channel_id)
        except Exception as exc:
            log.warning("routine.delivery_audience_unreadable", error_type=type(exc).__name__)
            return DeliveryOutcome(status="skipped", note="audience_unreadable")
        decision = authorize(
            policy,
            subject=Subject(),
            action=Action.POST,
            surface=Surface.ROUTINE,
            place=Place(
                channel_id=target.channel_id,
                parent_channel_id=parent_channel_id,
                category_id=category_id,
            ),
            hold=gate.post_hold,
            audience=audience,
        )
        if decision.reason == "audience_contained":
            # A result made outside a room that outsiders can now read stays out of it.
            log.info("routine.delivery_refused", routine_id=str(job.id), reason=decision.reason)
            return DeliveryOutcome(status="skipped", note="audience_contained")
        if not decision:
            log.info("routine.delivery_refused", routine_id=str(job.id), reason="protected_channel")
            return await fallback("protected_channel")
        assert isinstance(channel, discord.Thread | discord.TextChannel)

        async def still_deliverable() -> DeliveryOutcome | None:
            """The audience check, asked again before each chunk: a room can become one that
            outsiders read while a long result is still going out."""
            # `shadow` logged its record at the first check; only `on` refuses a chunk.
            if gate.mode != "on" or not gate.stored.recorded:
                return None
            try:
                fresh = await gate.audience(target.channel_id, parent_channel_id)
            except Exception as exc:
                log.warning("routine.delivery_audience_unreadable", error_type=type(exc).__name__)
                return DeliveryOutcome(status="skipped", note="audience_unreadable")
            again = authorize(
                policy,
                subject=Subject(),
                action=Action.POST,
                surface=Surface.ROUTINE,
                place=Place(
                    channel_id=target.channel_id,
                    parent_channel_id=parent_channel_id,
                    category_id=category_id,
                ),
                hold=gate.post_hold,
                audience=fresh,
            )
            if again.reason == "audience_contained":
                log.info("routine.delivery_refused", routine_id=str(job.id), reason=again.reason)
                return DeliveryOutcome(status="skipped", note="audience_contained")
            return None

        delivery_id = job.delivery_id or uuid.uuid4()  # the chunks are one reply for delete_reply

        async def record(sent: discord.Message) -> None:
            # A reply to the result starts a turn only for a recorded post (a
            # webhook post cannot be pinged); the creator is who asked for it.
            await record_turn_post(
                sessionmaker,
                tenant_id=job.tenant_id,
                platform="discord",
                ma_agent_id=job.agent_id,
                channel_id=str(channel.id),
                message_id=str(sent.id),
                requester_platform_user_id=creator,
                source=job.source,
                parent_channel_id=parent_channel_id,
                delivery_id=delivery_id,
            )

        identity = (
            await resolve_identity(job, tenant.external_id)
            if client is not None and resolve_identity is not None
            else None
        )
        if client is None or identity is None or identity.builtin:
            for chunk in split_fenced(
                await _untagged(
                    sessionmaker,
                    job,
                    render_fallback_post(
                        job,
                        platform="discord",
                        agent_name=await routine_agent_name(sessionmaker, job),
                        header=job.header,
                    ),
                ),
                _DISCORD_MAX_CHARS,
                word_boundary=True,
            ):
                if (refused := await creator_still_cleared(job)) is not None:
                    return refused
                if (refused := await still_deliverable()) is not None:
                    return refused
                sent = await channel.send(
                    content=chunk, allowed_mentions=discord.AllowedMentions.none()
                )
                await record(sent)
            await attach(job, channel, gate=gate)
            return DeliveryOutcome(status="delivered")
        # A bot fallback puts the name label on top, so leave room for it.
        limit = _DISCORD_MAX_CHARS - len(fallback_name_prefix(identity.name, ""))
        transport = DiscordPostTransport(
            client,
            channel,
            name=identity.name,
            avatar_url=identity.avatar_url,
            builtin=False,
            identity_enabled=True,
        )
        for index, chunk in enumerate(
            split_fenced(
                await _untagged(
                    sessionmaker,
                    job,
                    render_fallback_post(
                        job,
                        as_agent=True,
                        platform="discord",
                        agent_name=identity.name,
                        header=job.header,
                    ),
                ),
                limit,
                word_boundary=True,
            )
        ):
            if (refused := await creator_still_cleared(job)) is not None:
                return refused
            if (refused := await still_deliverable()) is not None:
                return refused
            sent = await transport.send(
                content=chunk,
                allowed_mentions=discord.AllowedMentions.none(),
                _prefix_if_fallback=index == 0,
            )
            await record(sent)
        await attach(job, channel, gate=gate)
        return DeliveryOutcome(status="delivered")

    return _post
