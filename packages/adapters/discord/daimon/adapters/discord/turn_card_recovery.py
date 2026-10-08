"""Post durable initial turn cards and find them in Discord history.

The lookup reports ambiguity and incomplete history reads explicitly. It does
not decide whether a missing card should be posted or whether a found card
should be edited.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

import structlog
from daimon.adapters.discord.lifecycle import DiscordTurnLifecycle
from daimon.adapters.discord.post_transport import DiscordPostTransport
from daimon.core.stores.domain import TurnCardIntentRow
from daimon.core.stores.turn_card_intents import (
    create_turn_card_intent,
    mark_turn_card_intent_unrecoverable,
    record_turn_card_message,
    retire_turn_card_intent,
)
from daimon.core.turn.bookkeeping import reconcile_found_card
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import discord
from discord.components import ActionRow, Button

log = structlog.get_logger()


class UnrecoverableTurnCardError(Exception):
    """A definite platform failure prevents editing this turn's pending card."""

    def __init__(self, reason: str, message_ids: set[int] | None = None) -> None:
        super().__init__(reason)
        self.message_ids = set(message_ids or ())


def is_definite_recovery_failure(err: BaseException) -> bool:
    """Distinguish missing identity or permissions from retryable API failures."""
    if isinstance(err, discord.HTTPException) and (err.status == 403 or err.code in (10003, 10015)):
        return True
    if isinstance(err, discord.ClientException) and str(err) in (
        "own webhook token unavailable",
        "own webhook no longer exists",
    ):
        return True
    return err.__cause__ is not None and is_definite_recovery_failure(err.__cause__)


async def post_initial_turn_card(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    tenant_id: UUID,
    thread_id: str,
    make_lifecycle: Callable[
        [UUID, Callable[[discord.Message], Awaitable[None]]], DiscordTurnLifecycle
    ],
) -> tuple[TurnCardIntentRow, DiscordTurnLifecycle]:
    """Commit a durable intent before posting, then persist Discord's response ID.

    If Discord accepts the post but the response is lost, or the message-ID
    commit fails, the prepared row remains for later history lookup. The caller
    must not start an explicitly prompted MA turn unless this function returns
    successfully. Unprompted turns may defer their first visible post until the
    render loop; that post still commits its response ID before the render hook
    returns.
    """
    intent_started = time.perf_counter()
    async with sessionmaker() as session:
        intent = await create_turn_card_intent(
            session,
            tenant_id=tenant_id,
            platform="discord",
            thread_id=thread_id,
            turn_token=uuid4(),
        )
        await session.commit()
    log.info(
        "turn.card_intent_committed",
        tenant_id=str(tenant_id),
        thread_id=thread_id,
        intent_id=str(intent.id),
        commit_ms=round((time.perf_counter() - intent_started) * 1000, 1),
    )

    async def record_posted_message(message: discord.Message) -> None:
        async with sessionmaker() as session:
            recorded = await record_turn_card_message(
                session, id=intent.id, message_id=str(message.id)
            )
            if not recorded:
                raise RuntimeError(
                    "Discord initial turn card intent no longer accepts its message ID"
                )
            await session.commit()

    lifecycle = make_lifecycle(intent.id, record_posted_message)
    await lifecycle.post_initial()
    return intent, lifecycle


async def retire_terminal_turn_card(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    intent_id: UUID,
    expected_message_id: str | None,
    no_post_confirmed: bool = False,
    allow_prepared_without_message: bool = False,
) -> bool:
    """Retire by response ID, or explicitly permit retirement of a prepared NULL-ID row.

    `no_post_confirmed` is reserved for callers that know they never attempted
    the send. Boot recovery uses `allow_prepared_without_message` only after
    two complete no-match reads separated by a monotonic minute.
    """
    if expected_message_id is None and not (no_post_confirmed or allow_prepared_without_message):
        return False
    try:
        async with sessionmaker() as session:
            retired = await retire_turn_card_intent(
                session, id=intent_id, expected_message_id=expected_message_id
            )
            await session.commit()
        return retired
    except SQLAlchemyError:
        log.warning(
            "turn.card_intent_retire_failed",
            intent_id=str(intent_id),
            message_id=expected_message_id,
            exc_info=True,
        )
        return False


async def expire_unrecoverable_turn_card(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    intent: TurnCardIntentRow,
    thread: discord.Thread | None,
    max_age_s: int,
    reason: str,
    candidate_message_ids: set[int] | None = None,
    allow_missing_member: bool = False,
    now: datetime | None = None,
) -> bool:
    """Close an aged intent after a definite failure; delete only its pending cards."""
    if not reason:
        raise ValueError("unrecoverable reason is required")
    current_time = now or datetime.now(UTC)
    if (current_time - intent.created_at).total_seconds() < max_age_s:
        return False
    if (
        thread is not None
        and thread.guild.me is None  # pyright: ignore[reportUnnecessaryComparison]
        and not allow_missing_member
    ):
        log.warning("turn.card_intent_unrecoverable_deferred", intent_id=str(intent.id))
        return False
    try:
        async with sessionmaker() as session:
            marked = await mark_turn_card_intent_unrecoverable(
                session,
                id=intent.id,
                cutoff=current_time - timedelta(seconds=max_age_s),
            )
            await session.commit()
    except SQLAlchemyError:
        log.warning(
            "turn.card_intent_unrecoverable_record_failed",
            intent_id=str(intent.id),
            exc_info=True,
        )
        return False
    if not marked:
        return False

    deleted = 0
    if thread is not None:
        member = thread.guild.me
        if member is not None and thread.permissions_for(member).manage_messages:  # pyright: ignore[reportUnnecessaryComparison]
            ids = set(candidate_message_ids or ())
            if intent.message_id is not None:
                try:
                    ids.add(int(intent.message_id))
                except ValueError:
                    log.warning(
                        "turn.card_intent_stale_delete_failed",
                        intent_id=str(intent.id),
                        message_id=intent.message_id,
                        error="invalid message ID",
                    )
            for message_id in sorted(ids):
                try:
                    message = await thread.fetch_message(message_id)
                    if intent.id not in turn_card_ids_from_message(message):
                        continue  # the recorded message may now contain the answer
                    await message.delete()
                    deleted += 1
                except discord.NotFound as err:
                    if err.code != 10008:
                        log.warning(
                            "turn.card_intent_stale_delete_failed",
                            intent_id=str(intent.id),
                            message_id=str(message_id),
                            exc_info=True,
                        )
                except (discord.HTTPException, discord.ClientException, ValueError):
                    log.warning(
                        "turn.card_intent_stale_delete_failed",
                        intent_id=str(intent.id),
                        message_id=str(message_id),
                        exc_info=True,
                    )
    log.warning(
        "turn.card_intent_unrecoverable",
        intent_id=str(intent.id),
        message_id=intent.message_id,
        reason=reason,
        age_s=round((current_time - intent.created_at).total_seconds()),
        cards_deleted=deleted,
    )
    return True


_TURN_CARD_CUSTOM_ID_PREFIX = "daimon:cancel:"


class TurnCardSearchState(StrEnum):
    """Outcome of an exhaustive scan for a turn card in thread history."""

    FOUND = "found"
    NOT_FOUND = "not_found"
    MULTIPLE = "multiple"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class TurnCardSearchResult:
    """Message IDs matching a turn ID and whether the history scan was conclusive."""

    state: TurnCardSearchState
    message_ids: tuple[int, ...]
    definite_failure: bool = False


def turn_card_custom_id(turn_id: UUID) -> str:
    """Encode a durable turn ID in the existing Cancel button's custom ID."""
    return f"{_TURN_CARD_CUSTOM_ID_PREFIX}{turn_id}"


def _turn_id_from_custom_id(custom_id: str | None) -> UUID | None:
    if custom_id is None or not custom_id.startswith(_TURN_CARD_CUSTOM_ID_PREFIX):
        return None
    value = custom_id.removeprefix(_TURN_CARD_CUSTOM_ID_PREFIX)
    try:
        turn_id = UUID(value)
    except ValueError:
        return None
    if str(turn_id) != value:
        return None
    return turn_id


def turn_card_ids_from_message(message: discord.Message) -> frozenset[UUID]:
    """Extract turn IDs from standard action-row buttons on a fetched message."""
    turn_ids: set[UUID] = set()
    for component in message.components:
        if not isinstance(component, ActionRow):
            continue
        for child in component.children:
            if not isinstance(child, Button):
                continue
            turn_id = _turn_id_from_custom_id(child.custom_id)
            if turn_id is not None:
                turn_ids.add(turn_id)
    return frozenset(turn_ids)


async def find_turn_card_message(
    thread: discord.Thread,
    *,
    turn_id: UUID,
    created_after: datetime,
    before: datetime,
    message_budget: int = 1000,
) -> TurnCardSearchResult:
    """Scan a bounded time and message window for this turn ID.

    discord.py turns a datetime bound into a millisecond snowflake. Start one
    second earlier so a card posted in the intent's timestamp bucket is not
    skipped. The turn ID still filters unrelated messages. discord.py paginates
    the finite history iterator. Any HTTP failure
    makes the result indeterminate, even if a matching message was already
    yielded, because unread pages could contain a duplicate. Reaching the
    message budget is also indeterminate: unread messages could contain a
    duplicate.
    """
    if message_budget <= 0:
        raise ValueError("message_budget must be positive")
    if before < created_after:
        return TurnCardSearchResult(
            state=TurnCardSearchState.INDETERMINATE,
            message_ids=(),
        )
    message_ids: set[int] = set()
    messages_read = 0
    try:
        async for message in thread.history(
            after=created_after - timedelta(seconds=1),
            before=before,
            oldest_first=True,
            limit=message_budget,
        ):
            messages_read += 1
            if turn_id in turn_card_ids_from_message(message):
                message_ids.add(message.id)
    except (discord.HTTPException, discord.ClientException, ValueError) as err:
        return TurnCardSearchResult(
            state=TurnCardSearchState.INDETERMINATE,
            message_ids=tuple(sorted(message_ids)),
            definite_failure=is_definite_recovery_failure(err),
        )

    if messages_read == message_budget:
        return TurnCardSearchResult(
            state=TurnCardSearchState.INDETERMINATE,
            message_ids=tuple(sorted(message_ids)),
        )

    ordered_ids = tuple(sorted(message_ids))
    if not ordered_ids:
        return TurnCardSearchResult(state=TurnCardSearchState.NOT_FOUND, message_ids=())
    if len(ordered_ids) == 1:
        return TurnCardSearchResult(state=TurnCardSearchState.FOUND, message_ids=ordered_ids)
    return TurnCardSearchResult(state=TurnCardSearchState.MULTIPLE, message_ids=ordered_ids)


async def reconcile_turn_card_intent(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    intent: TurnCardIntentRow,
    thread: discord.Thread,
    client: discord.Client | None = None,
    restarted: bool = True,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
    candidate_message_ids: set[int] | None = None,
) -> None:
    """Reconcile one snapshotted intent without delaying admission."""
    complete_miss_at: float | None = None
    incomplete_attempts = 0
    while incomplete_attempts < 3:
        current_time = now()
        search_before = min(current_time, intent.created_at + timedelta(minutes=10))
        result = await find_turn_card_message(
            thread,
            turn_id=intent.id,
            created_after=intent.created_at,
            before=search_before,
        )
        if candidate_message_ids is not None:
            candidate_message_ids.update(result.message_ids)
        if result.state in (TurnCardSearchState.FOUND, TurnCardSearchState.MULTIPLE):
            message_ids = set(result.message_ids)

            async def record(message_id: str) -> bool:
                try:
                    async with sessionmaker() as session:
                        recorded = await record_turn_card_message(
                            session, id=intent.id, message_id=message_id
                        )
                        if not recorded:
                            return False
                        await session.commit()
                    return True
                except SQLAlchemyError:
                    log.warning(
                        "turn.card_intent_recovered_id_failed",
                        intent_id=str(intent.id),
                        message_id=message_id,
                        exc_info=True,
                    )
                    return False

            async def edit(message_ids: set[int] = message_ids) -> bool:
                return await _reconcile_matching_messages(
                    thread,
                    client=client,
                    intent_id=intent.id,
                    message_ids=message_ids,
                    known_message_id=intent.message_id,
                    sleep=sleep,
                    restarted=restarted,
                )

            async def retire(message_id: str) -> None:
                await retire_terminal_turn_card(
                    sessionmaker, intent_id=intent.id, expected_message_id=message_id
                )

            try:
                await reconcile_found_card(
                    expected_message_id=intent.message_id,
                    recovered_message_id=(
                        str(min(message_ids)) if intent.message_id is None else intent.message_id
                    ),
                    record=record,
                    edit=edit,
                    retire=retire,
                )
            except UnrecoverableTurnCardError as err:
                err.message_ids.update(message_ids)
                raise
            return
        if result.state is TurnCardSearchState.NOT_FOUND and intent.message_id is not None:
            # The known response may be terminal and therefore absent from the
            # key search; still verify it while retaining the history scan's
            # duplicate coverage.
            if not await _reconcile_matching_messages(
                thread,
                client=client,
                intent_id=intent.id,
                message_ids=set(),
                known_message_id=intent.message_id,
                sleep=sleep,
                restarted=restarted,
            ):
                return
            await retire_terminal_turn_card(
                sessionmaker,
                intent_id=intent.id,
                expected_message_id=intent.message_id,
            )
            return
        if result.state is TurnCardSearchState.NOT_FOUND:
            if complete_miss_at is None:
                complete_miss_at = monotonic()
                await sleep(60.0)
                continue
            seconds_since_miss = monotonic() - complete_miss_at
            if seconds_since_miss < 60.0:
                await sleep(60.0 - seconds_since_miss)
                continue
            await retire_terminal_turn_card(
                sessionmaker,
                intent_id=intent.id,
                expected_message_id=None,
                allow_prepared_without_message=True,
            )
            return

        if result.definite_failure:
            raise UnrecoverableTurnCardError("history access denied", set(result.message_ids))
        incomplete_attempts += 1
        if incomplete_attempts < 3:
            await sleep(5.0)
    log.warning("turn.card_intent_recovery_exhausted", intent_id=str(intent.id))


async def _reconcile_matching_messages(
    thread: discord.Thread,
    *,
    client: discord.Client | None = None,
    intent_id: UUID,
    message_ids: set[int],
    known_message_id: str | None,
    sleep: Callable[[float], Awaitable[None]],
    restarted: bool = True,
) -> bool:
    """Edit every still-live match; return false on any unresolved API failure."""
    if known_message_id is not None:
        message_ids.add(int(known_message_id))
    for message_id in sorted(message_ids):
        for attempt in range(3):
            try:
                message = await thread.fetch_message(message_id)
            except discord.NotFound as err:
                if err.code != 10008 and is_definite_recovery_failure(err):
                    raise UnrecoverableTurnCardError(str(err), message_ids) from err
                break
            except (discord.HTTPException, discord.ClientException, ValueError) as err:
                if is_definite_recovery_failure(err):
                    raise UnrecoverableTurnCardError(str(err), message_ids) from err
                if attempt < 2:
                    await sleep(5.0)
                    continue
                log.warning(
                    "turn.card_intent_match_fetch_failed",
                    intent_id=str(intent_id),
                    message_id=str(message_id),
                    error=str(err),
                )
                return False
            if not await _mark_card_interrupted(
                message, intent_id=intent_id, client=client, restarted=restarted
            ):
                return False
            break
    return True


async def _mark_card_interrupted(
    message: discord.Message,
    *,
    intent_id: UUID,
    client: discord.Client | None = None,
    restarted: bool = True,
) -> bool:
    """Return true for a terminal card or a successfully edited live card."""
    if intent_id not in turn_card_ids_from_message(message):
        return True
    try:
        embed = discord.Embed(
            color=0xE74C3C,
            title="Stopped: Daimon restarted." if restarted else "Stopped.",
            description="Mention me to try again.",
        )
        if client is not None and message.webhook_id is not None:
            transport = DiscordPostTransport(
                client,
                message.channel,
                name=message.author.name,
                avatar_url=None,
                builtin=False,
            )
            if transport._destination() is None:  # pyright: ignore[reportPrivateUsage]
                return False
            replacement = await transport.edit(
                message, embed=embed, view=None, _allow_replacement=False
            )
            if isinstance(replacement, discord.Message) and replacement.id != message.id:
                return False
        else:
            await message.edit(embed=embed, view=None)
        return True
    except (discord.HTTPException, discord.ClientException) as err:
        if is_definite_recovery_failure(err):
            raise UnrecoverableTurnCardError(str(err), {message.id}) from err
        log.warning(
            "turn.card_intent_edit_failed",
            intent_id=str(intent_id),
            message_id=str(message.id),
            error=str(err),
        )
        return False
