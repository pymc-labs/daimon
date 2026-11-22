"""Watches on the web: a fired watch runs an unattended turn in its owner's chat, or in a
new chat per report, and its notices land in the chat it was made in.

Each wake is the owner's again at run time: an active member of the workspace, still
in the chat workspace, owning the chat. Anything else settles the wake unrun.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Coroutine
from datetime import datetime
from typing import Any

import structlog
from daimon.adapters.web.turns import WebTurns
from daimon.core.continuity.dispatch import dispatch_pending_continuations
from daimon.core.continuity.wakes import WakeThread, run_wake_poller
from daimon.core.continuity.watch_notices import poll_watch_notices
from daimon.core.continuity.watch_reports import dispatch_watch_report, run_watch_report_poller
from daimon.core.stores import web_chat as chat_store
from daimon.core.stores import web_identities as identities_store
from daimon.core.stores import web_people
from daimon.core.stores.domain import (
    TaskContinuationRow,
    WatchRow,
    WebMessageBlocks,
    WebPersonRow,
    WebRefusalCard,
    WebThreadRow,
)
from daimon.core.stores.watches import (
    REFUSED,
    NoticeAttempt,
    Refused,
    get_firing,
    get_watch,
    record_delivery_thread,
)
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.web_identity import WebPrincipal, principal_of, thread_key
from daimon.core.web_roles_rules import chat_standings, has_left, roles_of
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)

#: How often the web checks for watch notices.
NOTICE_POLL_S = 30.0
_PREFIX = "web:"


def _chat_id(key: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(key.removeprefix(_PREFIX)) if key.startswith(_PREFIX) else None
    except ValueError:
        return None


class WebWatchRunner:
    """The web process's wake, report and notice pollers."""

    def __init__(self, turns: WebTurns) -> None:
        self.turns = turns
        self.sessionmaker = turns.sessionmaker

    def pollers(self, should_stop: Callable[[], bool]) -> list[Coroutine[Any, Any, None]]:
        return [
            run_wake_poller(
                self.sessionmaker, platform="web", open_thread=self._open, should_stop=should_stop
            ),
            run_watch_report_poller(
                self.sessionmaker, platform="web", open_report=self._report, should_stop=should_stop
            ),
            self._notices(should_stop),
        ]

    async def _open(self, wake: WakeThread) -> bool:
        if self.turns.draining:
            return True
        self.turns.spawn(
            dispatch_pending_continuations(
                self.sessionmaker,
                self.turns.deps.anthropic,
                tenant_id=wake.tenant_id,
                platform="web",
                thread_id=wake.thread_id,
                run_follow_up=self.run,
                latest_user_message_at=self._latest,
            )
        )
        return True

    async def _report(self, row: TaskContinuationRow) -> None:
        if not self.turns.draining:
            self.turns.spawn(
                dispatch_watch_report(self.sessionmaker, row, run_report=self.run_report)
            )

    async def _latest(self, row: TaskContinuationRow) -> datetime | None:
        chat = _chat_id(row.thread_id)
        if chat is None:
            return None
        async with self.sessionmaker() as db:
            messages = await chat_store.list_messages(db, thread_id=chat)
        return max((m.created_at for m in messages if m.role == "user"), default=None)

    async def run(self, row: TaskContinuationRow, seed: str) -> None:
        """One watch turn in the chat `row` names, as its owner now stands."""
        if row.reason not in ("watch_fired", "watch_report"):
            raise AdmissionDenied(reason="runs_elsewhere")
        async with self.sessionmaker() as db:
            person, principal = await _owner(db, row)
            chat = _chat_id(row.thread_id)
            thread = (
                None
                if chat is None
                else await chat_store.get_thread(db, person_id=person.id, thread_id=chat)
            )
        if thread is None:
            raise AdmissionDenied(reason="runs_elsewhere")
        await self.turns.run_watch(row, seed, person=person, principal=principal, thread=thread)

    async def run_report(self, row: TaskContinuationRow, seed: str) -> None:
        """A new-chat report: in the chat this firing made, or a new one."""
        async with self.sessionmaker.begin() as db:
            person, _ = await _owner(db, row)
            thread = await _report_chat(db, row, person)
        await self.run(row.model_copy(update={"thread_id": thread_key(thread.id)}), seed)

    async def _notices(self, should_stop: Callable[[], bool]) -> None:
        while not should_stop():
            try:
                await poll_watch_notices(self.sessionmaker, deliver=self.notify, platform="web")
            except Exception:
                log.exception("watch_notice.poll_failed", platform="web")
            await asyncio.sleep(NOTICE_POLL_S)

    async def notify(
        self, watch: WatchRow, _workspace: str, text: str, /, *, attempt: NoticeAttempt
    ) -> bool | Refused:
        """Daimon's own card in the chat the watch was made in; one per notice, however
        often it is retried."""
        chat = _chat_id(watch.thread_id)
        if chat is None:
            return REFUSED
        what, _, todo = text.partition("\n")
        card = WebRefusalCard(id=attempt.nonce, title="Watch", body=what, help=todo)
        async with self.sessionmaker.begin() as db:
            row = await chat_store.upsert_assistant_message(
                db,
                tenant_id=watch.tenant_id,
                thread_id=chat,
                message_id=uuid.uuid5(uuid.NAMESPACE_URL, attempt.nonce),
                turn_id=uuid.uuid4(),
                content="",
                blocks=WebMessageBlocks(card=card),
                status="done",
            )
        return REFUSED if row is None else True


async def _owner(db: AsyncSession, row: TaskContinuationRow) -> tuple[WebPersonRow, WebPrincipal]:
    """The wake's owner as a web member now, or a refusal that settles it."""
    person = await web_people.get_person_for_account(
        db, tenant_id=row.tenant_id, account_id=row.requester_account_id
    )
    if person is None or person.kind == "visitor" or person.status != "active":
        raise AdmissionDenied(reason="invoker_not_allowed")
    standings = await chat_standings(db, [person])
    if has_left(standings, person):
        raise AdmissionDenied(reason="invoker_not_allowed")
    role = (await roles_of(db, [person], standings))[person.id]
    identities = await identities_store.list_for_person(db, person_id=person.id)
    groups = frozenset(g for identity in identities for g in identity.groups)
    return person, await principal_of(db, person, role, groups=groups)


async def _report_chat(
    db: AsyncSession, row: TaskContinuationRow, person: WebPersonRow
) -> WebThreadRow:
    firing = await get_firing(db, row.idempotency_key)
    watch = None if firing is None else await get_watch(db, firing.watch_id)
    if firing is None or watch is None or watch.delivery != "new_thread":
        raise AdmissionDenied(reason="runs_elsewhere")
    if firing.delivery_thread_id is not None:
        chat = _chat_id(firing.delivery_thread_id)
        thread = (
            None
            if chat is None
            else await chat_store.get_thread(db, person_id=person.id, thread_id=chat)
        )
        if thread is None:
            raise AdmissionDenied(reason="runs_elsewhere")
        return thread
    words = " ".join(watch.instructions.split())
    thread = await chat_store.create_thread(
        db,
        tenant_id=row.tenant_id,
        person_id=person.id,
        agent_id=row.target_ma_agent_id,
        agent_name=row.target_name,
        title=f"Watch report: {words}"[:80],
    )
    await record_delivery_thread(db, row.idempotency_key, thread_key(thread.id))
    return thread
