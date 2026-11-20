"""Answers to workflow asks from chat, decided by `waits.decide_wait` as the inbox does.

`claim_reply` runs before a chat message is admitted as a turn: a message in the thread
an ask opened, or an explicit reply to an ask, is that ask's answer (or a refusal, or a
hint); anything else returns None and goes on as ordinary chat. `press` is an ask's
buttons and its answer form. A person answers as their linked account; a refusal says
why and who can help, at most `REFUSALS_PER_WAIT` times a wait.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal, cast

from daimon.core.config import Settings
from daimon.core.stores import workflow_deliveries, workflow_runs, workflow_waits, workflows
from daimon.core.stores.accounts import get_account
from daimon.core.stores.domain import Role
from daimon.core.stores.workflow_run_events import NewEvent, append_events, list_events
from daimon.core.stores.workflow_waits import Wait, WaitPost
from daimon.core.workflows import asks, waits
from daimon.core.workflows.access import Who
from daimon.core.workflows.runs import Sessions
from daimon.core.workflows.views import people_of
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "ASK_ACTION",
    "REFUSALS_PER_WAIT",
    "AskForm",
    "Button",
    "Claimed",
    "Reply",
    "action_id",
    "ask_form",
    "buttons",
    "claim_reply",
    "form_answer",
    "form_of",
    "parse_action",
    "press",
]

Chat = Literal["discord", "slack", "teams"]
#: Button ids: `wfa:<wait id>:<route>`; the route `answer` opens the answer form.
ASK_ACTION: Final = "wfa"
REFUSALS_PER_WAIT: Final = 3
_ACTION = re.compile(rf"^{ASK_ACTION}:([0-9a-f-]{{36}}):([\w-]{{1,64}})$")
_WORD = re.compile(r"^\W*(\w+)\W*\s*(.*)$", re.S)


def action_id(wait_id: str, route: str) -> str:
    return f"{ASK_ACTION}:{wait_id}:{route}"


def parse_action(custom_id: str) -> tuple[uuid.UUID, str] | None:
    found = _ACTION.match(custom_id)
    return None if found is None else (uuid.UUID(found[1]), found[2])


@dataclass(frozen=True)
class Reply:
    """A chat message that may answer an ask, in platform ids."""

    platform: Chat
    tenant_ids: tuple[uuid.UUID, ...]
    author: str
    message_id: str
    text: str
    #: The thread the message is in, as asks record it (`workflow_wait_posts.thread_id`).
    thread_id: str | None = None
    #: The message it explicitly replies to.
    reply_to: str | None = None
    files: tuple[dict[str, JsonValue], ...] = ()


@dataclass(frozen=True)
class Claimed:
    """What became of a message or press; `text` is for the person, None for silence."""

    status: Literal["answered", "refused", "hint"]
    text: str | None


async def _who(
    session: AsyncSession, tenant_id: uuid.UUID, platform: str, author: str
) -> Who | None:
    # Imported here: the models module is heavy and only this lookup needs it.
    from daimon.core._models import PlatformPrincipal

    principal = await session.scalar(
        select(PlatformPrincipal).where(
            PlatformPrincipal.tenant_id == tenant_id,
            PlatformPrincipal.platform == platform,
            PlatformPrincipal.external_id == author,
        )
    )
    if principal is None or principal.foreign_home_id is not None:
        return None
    account = await get_account(session, principal.account_id)
    if account is None:
        return None
    return Who(tenant_id, account.id, admin=account.role is Role.ADMIN)


async def _ask(session: AsyncSession, wait: Wait) -> dict[str, Any]:
    for row in await workflow_deliveries.deliveries_of_wait(session, wait.id):
        if row.kind == "ask" and (found := asks.ask_of(row.payload)) is not None:
            return found
    return {}


async def _helper(session: AsyncSession, wait: Wait) -> str:
    """Who to ask for help: the workflow's owner by name."""
    run = await workflow_runs.get_run(session, wait.run_id)
    workflow = (
        None
        if run is None
        else await workflows.get_workflow(
            session, tenant_id=wait.tenant_id, workflow_id=run.workflow_id
        )
    )
    owner = None if workflow is None else workflow.owner_account_id
    names = await people_of(session, wait.tenant_id, [owner])
    if owner is not None and owner in names:
        return f"Ask {names[owner].name}, who owns this workflow."
    return "Ask the workflow's owner or an admin."


async def _refused(sm: Sessions, wait: Wait, why: str, *, now: datetime) -> Claimed:
    """A refusal in plain words naming who can help; silent once the wait had its share."""
    async with sm.begin() as session:
        earlier = [
            e
            for e in await list_events(session, wait.run_id)
            if e.type == "wait.answer_refused" and e.data.get("id") == str(wait.id)
        ]
        if len(earlier) >= REFUSALS_PER_WAIT:
            return Claimed("refused", None)
        helper = await _helper(session, wait)
        await append_events(
            session,
            run_id=wait.run_id,
            events=[NewEvent("wait.answer_refused", None, {"id": str(wait.id), "why": why})],
            actor="chat",
            at=now,
        )
    return Claimed("refused", f"{why}\n{helper}")


def _routes(ask: dict[str, Any]) -> list[tuple[str, str]]:
    routes = cast("list[dict[str, Any]]", ask.get("routes") or [])
    return [(str(r.get("name")), str(r.get("label"))) for r in routes]


def _from_text(ask: dict[str, Any], text: str) -> waits.Answer | None:
    """A typed answer: any text for a question, else a leading option word and a comment."""
    shape = ask.get("shape")
    if shape in ("text", "question"):
        return waits.Answer(route=waits.ANSWER, values={"text": text}) if text.strip() else None
    if shape not in ("approve", "choice"):
        return None
    found = _WORD.match(text)
    if found is None:
        return None
    word = found[1].casefold()
    for name, label in _routes(ask):
        if word in (name.casefold(), label.casefold(), label.split(" ")[0].casefold()):
            return waits.Answer(route=name, comment=found[2].strip() or None)
    return None


async def _decide(
    sm: Sessions,
    settings: Settings,
    *,
    wait: Wait,
    platform: Chat,
    author: str,
    answer: waits.Answer,
    key: str,
    in_thread: bool,
    now: datetime,
) -> Claimed:
    async with sm() as session:
        who = await _who(session, wait.tenant_id, platform, author)
    if who is None:
        why = "Only people with a Daimon account here can answer this."
        return await _refused(sm, wait, why, now=now)
    try:
        decided = await waits.decide_wait(
            sm,
            settings,
            who,
            wait.id,
            answer,
            via=platform,
            idempotency_key=key,
            in_thread=in_thread,
            now=now,
        )
    except waits.NotYours as err:
        return await _refused(sm, wait, str(err), now=now)
    except waits.WaitError as err:
        return Claimed("hint", str(err))
    if decided.status == "counted":
        return Claimed("answered", "Thanks, your answer is counted. More answers are needed.")
    return Claimed("answered", None)


@dataclass(frozen=True)
class AskForm:
    """What an ask's answer form asks: one text box, or a form's fields; a reject's reason."""

    title: str
    fields: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Button:
    custom_id: str
    label: str
    primary: bool


def buttons(ask: dict[str, Any]) -> list[Button]:
    """An ask's buttons in order, the primary last: its options, or Answer."""
    wait_id = str(ask.get("wait_id"))
    if ask.get("shape") in ("approve", "choice"):
        named = _routes(ask)
        return [
            Button(action_id(wait_id, name), label, i == len(named) - 1)
            for i, (name, label) in enumerate(named)
        ]
    return [Button(action_id(wait_id, waits.ANSWER), "Answer", True)]


def form_of(ask: dict[str, Any], route: str) -> AskForm | None:
    """The form `route`'s button opens before it answers, or None when it answers at once."""
    title = str(ask.get("title") or "Answer")[:45]
    if ask.get("shape") == "approve" and route == waits.REJECT:
        return AskForm(title, (("comment", "Why are you rejecting it?"),))
    if route != waits.ANSWER:
        return None
    if ask.get("shape") == "form":
        named = cast("list[dict[str, Any]]", ask.get("fields") or [])
        fields = tuple((str(f["name"]), str(f["title"])) for f in named)
        return AskForm(title, fields)
    return AskForm(title, (("text", "Your answer"),))


async def ask_form(sm: Sessions, *, platform: Chat, message_id: str, route: str) -> AskForm | None:
    """`form_of` the ask posted as `message_id`."""
    async with sm() as session:
        found = await workflow_waits.post_by_message(
            session, platform=platform, message_id=message_id
        )
        ask = {} if found is None else await _ask(session, found[1])
    return form_of(ask, route)


def form_answer(route: str, values: dict[str, str]) -> tuple[dict[str, JsonValue], str | None]:
    """A sent form as `press` takes it: the values, and a reject's reason as its comment."""
    if route == waits.REJECT:
        return {}, values.get("comment")
    return dict(values), None


def _in_thread(post: WaitPost) -> bool:
    """A thread ask (no addressee) is answered by anyone the wait's `thread` token admits."""
    return post.addressee_account_id is None


async def claim_reply(
    sm: Sessions, settings: Settings, reply: Reply, *, now: datetime | None = None
) -> Claimed | None:
    """The ask `reply` answers, decided; None when it answers no open ask."""
    if not reply.tenant_ids or (reply.thread_id is None and reply.reply_to is None):
        return None
    async with sm() as session:
        found = await workflow_waits.open_post_for(
            session,
            tenant_ids=reply.tenant_ids,
            platform=reply.platform,
            thread_id=reply.thread_id,
            reply_to=reply.reply_to,
        )
        if found is None:
            return None
        post, wait = found
        ask = await _ask(session, wait)
    answer = _from_text(ask, reply.text)
    if answer is None:
        words = " or ".join(name for name, _ in _routes(ask)) or "your answer"
        hint = (
            "Use the Answer button to fill this in."
            if ask.get("shape") == "form"
            else f"Start your reply with {words}, or use a button."
        )
        return Claimed("hint", hint)
    answer = answer.model_copy(update={"files": list(reply.files)})
    return await _decide(
        sm,
        settings,
        wait=wait,
        platform=reply.platform,
        author=reply.author,
        answer=answer,
        key=f"{reply.platform}:{reply.message_id}",
        in_thread=_in_thread(post),
        now=now or datetime.now(UTC),
    )


async def press(
    sm: Sessions,
    settings: Settings,
    *,
    platform: Chat,
    author: str,
    message_id: str,
    route: str,
    values: dict[str, JsonValue] | None = None,
    comment: str | None = None,
    now: datetime | None = None,
) -> Claimed:
    """A press of the buttons on the ask posted as `message_id`, or its answer form sent."""
    async with sm() as session:
        found = await workflow_waits.post_by_message(
            session, platform=platform, message_id=message_id
        )
    if found is None:
        return Claimed("hint", "This request is gone.")
    post, wait = found
    answer = waits.Answer(route=route, comment=comment, values=values or {})
    return await _decide(
        sm,
        settings,
        wait=wait,
        platform=platform,
        author=author,
        answer=answer,
        key=f"{platform}:{message_id}:{author}",
        in_thread=_in_thread(post),
        now=now or datetime.now(UTC),
    )
