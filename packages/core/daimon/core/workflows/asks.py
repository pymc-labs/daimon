"""Human asks on chat: what an approval or question posts, where each post landed, and the
reminders, updates and owner notice that follow.

An ask goes by DM to each person the wait names (`deliver: dm`), or as one root message in
a channel that opens a thread (`deliver: thread`); the inbox lists it either way. A test run
asks its tester only. Each post is a `workflow_deliveries` job, so a Finish's rules apply at
post time; a post records where it landed (`workflow_wait_posts`), which is what lets a
reply there answer. A held or refused ask stays in the inbox and tells the owner once.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final, Literal

from daimon.core.stores import workflow_deliveries, workflow_runs, workflow_waits, workflows
from daimon.core.stores.workflow_deliveries import DeliveryKind, DeliveryRow
from daimon.core.stores.workflow_run_events import NewEvent, append_events
from daimon.core.stores.workflow_runs import RunRow
from daimon.core.stores.workflow_waits import Wait, WaitPost
from daimon.core.workflows.delivery import dm_target, target_for
from daimon.core.workflows.engine import Event
from daimon.core.workflows.views import people_of
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "Ask",
    "AskShape",
    "ask_of",
    "queue",
    "queue_reminders",
    "queue_updates",
    "record_post",
    "tell_owner_undelivered",
]

AskShape = Literal["approve", "choice", "text", "form", "question"]
#: Why an ask did not go, in words for the owner's notice.
_WHY: Final[dict[str, str]] = {
    "dm_disabled": "direct messages are off in this workspace",
    "recipient_not_on_platform": "they have no linked account here",
    "speaker_not_on_platform": "the person the run runs as has no linked account here",
    "destination_unavailable": "the place to post is gone or out of reach",
    "creator_cannot_post": "the person the run runs as cannot post there",
    "audience_contained": "the channel's audience rules keep it there",
    "protected_channel": "the channel does not take Daimon's posts",
}


@dataclass(frozen=True)
class Ask:
    """What a wait's chat posts carry, built by the code that opens the wait."""

    deliver: str
    workflow: str
    title: str
    text: str
    shape: AskShape
    #: `(name, label)` per button, the primary last.
    routes: tuple[tuple[str, str], ...]
    home: str | None = None
    channel: str | None = None
    files: tuple[dict[str, JsonValue], ...] = ()
    #: A form's `(name, title)` fields, asked in the platform's modal.
    fields: tuple[tuple[str, str], ...] = field(default=())


def _hint(ask: Ask) -> str:
    if ask.shape == "form":
        return "Use the Answer button to fill it in."
    if ask.shape in ("text", "question"):
        return "Reply here or use the Answer button."
    words = " or ".join(name for name, _ in ask.routes)
    return f"Use a button, or reply starting with {words}."


def ask_of(payload: dict[str, Any]) -> dict[str, Any] | None:
    """A delivery's ask part, or None for a Finish or notice."""
    raw = payload.get("ask")
    return raw if isinstance(raw, dict) else None  # pyright: ignore[reportUnknownVariableType]


def _targets(run: RunRow, addressees: Sequence[str], ask: Ask) -> list[dict[str, Any]]:
    if run.mode == "test":
        return [dm_target(run.run_as_account_id, home=ask.home)]
    if ask.deliver == "dm":
        return [dm_target(a, home=ask.home) for a in addressees]
    channel = ask.channel or ask.home
    if ask.deliver != "thread" or channel is None:
        return []
    target = target_for(channel, home=ask.home, mode="live", run_as=None, owner=None)
    return [target | {"open_thread": target["kind"] == "channel"}]


async def queue(
    session: AsyncSession,
    *,
    run: RunRow,
    wait_id: uuid.UUID,
    addressees: Sequence[str],
    ask: Ask,
) -> list[Event]:
    """One post per DM addressee, or one thread root; none for an inbox-only ask."""
    events: list[Event] = []
    for target in _targets(run, addressees, ask):
        payload: dict[str, Any] = {
            "text": f"{ask.text}\n\n{_hint(ask)}",
            "files": list(ask.files),
            "workflow": ask.workflow,
            "run_number": run.number,
            "speaker": None if run.run_as_account_id is None else str(run.run_as_account_id),
            "home": ask.home,
            "ask": {
                "wait_id": str(wait_id),
                "title": ask.title,
                "shape": ask.shape,
                "routes": [{"name": n, "label": label} for n, label in ask.routes],
                "fields": [{"name": n, "title": t} for n, t in ask.fields],
                "addressee": target.get("account_id"),
                "body": ask.text,
            },
        }
        delivery_id = await workflow_deliveries.queue_delivery(
            session,
            tenant_id=run.tenant_id,
            workflow_id=run.workflow_id,
            run_id=run.id,
            wait_id=wait_id,
            kind="ask",
            target=target,
            payload=payload,
        )
        events.append(
            Event("delivery.queued", None, {"delivery_id": str(delivery_id), "kind": "ask"})
        )
    return events


async def record_post(
    session: AsyncSession,
    row: DeliveryRow,
    *,
    platform: str,
    channel_id: str,
    thread_id: str | None,
    message_id: str,
    link: str | None = None,
    at: datetime,
) -> WaitPost | None:
    """Where an ask landed, so a reply there answers it; `wait.posted` on the run's ledger."""
    ask = ask_of(row.payload)
    if row.kind != "ask" or ask is None or row.wait_id is None or row.run_id is None:
        return None
    raw = ask.get("addressee")
    addressee = uuid.UUID(raw) if isinstance(raw, str) else None
    post = await workflow_waits.record_post(
        session,
        tenant_id=row.tenant_id,
        wait_id=row.wait_id,
        platform=platform,
        channel_id=channel_id,
        thread_id=thread_id,
        message_id=message_id,
        addressee_account_id=addressee,
    )
    sent: dict[str, JsonValue] = {
        "platform": platform,
        "kind": "dm" if row.target.get("kind") == "dm" else "thread",
        "addressee": raw if isinstance(raw, str) else None,
        "link": link,
        "at": at.isoformat(),
    }
    await append_events(
        session,
        run_id=row.run_id,
        events=[NewEvent("wait.posted", None, {"id": str(row.wait_id), "sent": sent})],
        actor="delivery",
        at=at,
    )
    return post


async def _queue_on_posts(
    session: AsyncSession,
    wait: Wait,
    *,
    kind: DeliveryKind,
    target_kind: str,
    text: str,
) -> int:
    run = await workflow_runs.get_run(session, wait.run_id)
    if run is None:
        return 0
    asked = [
        d for d in await workflow_deliveries.deliveries_of_wait(session, wait.id) if d.kind == "ask"
    ]
    if not asked:
        return 0
    base = asked[0].payload
    posts = await workflow_waits.posts_of_wait(session, wait.id)
    for post in posts:
        await workflow_deliveries.queue_delivery(
            session,
            tenant_id=wait.tenant_id,
            workflow_id=run.workflow_id,
            run_id=run.id,
            wait_id=wait.id,
            kind=kind,
            target={
                "kind": target_kind,
                "channel_id": post.channel_id,
                "thread_id": post.thread_id,
                "message_id": post.message_id,
            },
            payload={
                "text": text,
                "files": [],
                "workflow": base.get("workflow"),
                "run_number": base.get("run_number"),
                "speaker": base.get("speaker"),
                "update": kind == "ask_update",
            },
        )
    return len(posts)


async def queue_reminders(session: AsyncSession, wait: Wait) -> int:
    """A reminder in each DM or thread the ask went to."""
    title = str((wait.shown or {}).get("title") or "This question")
    text = f"Reminder: {title} is still waiting for an answer."
    return await _queue_on_posts(session, wait, kind="reminder", target_kind="reply", text=text)


async def queue_updates(session: AsyncSession, wait: Wait, status: str) -> int:
    """Each posted ask edited to say how it ended; its buttons go."""
    asked = [
        d for d in await workflow_deliveries.deliveries_of_wait(session, wait.id) if d.kind == "ask"
    ]
    ask = ask_of(asked[0].payload) if asked else None
    body = str(ask.get("body") or "") if ask else ""
    text = f"{body}\n\n{status}".strip()
    return await _queue_on_posts(session, wait, kind="ask_update", target_kind="edit", text=text)


async def answered_status(session: AsyncSession, wait: Wait, *, by: uuid.UUID, label: str) -> str:
    names = await people_of(session, wait.tenant_id, [by])
    name = names[by].name if by in names else "someone"
    return f"{label} by {name}."


async def tell_owner_undelivered(session: AsyncSession, row: DeliveryRow, note: str | None) -> None:
    """An ask that did not go: tell the owner once per wait; the inbox still lists it."""
    if row.kind != "ask" or row.wait_id is None or row.run_id is None:
        return
    if any(
        d.kind == "notice"
        for d in await workflow_deliveries.deliveries_of_wait(session, row.wait_id)
    ):
        return
    workflow = await workflows.get_workflow(
        session, tenant_id=row.tenant_id, workflow_id=row.workflow_id
    )
    if workflow is None or workflow.owner_account_id is None:
        return
    ask = ask_of(row.payload) or {}
    raw = ask.get("addressee")
    who = "the people it asks"
    if isinstance(raw, str):
        account = uuid.UUID(raw)
        names = await people_of(session, row.tenant_id, [account])
        who = names[account].name if account in names else "a person"
    why = _WHY.get(note or "", "the post was not allowed")
    home = row.payload.get("home")
    target = (
        target_for("home", home=home, mode="live", run_as=None, owner=None)
        if isinstance(home, str)
        else dm_target(workflow.owner_account_id, home=None)
    )
    number = row.payload.get("run_number")
    await workflow_deliveries.queue_delivery(
        session,
        tenant_id=row.tenant_id,
        workflow_id=row.workflow_id,
        run_id=row.run_id,
        wait_id=row.wait_id,
        kind="notice",
        target=target,
        payload={
            "text": (
                f'Run {number} of "{workflow.name}" could not ask {who} on chat: {why}. '
                "The question waits in the Workflows inbox."
            ),
            "files": [],
            "workflow": workflow.name,
            "run_number": number,
            "speaker": str(workflow.owner_account_id),
        },
    )
