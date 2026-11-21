"""Mention triggers: someone tags Daimon in a channel or thread a workflow listens to.

Every chat handler calls `claim_reply`, then `claim_mention`, then admits the message as chat
(`tests/parity/test_workflow_answer_paths.py`). A claimed mention starts its run here, as its
author with untrusted inputs, through the starts loop's own `settle_event`. Anything short of a
started run (no match, workflows or mentions off, no right to run, a held key) leaves the
message to ordinary chat; only a held key or a refusal is recorded, as a skipped event.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime

import structlog
from daimon.core.config import Settings
from daimon.core.posted_controls import PostedCard
from daimon.core.stores import workflow_triggers as store
from daimon.core.stores import workflows as workflows_store
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.web_links import WebAt
from daimon.core.workflows import gate, triggers
from daimon.core.workflows.access import may_run
from daimon.core.workflows.chat_cards import ChatPlace, run_card_of
from daimon.core.workflows.runs import Sessions
from daimon.core.workflows.triggers import MappingError, passes
from pydantic import JsonValue

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Claimed:
    """A mention that started its run: the run's card to post (none for a redelivery), which
    the poster records as the run's card (`chat_cards.show_on_card`) so it follows the run."""

    card: PostedCard | None = None
    run_id: uuid.UUID | None = None


@dataclass(frozen=True)
class Mention:
    """A chat message that tags Daimon. `channel_id` is a thread's parent channel."""

    tenant_id: uuid.UUID
    platform: str
    author: str
    message_id: str
    text: str
    channel_id: str
    thread_id: str | None = None
    files: tuple[str, ...] = ()
    #: The message's and the thread's links, when the platform has them.
    link: str | None = None
    thread_link: str | None = None
    #: Reads both links once a trigger listens here, when they cost a platform call.
    read_links: Callable[[], Awaitable[tuple[str | None, str | None]]] | None = field(
        default=None, compare=False
    )

    def context(self) -> dict[str, JsonValue]:
        """What `match` and `inputs` read: `message` and `thread`."""
        message: dict[str, JsonValue] = {
            "text": self.text,
            "author": self.author,
            "files": list(self.files),
            "link": self.link,
        }
        thread: dict[str, JsonValue] = {
            "platform": self.platform,
            "channel": self.channel_id,
            "id": self.thread_id or self.message_id,
            "link": self.thread_link or self.link,
        }
        return {"message": message, "thread": thread}

    def origin(self) -> dict[str, JsonValue]:
        """The run's `origin`: where `deliver.to: origin` replies."""
        place = ChatPlace(self.platform, self.channel_id, self.message_id, self.thread_id)
        return {**place.ref(), **({"link": self.link} if self.link else {})}


async def claim_mention(
    sm: Sessions, settings: Settings, mention: Mention, links: WebAt, now: datetime
) -> Claimed | None:
    """The started run (or a redelivery's nothing to post), else None: ordinary chat."""
    features = await gate.features_at(sm, settings, mention.tenant_id, mention.channel_id)
    if features is None or not (features.enabled and features.mentions):
        return None
    places = [mention.channel_id, *([mention.thread_id] if mention.thread_id else [])]
    async with sm() as session:
        listening = await store.mention_triggers_at(
            session, tenant_id=mention.tenant_id, places=places
        )
    if not listening:
        return None
    if mention.read_links is not None:
        link, thread_link = await mention.read_links()
        mention = replace(mention, link=link, thread_link=thread_link, read_links=None)
    context = mention.context()
    async with sm.begin() as session:
        author = await get_or_create_platform_principal(
            session,
            tenant_id=mention.tenant_id,
            platform=mention.platform,
            external_id=mention.author,
        )
        winner = None
        for trigger in listening:  # the oldest that matches and the author may run
            if not _matches(trigger, context, now):
                continue
            workflow = await workflows_store.get_workflow(
                session, tenant_id=trigger.tenant_id, workflow_id=trigger.workflow_id
            )
            if workflow is not None and await may_run(session, workflow, author.account_id):
                winner = trigger
                break
    if winner is None:
        return None
    started = await _start(sm, settings, winner, mention, author.account_id, now)
    if not isinstance(started, uuid.UUID):
        return started
    card = await run_card_of(sm, started, links, mention.channel_id, mention.thread_id)
    return Claimed(card, started)


def _matches(trigger: store.TriggerRow, context: dict[str, JsonValue], now: datetime) -> bool:
    match = (trigger.live or {}).get("match")
    try:
        return passes(match if isinstance(match, str) else None, context, now=now)
    except MappingError:
        return False  # the starts rule rejects it on the event; here it is chat


async def _start(
    sm: Sessions,
    settings: Settings,
    trigger: store.TriggerRow,
    mention: Mention,
    author: uuid.UUID,
    now: datetime,
) -> uuid.UUID | Claimed | None:
    """Record the event, then start it as the starts loop would; a held key or a refusal
    marks it skipped and leaves the message to chat."""
    async with sm.begin() as session:
        event, first = await store.record_event(
            session,
            trigger=trigger,
            status="accepted",
            received_at=now,
            delivery_id=f"{mention.platform}:{mention.message_id}",
            verified=True,
            payload={**mention.context(), "origin": mention.origin(), "run_as": str(author)},
        )
    if first is not None:
        return Claimed() if first.status in ("accepted", "started") else None
    async with sm.begin() as session:
        claimed = await store.claim_accepted_events(session, limit=1, event_id=event.id)
        if not claimed:
            return Claimed()  # the starts loop took it first
        run = await triggers.settle_event(sm, settings, session, *claimed[0], now)
    if run is None:
        return None
    log.info("workflow_mention.started", run_id=str(run.id), trigger_id=str(trigger.id))
    return run.id
