"""Leased compute lifecycle and background commands.

Only the authenticated session owner, through the attended conversation the session
started in, may execute in, write to or extend it, within the configured caps. The
owner lists and stops their sessions from any of their attended conversations; a
tenant admin who is present, or the fenced reconciler, may stop one too.

Only the session owner, through an authorized attended conversation, can start a
background command; its status and result go only to that owner; its completion
wake goes only to the originating conversation, once. The turn that wake starts
may read the command's session (`turn_may_read`). Where a server admin turned on
`compute_wake_runs`, it may also run commands on it, write to it and stop it, for a
limited number of wake turns in a row (`turn_may_act`); it never launches, extends or
lists. An inline command is a background command that its caller waits for (`run`).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil
from typing import Literal

import structlog
from daimon.core import compute_tasks as scripts
from daimon.core.access_policy import AGENT_CHAT_SCOPE_PREFIX
from daimon.core.compute_models import (
    ACTIVE_TASK_STATES,
    ComputeResource,
    ComputeSession,
    ComputeStatus,
    ComputeTask,
    machine_readiness,
)
from daimon.core.compute_provider import (
    ComputeProvider,
    ComputeProviderError,
    app_name,
    session_token,
)
from daimon.core.config import ComputeSettings
from daimon.core.errors import StoreError
from daimon.core.features.snapshot import FeaturePlace, feature_in_or_none
from daimon.core.stores import compute_sessions as store
from daimon.core.stores import compute_tasks as task_store
from daimon.core.stores.domain import TurnOriginRow
from daimon.core.stores.session_settle_state import find_agent_chat_session
from daimon.core.stores.tenants import list_tenants_by_platform
from daimon.core.stores.turn_origins import get_active_origin
from pydantic import JsonValue
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

log = structlog.get_logger(__name__)

# How long a launch waits for its machine before it returns the session as still
# starting, and how often it asks the provider meanwhile.
_LAUNCH_WAIT = timedelta(seconds=30)
_START_POLL_SECONDS = 2.0
# A machine that has not started this long after launch is treated as failed.
_START_DEADLINE = timedelta(minutes=10)
#: What a caller is told while a machine starts, and when it never will.
STILL_STARTING = "The machine is still starting. Try again in about 10 seconds."
START_FAILED = (
    "The machine could not start, so it is being removed. "
    "Ask an admin to check the image profile it was launched from."
)
#: What the owner is told when they use, from one conversation, a machine another started.
ELSEWHERE = (
    "This machine belongs to another of your conversations. Use it there, or stop it from here."
)
# A start whose inline call was lost has this long to leave its pid file.
_START_GRACE = timedelta(minutes=2)
# The longest the machine may take to answer the call that starts a command.
_START_CALL_SECONDS = 30
# How long an inline command's caller sleeps before each look at it, in seconds;
# every look after these is 2 seconds later.
_INLINE_POLLS = (0.3, 0.7, 1.0)
# An inline command may start while `per_session_tasks` background commands run, so
# the bot can still look at the machine.
_INLINE_EXTRA_TASKS = 4
# Seconds between the caller's last look at an inline command and the scheduler's first.
_INLINE_HANDOVER = 5
#: How often a status read that waits looks at its command again.
_STATUS_POLL_SECONDS = 2.0
#: What a status read says when it could not read the command at all.
STATUS_REFUSED = "Could not check the command right now. Try again soon."
#: The longest a seen end's message withdrawal may hold an answer: past it, the message stays.
_WITHDRAW_SECONDS = 2.0
# How long after its deadline an unreachable or stuck task is given up.
_DEADLINE_GRACE = timedelta(minutes=5)
#: The machine operations that only read: the ones a completion wake's turn may always make.
READ_OPERATIONS = frozenset({"read_file", "list_files"})


#: What an agent-chat session is told when cloud computers from agent chat are off for it.
AGENT_CHAT_OFF = (
    "Cloud computers from agent chat are turned off here. Nothing was done. "
    "Ask a server admin to turn them on."
)


@dataclass(frozen=True)
class ComputePlace:
    """Where an agent-chat session uses cloud computers (`agent_chat_owner`).

    It is no turn origin: its thread is a scope of compute alone (`AGENT_CHAT_SCOPE_PREFIX`),
    never given to origin, seal or audience code, and its parent channel is empty. The
    agent fields are named as an origin's, for the session row. `feature_channel_id` is the
    channel the session's tools were chosen for, where its features are read. Its owner
    counts as present: the rule that made it is checked again on every call (`_origin`).
    """

    platform: str
    thread_id: str
    responder_ma_agent_id: str
    responder_name: str
    feature_channel_id: str | None
    parent_channel_id: str = ""


def agent_chat_thread(chat_session_id: str) -> str:
    """The compute scope of the agent-chat session whose credential signs `chat_session_id`."""
    return AGENT_CHAT_SCOPE_PREFIX + chat_session_id


async def agent_chat_owner(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    chat_session_id: str,
    platform: str,
    agent_id: str,
    agent_name: str,
) -> ComputePlace | None:
    """The one rule for cloud computers from agent chat: the place of the agent-chat session
    whose credential signs `chat_session_id`, or None.

    The session was found by that grant for the account that created it
    (`find_agent_chat_session`), and `compute` and `compute_agent_chat` are both on for its
    channel and agent (`feature_in_or_none` checks no `depends_on`, so both are read; a
    feature that can't be read is off). The credential's own checks (a chat execution of
    a person's account, no agent key, routine, helper or held turn) are its caller's
    (`tools.compute._caller`).
    """
    found = await find_agent_chat_session(db, grant=chat_session_id, account_id=account_id)
    if found is None:
        return None
    place = FeaturePlace.of(tenant_id, platform, found.channel_id, agent_id=agent_id)
    compute = await feature_in_or_none(db, None, place, "compute")
    agent_chat = await feature_in_or_none(db, None, place, "compute_agent_chat")
    if compute is None or agent_chat is None or not (compute.on and agent_chat.on):
        return None
    return ComputePlace(
        platform=platform,
        thread_id=agent_chat_thread(chat_session_id),
        responder_ma_agent_id=agent_id,
        responder_name=agent_name,
        feature_channel_id=found.channel_id,
    )


def owner_present(origin: TurnOriginRow | ComputePlace) -> bool:
    """The owner sent this turn's message and waits for its reply. An agent-chat session's
    owner counts as present wherever its rule holds (`agent_chat_owner`)."""
    if isinstance(origin, ComputePlace):
        return True
    return origin.attended and origin.human_invocation is True


def made_here(session: ComputeSession, origin: TurnOriginRow | ComputePlace) -> bool:
    """Whether `session` started in `origin`'s conversation, the only one that may use it."""
    return (session.platform, session.thread_id, session.parent_channel_id) == (
        origin.platform,
        origin.thread_id,
        origin.parent_channel_id,
    )


async def turn_may_read(
    db: AsyncSession,
    origin: TurnOriginRow | ComputePlace,
    session: ComputeSession,
    *,
    now: datetime,
) -> bool:
    """Whether this turn may read this session: the one rule for every compute read.

    The owner is present, or the turn is the completion wake of one of the session's
    own background commands (`stores.compute_tasks.wake_turn_of`). Such a wake reads
    and reports; only `turn_may_act` lets it run, write or stop, and it never launches
    or extends.
    """
    if isinstance(origin, ComputePlace) or owner_present(origin):
        return True
    return await task_store.wake_turn_of(db, origin, session, now=now)


async def turn_may_act(
    db: AsyncSession,
    origin: TurnOriginRow | ComputePlace,
    session: ComputeSession,
    *,
    now: datetime,
    limit: int,
) -> bool | Literal["limit"]:
    """Whether this turn may run commands, write files or stop this session.

    The owner is present; or the turn is the completion wake of one of the session's
    own commands in a thread, `compute_wake_runs` is on at the session's place, and
    fewer than `limit` wake turns acted since the owner last ran a command or wrote a
    file (`ComputeSession.wake_turns`, read from the locked row). A wake turn already on
    that list may act again. `"limit"` says the list stopped it.
    """
    if isinstance(origin, ComputePlace) or owner_present(origin):
        return True
    if not task_store.wake_reads_in(session) or not await task_store.wake_turn_of(
        db, origin, session, now=now
    ):
        return False
    if not await task_store.wake_runs_on(db, session):
        return False
    turns = session.wake_turns
    return True if origin.id.hex in turns or len(turns) < limit else "limit"


async def note_act(
    db: AsyncSession,
    caller_account_id: uuid.UUID,
    origin: TurnOriginRow | ComputePlace,
    session: ComputeSession,
) -> ComputeSession:
    """Record an act on `session`, in the transaction that holds its lock: the one rule
    for every act path (a command, inline or background, a file write and a stop).

    The owner acting here empties the list; a wake turn `turn_may_act` let through joins
    it once. A chat message alone changes nothing, and neither does a read.
    """
    if owner_present(origin) and caller_account_id == session.owner_account_id:
        turns: list[str] = []
    elif (
        isinstance(origin, ComputePlace)
        or owner_present(origin)
        or origin.id.hex in session.wake_turns
    ):
        return session
    else:
        turns = [*session.wake_turns, origin.id.hex]
    if turns == session.wake_turns:
        return session
    noted = session.model_copy(update={"wake_turns": turns})
    await store.save(db, noted)
    return noted


@dataclass(frozen=True)
class ComputeCaller:
    """Who calls a compute tool. `origin` is a chat turn's verified origin, or the place of
    an agent-chat session (`agent_chat_owner`)."""

    tenant_id: uuid.UUID
    account_id: uuid.UUID
    origin: TurnOriginRow | ComputePlace
    chat_session_id: str
    external_user_id: str
    admin: bool = False


@dataclass(frozen=True)
class Polled:
    """One poll of a task: the task as its transaction committed it (None when it is
    gone), and whether this poll settled it."""

    task: ComputeTask | None
    settled: bool = False


@dataclass(frozen=True)
class InlineRun:
    """What `ComputeSessions.run` saw. `ended_here`: this call settled the task, so its
    caller reports the end. Otherwise the conversation gets the end as a message."""

    task: ComputeTask
    ended_here: bool


class ComputeSessions:
    def __init__(
        self,
        sm: async_sessionmaker[AsyncSession],
        settings: ComputeSettings,
        provider: ComputeProvider,
        *,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.sm, self.settings, self.provider = sm, settings, provider
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep

    def ttl(self, hours: int | None) -> int:
        value = (
            hours
            if hours is not None
            else min(self.settings.default_idle_ttl_hours, self.settings.max_ttl_hours)
        )
        if not 1 <= value <= self.settings.max_ttl_hours:
            raise StoreError(f"Lifetime must be between 1 and {self.settings.max_ttl_hours} hours")
        return value

    async def _origin(
        self, db: AsyncSession, caller: ComputeCaller, *, absent_ok: bool = False
    ) -> TurnOriginRow | ComputePlace:
        """The caller's live origin, or its agent-chat place with its rule checked again.
        ``absent_ok`` leaves a turn with no owner present to `turn_may_read` or
        `turn_may_act`."""
        if isinstance(caller.origin, ComputePlace):
            place = await agent_chat_owner(
                db,
                tenant_id=caller.tenant_id,
                account_id=caller.account_id,
                chat_session_id=caller.chat_session_id,
                platform=caller.origin.platform,
                agent_id=caller.origin.responder_ma_agent_id,
                agent_name=caller.origin.responder_name,
            )
            if place is None or place != caller.origin:
                raise StoreError(AGENT_CHAT_OFF)
            return place
        origin = await get_active_origin(
            db,
            tenant_id=caller.tenant_id,
            account_id=caller.account_id,
            origin_id=caller.origin.id,
            platform=caller.origin.platform,
            now=self.clock(),
        )
        if (
            origin is None
            or not caller.chat_session_id
            or origin.chat_session_id != caller.chat_session_id
            or origin != caller.origin
            or not (absent_ok or owner_present(origin))
        ):
            raise StoreError("Use an active attended conversation with the session owner.")
        return origin

    async def _owned(
        self,
        db: AsyncSession,
        caller: ComputeCaller,
        session_id: uuid.UUID,
        *,
        stop: bool = False,
        read: bool = False,
        act: bool = False,
    ) -> ComputeSession:
        """The caller's session. ``read`` and ``act`` let a turn with no owner present
        through when `turn_may_read` or `turn_may_act` allows it."""
        origin = await self._origin(db, caller, absent_ok=read or act)
        session = await store.get(db, caller.tenant_id, session_id, lock=True)
        if session is None or session.deployment_id != self.settings.deployment_id:
            raise StoreError("Session not found")
        # The emergency stop needs the admin here: a wake turn never stops another's machine.
        if stop and caller.admin and owner_present(origin):
            return session
        if stop and session.owner_account_id == caller.account_id and owner_present(origin):
            # The owner may stop their machine from any of their conversations.
            return session
        if session.owner_account_id != caller.account_id:
            # As for a missing one: another person's machine is not shown to exist.
            raise StoreError("Session not found")
        if not made_here(session, caller.origin):
            if act and not owner_present(origin):
                # Today's words for a turn with nobody here: it cannot stop it from here.
                raise StoreError("The machine owner must be here in an active conversation.")
            raise StoreError(ELSEWHERE)
        if read and not await turn_may_read(db, origin, session, now=self.clock()):
            raise StoreError("The machine owner must be here in an active conversation.")
        if act:
            limit = self.settings.wake_run_limit
            allowed = await turn_may_act(db, origin, session, now=self.clock(), limit=limit)
            if allowed == "limit":
                raise StoreError(
                    f"This machine ran {limit} times with nobody here. Tell the owner what "
                    "happened. It continues after the owner runs a command on it."
                )
            if not allowed:
                raise StoreError("The machine owner must be here in an active conversation.")
        return session

    async def launch(
        self,
        caller: ComputeCaller,
        *,
        profile: str,
        region: str | None,
        ttl_hours: int | None,
        approval_key: str,
        source_metadata: dict[str, str],
    ) -> ComputeSession:
        if not self.settings.enabled:
            raise StoreError("Starting a machine is turned off. Ask an admin.")
        image = self.settings.profiles.get(profile)
        if image is not None and region is None and image.regions:
            # No region given: the profile's first. With Cloud Run set up a profile
            # always names one (`config._cloud_run_profile_error`).
            region = image.regions[0]
        if image is None or region is None or region not in image.regions:
            raise StoreError("Choose an available image profile and region.")
        ttl = self.ttl(ttl_hours)
        session_id = uuid.uuid4()
        # Refuse missing authentication before registering.
        session_token(self.settings, session_id)
        if not approval_key:
            raise StoreError("Owner approval is required")
        now = self.clock()
        session = ComputeSession(
            id=session_id,
            tenant_id=caller.tenant_id,
            owner_account_id=caller.account_id,
            owner_external_user_id=caller.external_user_id,
            deployment_id=self.settings.deployment_id,
            platform=caller.origin.platform,
            parent_channel_id=caller.origin.parent_channel_id,
            thread_id=caller.origin.thread_id,
            agent_id=caller.origin.responder_ma_agent_id,
            agent_name=caller.origin.responder_name,
            source_metadata=source_metadata,
            request_key=approval_key,
            app_name=app_name(self.settings.deployment_id, session_id),
            profile=profile,
            image=image.image,
            region=region,
            cpus=image.cpus,
            memory_mb=image.memory_mb,
            ttl_hours=ttl,
            expires_at=now + timedelta(hours=ttl),
            created_at=now,
        )
        async with self.sm.begin() as db:
            await self._origin(db, caller)
            session = await store.register(db, session, cap=self.settings.per_person_sessions)
        # Registration commits before any external mutation. A lost response is
        # recovered by the deterministic app name, not by launching a second app.
        waited_until = self.clock() + _LAUNCH_WAIT
        # A provider call that hangs must not hold the launch past its wait either.
        give_up_at = asyncio.get_running_loop().time() + _LAUNCH_WAIT.total_seconds()
        current = session
        while True:
            failed = False
            try:
                # One short transaction a read: no row lock is held while the launch waits.
                async with asyncio.timeout_at(give_up_at), self.sm.begin() as db:
                    read = await self._owned(db, caller, session.id)
                    settled_failed = False
                    if read.state == "launching" and read.expires_at <= self.clock():
                        read = read.model_copy(update={"state": "stopping"})
                        await store.save(db, read)
                    elif read.state == "launching":
                        read = await self._settle(db, read, tell_owner=False)
                        settled_failed = read.state == "stopping"
            except TimeoutError:
                # The read rolled back, so `current` keeps the last committed state.
                # `ensure` is safe to repeat: the next call or the scheduler moves
                # the session on.
                break
            # Only a committed read is reported or acted on.
            current, failed = read, settled_failed
            if current.state != "launching" or self.clock() >= waited_until:
                break
            await self.sleep(_START_POLL_SECONDS)
        if failed:
            await self._remove_failed(current)
            raise StoreError(START_FAILED)
        # Still `launching` here means the machine needs longer: the caller is told
        # so, and the next call or the scheduler moves the session on.
        return current

    async def _settle(
        self, db: AsyncSession, session: ComputeSession, *, tell_owner: bool
    ) -> ComputeSession:
        """Move a launching session on by what the provider reports, and save it.

        The only place a session becomes `running`: after the provider says the
        machine has started. A machine that failed, or has not started within
        `_START_DEADLINE`, ends the session as `stopping` for cleanup. Anything else
        leaves it `launching`. `ensure` creates the machine when it is missing, so an
        unknown create outcome is recovered here and never sent twice. `tell_owner`
        queues the one notice for a failure nobody is in the call to hear about.
        """
        resource = await self.provider.ensure(session, session_token(self.settings, session.id))
        readiness = machine_readiness(resource.machine_state)
        state = session.state
        if readiness == "ready":
            state = "running"
        elif readiness == "failed" or self.clock() - session.created_at > _START_DEADLINE:
            state = "stopping"
        settled = session.model_copy(
            update={"state": state, "machine_id": resource.machine_id or session.machine_id}
        )
        await store.save(db, settled)
        if state == "stopping" and tell_owner:
            await store.start_failed(db, settled, self.clock())
        return settled

    async def _remove_failed(self, session: ComputeSession) -> None:
        """Delete a failed start's resource now; the scheduler retries what this cannot."""
        try:
            await self._delete(session)
        except ComputeProviderError:
            log.warning("compute.failed_start_delete_retry", session_id=str(session.id))

    async def _started(
        self,
        caller: ComputeCaller,
        session_id: uuid.UUID,
        *,
        refuse_starting: bool = True,
        read: bool = False,
        act: bool = False,
    ) -> None:
        """Let a call through only to a machine that has started.

        A session that is still `launching` is moved on first. If its machine is not
        ready the caller is told so in plain words and nothing is sent to it, so no
        command or background task is started on a machine that cannot answer.
        `refuse_starting` is off for a status read, which reports the wait instead.
        """
        async with self.sm.begin() as db:
            session = await self._owned(db, caller, session_id, read=read, act=act)
            if session.state != "launching" or session.expires_at <= self.clock():
                return
            session = await self._settle(db, session, tell_owner=False)
        if session.state == "stopping":
            await self._remove_failed(session)
            raise StoreError(START_FAILED)
        if session.state == "launching" and refuse_starting:
            raise StoreError(STILL_STARTING)

    async def extend(
        self, caller: ComputeCaller, session_id: uuid.UUID, *, hours: int, approval_key: str
    ) -> ComputeSession:
        ttl = self.ttl(hours)
        if not approval_key:
            raise StoreError("Owner approval is required")
        await self._started(caller, session_id)
        async with self.sm.begin() as db:
            session = await self._owned(db, caller, session_id)
            if session.state != "running" or session.expires_at <= self.clock():
                raise StoreError("The session is stopping or expired. Launch a new session.")
            if approval_key not in session.extension_keys:
                session = session.model_copy(
                    update={
                        "expires_at": self.clock() + timedelta(hours=ttl),
                        "warned_for": None,
                        "extension_keys": [*session.extension_keys, approval_key],
                    }
                )
                await store.save(db, session)
            return session

    async def stop(self, caller: ComputeCaller, session_id: uuid.UUID) -> ComputeSession:
        async with self.sm.begin() as db:
            session = await self._owned(db, caller, session_id, stop=True, act=True)
            session = await note_act(db, caller.account_id, caller.origin, session)
            if session.state != "deleted":
                session = session.model_copy(update={"state": "stopping"})
                await store.save(db, session)
            if session.owner_account_id == caller.account_id and made_here(session, caller.origin):
                await self._stopped_by_owner(db, session)
        # Stopping intent survives failed provider calls and process restarts.
        await self._delete(session)
        async with self.sm.begin() as db:
            result = await store.get(db, caller.tenant_id, session_id)
            assert result is not None
            return result

    async def _stopped_by_owner(self, db: AsyncSession, session: ComputeSession) -> None:
        """The owner present in the session's own conversation, or its wake turn, stopped
        the machine, so that conversation knows: each command still running ends as
        cancelled with no message, and a message nobody claimed yet for one that ended is
        withdrawn (`_seen`). Under the session lock the stop took. A stop from another
        of the owner's conversations, an admin's emergency stop and expiry keep one
        message per command: the session's conversation did not see it."""
        now = self.clock()
        for task in await task_store.list_for_session(db, session.tenant_id, session.id):
            if task.state in ACTIVE_TASK_STATES:
                await task_store.finish(
                    db,
                    task,
                    session,
                    state="cancelled",
                    now=now,
                    wake=False,
                    run_limit=self.settings.wake_run_limit,
                )
            else:
                await self._seen(db, task)

    async def _delete(self, session: ComputeSession) -> None:
        async with self.sm.begin() as db:
            current = await store.get(db, session.tenant_id, session.id, lock=True)
            if current is None or current.state == "deleted":
                return
            if current.state != "stopping":
                raise StoreError("Session is not marked for deletion")
            await self.provider.delete(
                ComputeResource(
                    app_name=current.app_name,
                    deployment_id=current.deployment_id,
                    session_id=current.id,
                    machine_id=current.machine_id,
                    region=current.region,
                )
            )
            await store.save(db, current.model_copy(update={"state": "deleted"}))

    async def get(
        self, caller: ComputeCaller, session_id: uuid.UUID, *, act: bool = False
    ) -> ComputeSession:
        """The session, for a turn that may read it; with ``act``, one that may also act
        on it, so a refusal comes before anything else the call does."""
        async with self.sm.begin() as db:
            return await self._owned(db, caller, session_id, read=not act, act=act)

    async def status(self, caller: ComputeCaller, session_id: uuid.UUID) -> ComputeStatus:
        """The session and its machine as they are now.

        A session still `launching` is moved on first, so a status never shows
        `running` for a machine that has not started, and shows it as soon as it has.
        """
        await self._started(caller, session_id, refuse_starting=False, read=True)
        async with self.sm.begin() as db:
            session = await self._owned(db, caller, session_id, read=True)
            resource = await self.provider.inspect(session)
            return ComputeStatus(**session.model_dump(), machine_state=resource.machine_state)

    async def list(self, caller: ComputeCaller) -> list[ComputeSession]:
        async with self.sm.begin() as db:
            await self._origin(db, caller)
            sessions = await store.list_sessions(db, caller.tenant_id, owner=caller.account_id)
            # Every conversation's: the owner sees all their machines and may stop any.
            return [
                s
                for s in sessions
                if s.deployment_id == self.settings.deployment_id and s.state != "deleted"
            ]

    async def call(
        self,
        caller: ComputeCaller,
        session_id: uuid.UUID,
        *,
        tool: str,
        arguments: dict[str, JsonValue],
    ) -> JsonValue:
        # A command is a task (`run`); its start and probes call the machine's run_bash.
        if tool not in {"write_file"} | READ_OPERATIONS:
            raise StoreError("Unsupported session operation")
        read = tool in READ_OPERATIONS
        await self._started(caller, session_id, read=read, act=not read)
        async with self.sm.begin() as db:
            session = await self._owned(db, caller, session_id, read=read, act=not read)
            if not read:
                session = await note_act(db, caller.account_id, caller.origin, session)
            if session.state != "running" or session.expires_at <= self.clock():
                raise StoreError("The session is expired or stopping. Launch a new session.")
            result = await self.provider.call(
                session, session_token(self.settings, session.id), tool, arguments
            )
            if not read:
                await store.save(
                    db,
                    session.model_copy(
                        update={
                            "expires_at": self.clock()
                            + timedelta(hours=min(session.ttl_hours, self.settings.max_ttl_hours)),
                            "warned_for": None,
                        }
                    ),
                )
            return result

    async def reconcile(self) -> None:
        if not self.settings.deployment_id:
            return
        # Inventory precedes the registration snapshot: every app's intent must
        # have committed before its creation, including concurrent launches.
        try:
            resources = await self.provider.inventory()
        except Exception:
            # Listing failures, including timeouts, must not prevent known-row cleanup.
            # Never log remote exception bodies or headers.
            resources = []
            log.warning("compute.inventory_retry")
        known: set[uuid.UUID] = set()
        for tenant in await list_tenants_by_platform(self.sm):
            async with self.sm.begin() as db:
                sessions = await store.list_sessions(db, tenant.id, include_deleted=False)
            for session in sessions:
                if session.deployment_id != self.settings.deployment_id:
                    continue
                known.add(session.id)
                try:
                    await self._reconcile_one(session, archived=tenant.archived_at is not None)
                except Exception:
                    # Never log remote exceptions: requests may include credentials.
                    log.warning("compute.reconcile_retry", session_id=str(session.id))
        for resource in resources:
            if (
                resource.deployment_id == self.settings.deployment_id
                and resource.session_id not in known
            ):
                try:
                    await self.provider.delete(resource)
                except ComputeProviderError:
                    log.warning("compute.orphan_delete_retry", session_id=str(resource.session_id))

    async def _allowed(self, db: AsyncSession, session: ComputeSession) -> bool | None:
        """Whether a session still waiting to launch may: the deployment switch and the
        `compute` feature at its place (`store.feature_place`). Off here stops it instead of
        provisioning; None when a feature cannot be read, so this tick leaves the session
        as it is. An agent-chat session's machine also needs `compute_agent_chat`, and is
        stopped when its session's record is not found."""
        if not self.settings.enabled:
            return False
        place = await store.feature_place(db, session)
        if place is None:
            return False
        found = [await feature_in_or_none(db, None, place, "compute")]
        if store.in_agent_chat(session):
            found.append(await feature_in_or_none(db, None, place, "compute_agent_chat"))
        if any(feature is not None and not feature.on for feature in found):
            return False
        return None if None in found else True

    async def _reconcile_one(self, session: ComputeSession, *, archived: bool) -> None:
        async with self.sm.begin() as db:
            current = await store.get(
                db, session.tenant_id, session.id, lock=True, skip_locked=True
            )
            if current is None or current.state == "deleted":
                return
            now = self.clock()
            allowed: bool | None = True
            if current.state == "launching" and not archived and current.expires_at > now:
                allowed = await self._allowed(db, current)
                if allowed is None:
                    return
            if (
                archived
                or current.expires_at <= now
                or current.state == "stopping"
                or (current.state == "launching" and not allowed)
            ):
                current = current.model_copy(update={"state": "stopping"})
                await store.save(db, current)
            elif current.state == "launching":
                # Unknown create outcomes are never blindly re-POSTed. ensure()
                # recovers an existing machine by its deterministic app identity.
                # Nobody is in this call, so a failed start is told to the owner.
                current = await self._settle(db, current, tell_owner=True)
            elif (
                current.expires_at - now <= timedelta(hours=min(1, current.ttl_hours / 4))
                and current.warned_for != current.expires_at
            ):
                await store.warn(db, current, now)
        if current.state == "stopping":
            await self._delete(current)

    def _task_timeout(self, minutes: int | None) -> int:
        limit = self.settings.max_ttl_hours * 60
        value = min(8 * 60, limit) if minutes is None else minutes
        if not 1 <= value <= limit:
            raise StoreError(f"A command can run for 1 to {limit} minutes.")
        return value

    async def start_task(
        self,
        caller: ComputeCaller,
        session_id: uuid.UUID,
        *,
        command: str,
        timeout_minutes: int | None,
        request_key: str,
        timeout_seconds: int | None = None,
        extra_cap: int = 0,
        wait_seconds: float | None = None,
    ) -> ComputeTask:
        """Start a command once per `request_key`, detached on the machine.

        `timeout_seconds`, when given, ends the command after that many seconds in
        place of `timeout_minutes`. `extra_cap` raises the session's task limit for
        this start. `wait_seconds` says its caller waits that long for it to end: the
        row's `wait_until` keeps the scheduler away until the wait, the start call and
        `_INLINE_HANDOVER` have passed.
        """
        if not request_key:
            raise StoreError("Run the command from the owner's conversation.")
        if timeout_seconds is None:
            minutes = self._task_timeout(timeout_minutes)
            seconds = minutes * 60
        else:
            # The stored row counts whole minutes; less than 1 second rounds to 0, refused.
            minutes = self._task_timeout(ceil(timeout_seconds / 60))
            seconds = timeout_seconds
        # Before the task row exists: a task registered for a machine that cannot
        # answer would be reported lost.
        await self._started(caller, session_id, act=True)
        async with self.sm.begin() as db:
            session = await self._owned(db, caller, session_id, act=True)
            session = await note_act(db, caller.account_id, caller.origin, session)
            now = self.clock()
            if session.state != "running" or session.expires_at <= now:
                raise StoreError("The session is expired or stopping. Launch a new session.")
            task_id = uuid.uuid4()
            # The start call may take its whole limit before the caller starts to wait.
            held = _START_CALL_SECONDS + _INLINE_HANDOVER + (wait_seconds or 0)
            task, created = await task_store.register(
                db,
                ComputeTask(
                    id=task_id,
                    tenant_id=session.tenant_id,
                    session_id=session.id,
                    owner_account_id=caller.account_id,
                    request_key=request_key,
                    command_preview=command[: scripts.PREVIEW_CHARS],
                    timeout_minutes=minutes,
                    deadline_at=now + timedelta(seconds=seconds),
                    output_path=scripts.output_path(task_id),
                    created_at=now,
                    wait_until=None if wait_seconds is None else now + timedelta(seconds=held),
                ),
                cap=self.settings.per_session_tasks + extra_cap,
            )
        if not created:
            # A retried call never starts the command again; the poller settles it.
            return task
        # The row commits before the machine is touched. If this call is lost, the
        # task stays starting and the poller marks it lost unless its pid appears.
        async with self.sm.begin() as db:
            session = await self._owned(db, caller, session_id, act=True)
            # Again here: an owner act since the registration may have emptied the list.
            session = await note_act(db, caller.account_id, caller.origin, session)
            if session.state != "running" or session.expires_at <= self.clock():
                raise StoreError("The session is expired or stopping. Launch a new session.")
            scripts.parse_start(
                await self.provider.call(
                    session,
                    session_token(self.settings, session.id),
                    "run_bash",
                    {
                        "command": scripts.start_script(task.id, command, timeout_s=seconds),
                        "timeout_s": _START_CALL_SECONDS,
                    },
                )
            )
            await self._renew_for(db, session, task)
            return await task_store.advance(db, task, state="running") or task

    async def run(
        self,
        caller: ComputeCaller,
        session_id: uuid.UUID,
        *,
        command: str,
        wait_seconds: float,
        timeout_seconds: int | None,
        timeout_minutes: int | None,
        request_key: str,
    ) -> InlineRun:
        """`_run`, then, as its last step, an end another poll settled is marked seen: the
        caller is told the command ended (`tools.compute._ENDED`)."""
        ran = await self._run(
            caller,
            session_id,
            command=command,
            wait_seconds=wait_seconds,
            timeout_seconds=timeout_seconds,
            timeout_minutes=timeout_minutes,
            request_key=request_key,
        )
        if not ran.ended_here:
            await self.mark_seen(caller, ran.task)
        return ran

    async def _run(
        self,
        caller: ComputeCaller,
        session_id: uuid.UUID,
        *,
        command: str,
        wait_seconds: float,
        timeout_seconds: int | None,
        timeout_minutes: int | None,
        request_key: str,
    ) -> InlineRun:
        """Start a command and wait up to `wait_seconds` for it to end.

        A command whose end this call settles is returned with `ended_here`, and queues
        no wake: the caller sees it. A command that needs longer gets its end as a
        message in the conversation. One another poll settled is returned ended, and
        `run` withdraws its message: the caller is told it ended. Who settles is decided
        under the session lock (`_poll_task`). `start_task` never settles a task; a
        retried background key can return one that ended elsewhere.

        Locks: `start_task` takes the session lock; its time counts against the wait, but
        it is never cut short. Each poll takes the lock within the deadline; once a poll
        settles the task, nothing else runs. The hand-over writes only the task row.
        """
        # The wait starts now: a slow start uses part of it. A probe or a lock wait can take
        # far longer than the wait (the transport allows 50 s), so each poll is bounded by
        # this deadline on the loop's clock. The start itself is not: a cancelled start
        # would leave a command nobody knows the state of.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, wait_seconds)
        give_up = self.clock() + timedelta(seconds=wait_seconds)
        task = await self.start_task(
            caller,
            session_id,
            command=command,
            timeout_minutes=timeout_minutes,
            timeout_seconds=timeout_seconds,
            request_key=request_key,
            # Only a caller who waits gets the extra room: a background start keeps the cap.
            extra_cap=_INLINE_EXTRA_TASKS if wait_seconds > 0 else 0,
            wait_seconds=wait_seconds,
        )
        # A row with no `wait_until` (a retried background start) is the scheduler's.
        give_up = (
            min(give_up, task.wait_until - timedelta(seconds=_INLINE_HANDOVER))
            if task.wait_until is not None
            else self.clock()
        )
        polls = iter(_INLINE_POLLS)
        while (
            task.state in ACTIVE_TASK_STATES and self.clock() < give_up and loop.time() < deadline
        ):
            await self.sleep(min(next(polls, 2.0), (give_up - self.clock()).total_seconds()))
            try:
                polled = await self._poll_task(
                    task.tenant_id, task.id, wake=False, inline=True, deadline=deadline
                )
            except TimeoutError:
                # Rolled back before any write: this caller settled nothing.
                break
            except SQLAlchemyError:
                # Nothing committed. The scheduler takes it over after `wait_until`.
                log.warning("compute.inline_poll_retry", task_id=str(task.id))
                return InlineRun(task, ended_here=False)
            if polled.task is None:
                break
            if polled.settled:
                # Saved with no wake, so the caller gets it now: nothing may wait or fail
                # after this, and no second read happens.
                return InlineRun(polled.task, ended_here=True)
            task = polled.task
        return InlineRun(await self._hand_over(task), ended_here=False)

    async def _hand_over(self, task: ComputeTask) -> ComputeTask:
        """Let the scheduler poll a task its caller stopped waiting for, from now on.

        One short transaction on the task row alone: it never waits for the session
        lock, which a slow probe can hold. Its caller already proved it owns the task.
        """
        try:
            async with self.sm.begin() as db:
                current = await task_store.get(db, task.tenant_id, task.id)
                if (
                    current is not None
                    and current.state in ACTIVE_TASK_STATES
                    and current.wait_until is not None
                ):
                    handed = await task_store.advance(db, current, wait_until=None)
                    current = handed or await task_store.get(db, task.tenant_id, task.id)
                return current or task
        except SQLAlchemyError:
            log.warning("compute.inline_handover_retry", task_id=str(task.id))
            return task

    async def _renew_for(
        self, db: AsyncSession, session: ComputeSession, task: ComputeTask
    ) -> None:
        """Keep the lease alive for a running task, never past max TTL from its start."""
        now = self.clock()
        ceiling = task.created_at + timedelta(hours=self.settings.max_ttl_hours)
        target = min(now + timedelta(hours=session.ttl_hours), ceiling)
        if target > session.expires_at:
            await store.save(
                db, session.model_copy(update={"expires_at": target, "warned_for": None})
            )

    async def _owned_task(
        self, db: AsyncSession, caller: ComputeCaller, task_id: uuid.UUID
    ) -> ComputeTask:
        task = await task_store.get(db, caller.tenant_id, task_id)
        if task is None or task.owner_account_id != caller.account_id:
            raise StoreError("Command not found")
        # The session check also proves the same owner and the same conversation.
        await self._owned(db, caller, task.session_id, read=True)
        return task

    async def task_status(
        self,
        caller: ComputeCaller,
        task_id: uuid.UUID,
        *,
        wait_seconds: float = 0,
        deadline: float | None = None,
    ) -> ComputeTask:
        """`_task_status`, then, as its last step, an end another poll settled is marked
        seen. One this call settled itself queued no message, so it returns at once, with
        no transaction after the settling poll."""
        task, settled_here = await self._task_status(
            caller, task_id, wait_seconds=wait_seconds, deadline=deadline
        )
        if not settled_here:
            await self.mark_seen(caller, task)
        return task

    async def _task_status(
        self,
        caller: ComputeCaller,
        task_id: uuid.UUID,
        *,
        wait_seconds: float = 0,
        deadline: float | None = None,
    ) -> tuple[ComputeTask, bool]:
        """Read a task and, while it is active, probe the machine until it ends or
        `wait_seconds` pass, with whether this call settled it. A task that already ended
        returns at once.

        Only a turn of the session's own conversation gets here (the owner present, or
        the session's own wake turn), so an end it reads is the conversation's: no wake
        is queued for it, and `task_status` withdraws an unclaimed one. While an inline
        caller still owns the task, a probe only reads it. Like a poll, a probe that
        finds the command running keeps the lease alive.

        One deadline on the event loop's clock, taken when the call starts unless the
        caller passes the one its own call started (`deadline`), bounds every step before
        a write: the one authorization read, and each poll's reads, lock wait
        and probe. A step cut short settles nothing: the last state read is returned, or
        the plain refusal when nothing was read yet. With no wait there is one probe and
        no deadline, as before. A database error before the first probe succeeded is the
        plain refusal; after one, the state read then is returned. A probe that settles
        the command returns it at once, with no read after it. A write, its commit and
        the kill of an overdue command are never cut short, so the call can run a little
        longer than `wait_seconds` when the machine or the database is slow.
        """
        wait_seconds = max(0.0, wait_seconds)
        loop = asyncio.get_running_loop()
        if deadline is None and wait_seconds:
            deadline = loop.time() + wait_seconds
        give_up = self.clock() + timedelta(seconds=wait_seconds)
        try:
            async with self.sm.begin() as db:
                async with asyncio.timeout_at(deadline):
                    task = await self._owned_task(db, caller, task_id)
        except (TimeoutError, SQLAlchemyError):
            log.warning("compute.task_status_retry", task_id=str(task_id))
            raise StoreError(STATUS_REFUSED) from None
        if task.state not in ACTIVE_TASK_STATES:
            return task, False
        probed = False
        while True:
            try:
                polled = await self._poll_task(
                    task.tenant_id, task.id, wake=False, deadline=deadline
                )
            except TimeoutError:
                # Rolled back before any write: this call settled nothing.
                return task, False
            except SQLAlchemyError:
                # Never relay database errors; the scheduler keeps polling.
                log.warning("compute.task_status_retry", task_id=str(task.id))
                if not probed:
                    raise StoreError(STATUS_REFUSED) from None
                return task, False
            if polled.task is None:
                return task, False
            task = polled.task
            if polled.settled:
                # This call saved the end: its caller gets it now, with no read after it.
                return task, True
            probed = True
            left = (give_up - self.clock()).total_seconds()
            if (
                task.state not in ACTIVE_TASK_STATES
                or left <= 0
                or (deadline is not None and loop.time() >= deadline)
            ):
                return task, False
            await self.sleep(min(_STATUS_POLL_SECONDS, left))

    async def task_result(self, caller: ComputeCaller, task_id: uuid.UUID) -> ComputeTask:
        """A task, read by a turn of its session's own conversation (`_owned_task`). It
        withdraws nothing: a caller that answers with it calls `mark_seen` last."""
        async with self.sm.begin() as db:
            return await self._owned_task(db, caller, task_id)

    async def mark_seen(self, caller: ComputeCaller, task: ComputeTask) -> None:
        """The last step before a turn of the session's own conversation is answered with
        this finished task: withdraw the message that would tell the conversation again.

        Only once every check passed and the result is in hand, in a transaction of its
        own. A failure is logged and never fails the answer: a late extra message is
        better than a lost one. `caller` must have read the task (`task_result`)."""
        if task.state in ACTIVE_TASK_STATES or task.owner_account_id != caller.account_id:
            return
        try:
            # Bounded, so a slow database never holds an answer already in hand. Not
            # shielded: a request cancelled here cannot answer anyway, and the withdrawal
            # rolls back with it, so the message stays.
            async with asyncio.timeout(_WITHDRAW_SECONDS), self.sm.begin() as db:
                await self._seen(db, task)
        except Exception:
            log.warning("compute.wake_withdraw_retry", task_id=str(task.id))

    async def _seen(self, db: AsyncSession, task: ComputeTask) -> None:
        """Withdraw a finished task's message nobody claimed yet: the one rule for a
        conversation that already has the end (`mark_seen`, and the owner's stop). A
        claimed message (its turn is starting or running) is left alone, and a withdrawn
        one never runs, so it is no wake turn."""
        if task.state not in ACTIVE_TASK_STATES:
            await task_store.withdraw_wake(db, task)

    async def poll_tasks(self) -> None:
        """One scheduler pass over active tasks; each task's failure is isolated."""
        if not self.settings.deployment_id:
            return
        for tenant in await list_tenants_by_platform(self.sm):
            try:
                async with self.sm.begin() as db:
                    tasks = await task_store.list_active(db, tenant.id, now=self.clock())
            except Exception:
                # One tenant's read failure must not stop the pass for the others.
                log.warning("compute.task_list_retry", tenant_id=str(tenant.id))
                continue
            for task in tasks:
                try:
                    await self._poll_task(task.tenant_id, task.id, wake=True)
                except Exception:
                    # Never log remote exceptions: requests may include credentials.
                    log.warning("compute.task_poll_retry", task_id=str(task.id))

    async def _poll_task(
        self,
        tenant_id: uuid.UUID,
        task_id: uuid.UUID,
        *,
        wake: bool,
        inline: bool = False,
        deadline: float | None = None,
    ) -> Polled:
        """Probe an active task once, and settle it when it ended; say whether this call did.

        The task it returns is the one its transaction committed, so a caller that settled
        it needs no second read.

        Under the session lock, one side owns a task its caller waits for. The `inline`
        caller may settle it only while `wait_until` is more than `_INLINE_HANDOVER`
        seconds ahead. The scheduler and a status read may settle it only once
        `wait_until` is empty or past. Outside its own window a call does nothing, so the
        caller and the conversation's message never both report one end.

        `deadline`, on the event loop's clock, bounds the reads, the wait for the session
        lock and the probe. All come before any write, so a `TimeoutError` settles
        nothing. The writes, their commit and the kill of an overdue command are never
        cut short.
        """
        async with self.sm.begin() as db:
            async with asyncio.timeout_at(deadline):
                task = await task_store.get(db, tenant_id, task_id)
                if task is None or task.state not in ACTIVE_TASK_STATES:
                    return Polled(task)
                # The session lock serializes with stop and deletion. The poller skips
                # a session busy with an inline call and returns next pass.
                session = await store.get(
                    db, tenant_id, task.session_id, lock=True, skip_locked=wake
                )
                if session is None:
                    return Polled(task)
                # Re-read under the lock: another poll may have settled it meanwhile.
                task = await task_store.get(db, tenant_id, task_id)
                if task is None or task.state not in ACTIVE_TASK_STATES:
                    return Polled(task)
            now = self.clock()
            held = task.wait_until
            if inline:
                owns = held is not None and now < held - timedelta(seconds=_INLINE_HANDOVER)
            else:
                owns = held is None or now >= held
            if not owns:
                return Polled(task)
            limit = self.settings.wake_run_limit
            if (
                session.state != "running"
                or session.expires_at <= now
                or session.deployment_id != self.settings.deployment_id
            ):
                done = await task_store.finish(
                    db, task, session, state="cancelled", now=now, wake=wake, run_limit=limit
                )
                return Polled(done or task, settled=done is not None)
            token = session_token(self.settings, session.id)
            overdue = now > task.deadline_at + _DEADLINE_GRACE
            try:
                async with asyncio.timeout_at(deadline):
                    answer = await self.provider.call(
                        session,
                        token,
                        "run_bash",
                        {"command": scripts.probe_script(task.id), "timeout_s": 20},
                    )
                probe = scripts.parse_probe(answer)
            except ComputeProviderError:
                if not overdue:
                    return Polled(task)
                done = await task_store.finish(
                    db, task, session, state="lost", now=now, wake=wake, run_limit=limit
                )
                return Polled(done or task, settled=done is not None)
            if probe.status == "exit":
                assert probe.exit_code is not None
                state = (
                    "succeeded"
                    if probe.exit_code == 0
                    else "timed_out"
                    if probe.exit_code == 124 or now >= task.deadline_at
                    else "failed"
                )
                done = await task_store.finish(
                    db,
                    task,
                    session,
                    state=state,
                    now=now,
                    wake=wake,
                    run_limit=limit,
                    exit_code=probe.exit_code,
                    output_bytes=probe.output_bytes,
                    output_tail=probe.tail,
                )
            elif probe.status == "running" and overdue:
                try:
                    await self.provider.call(
                        session,
                        token,
                        "run_bash",
                        {"command": scripts.kill_script(task.id), "timeout_s": 20},
                    )
                except ComputeProviderError:
                    # Settle anyway: the task is past its deadline, and deleting the
                    # machine at lease end is the final kill.
                    log.warning("compute.task_kill_retry", task_id=str(task.id))
                done = await task_store.finish(
                    db,
                    task,
                    session,
                    state="timed_out",
                    now=now,
                    wake=wake,
                    run_limit=limit,
                    output_bytes=probe.output_bytes,
                    output_tail=probe.tail,
                )
            elif probe.status == "running" or (
                probe.status == "missing"
                and task.state == "starting"
                and now - task.created_at < _START_GRACE
            ):
                await self._renew_for(db, session, task)
                if probe.status == "running":
                    task = (
                        await task_store.advance(
                            db,
                            task,
                            state="running",
                            output_bytes=probe.output_bytes,
                            output_tail=probe.tail,
                        )
                        or task
                    )
                return Polled(task)
            else:
                # The process ended without an exit file (machine restart) or never
                # started. It is reported, never run again. A probe that finds no
                # files at all has no output to report: the machine was replaced, so
                # the owner keeps the output the last poll saved.
                kept = probe.status == "missing"
                done = await task_store.finish(
                    db,
                    task,
                    session,
                    state="lost",
                    now=now,
                    wake=wake,
                    run_limit=limit,
                    output_bytes=None if kept else probe.output_bytes,
                    output_tail=None if kept else probe.tail,
                )
            return Polled(done or task, settled=done is not None)
