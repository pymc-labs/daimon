"""Workflow posts: what a run queues when it ends, and the rules each post passes.

A Finish with `deliver` queues one `finish` post when its run succeeds, and a post step
one `post` as it runs (`queue_deliver`). A live run that fails or times out queues one
`notice` to the owner, unless the run before it failed too. Every one of them is written by
`queue_post`. A Finish or post speaks for the run-as person, a notice for the owner.

A templated `to`, `also` or `mention` must land in the spec's `destinations`, a thread
under a listed channel, the run's origin or a thread this run's posts opened; anything else
is refused `not_a_destination` (at queue time, or at post time where only the platform knows
a thread's parent). Literal ids were approved at publish. At post time the
rules of a routine's result apply (`clear_post`, then `post_refusal` once the adapter has
placed the channel): a live workspace, a readable access policy, a speaker still allowed
to invoke, no write protection, the audience gate, and reach. The adapter checks that
the speaker may post in the channel themselves.

A routine step's `result` (and its channel's failure notice) is no such post: it goes through
its platform's routine poster as a `DeliveryJob` (`result_poster`), as its routine's would.

A post to a person's DM (`owner`, `run_as`, an ask's addressee, and every test run, which
delivers to the tester) goes on the workspace's platform when it has DMs on and the person
is linked there; the channel rules then have no channel to check.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal, cast

from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, Place, Subject, Surface, authorize
from daimon.core.config import DirectMessagePolicy, Settings, env_admits
from daimon.core.delivery import DeliveryOutcome, WorkflowPoster
from daimon.core.fire.delivery import INTERRUPTED, DeliveryJob, Poster
from daimon.core.fire.subject import workflow_subject
from daimon.core.membership import MembershipResolver
from daimon.core.membership_subject import resolve_subject
from daimon.core.outbound.mentions import Platform, RenderedPost, attribution_line, render_post
from daimon.core.outbound.opt_out import untag_opted_out
from daimon.core.outbound.reach import ReachPlan, ReachTally, check_reach
from daimon.core.routine_delivery import creator_refusal_for
from daimon.core.routine_header import routine_header_shown
from daimon.core.routine_hold import DeliveryGate, workflow_delivery_gate
from daimon.core.stores import (
    web_people,
    workflow_deliveries,
    workflow_runs,
    workflow_threads,
    workflow_waits,
    workflows,
)
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.accounts import get_account
from daimon.core.stores.direct_messages import dm_enabled
from daimon.core.stores.domain import Role
from daimon.core.stores.identity import platform_external_ids
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.workflow_deliveries import DeliveryRow
from daimon.core.stores.workflow_runs import RunRow
from daimon.core.stores.workflows import WorkflowRow
from daimon.core.web_links import WebAt
from daimon.core.workflows.engine import Event, RunEnd, RunFacts, StepView, expression_context
from daimon.core.workflows.expressions import ExpressionError, expressions_in, render
from daimon.core.workflows.spec import Deliver, FinishNode, RoutineNode, Spec, parse_spec
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "ASK_CLOSED",
    "DM_OFF",
    "NOT_A_DESTINATION",
    "Reach",
    "WorkflowPost",
    "address",
    "clear_post",
    "origin_target",
    "post_refusal",
    "post_text",
    "queue_deliver",
    "queue_failure_notice",
    "queue_finish",
    "queue_post",
    "reach_limits",
    "result_job",
    "result_poster",
    "target_for",
    "titled_thread",
]

DM_OFF: Final[str] = "dm_disabled"
#: An ask or reminder whose wait closed before it went: nothing to post, no one to tell.
ASK_CLOSED: Final[str] = "ask_closed"
NOTICE_TITLE: Final[str] = "Workflow run failed"
#: A templated address outside the spec's destinations, the run's origin and its own threads.
NOT_A_DESTINATION: Final[str] = "not_a_destination"
_KEYWORDS: Final = frozenset({"home", "owner", "run_as", "origin"})
PostKind = Literal["finish", "post", "notice", "result"]
_SLACK_THREAD: Final[re.Pattern[str]] = re.compile(r"([CG][A-Z0-9]{2,}):([0-9]+\.[0-9]+)")
_TEAMS_THREAD: Final[str] = ";messageid="


def target_for(
    to: str,
    *,
    home: str | None,
    mode: str,
    run_as: uuid.UUID | None,
    owner: uuid.UUID | None,
) -> dict[str, Any]:
    """Where `deliver.to` points: `home`, `owner`, `run_as`, or a channel or thread id.

    A Slack thread is `<channel>:<ts>`, a Teams one `<channel>;messageid=<id>`; a Discord
    thread is a channel id the poster resolves. A test run delivers to the tester only.
    """
    if mode == "test" or to in ("owner", "run_as"):
        account = owner if to == "owner" and mode == "live" else run_as
        return dm_target(account, home=home)
    channel = home if to == "home" else to
    thread: str | None = None
    if channel is not None and _TEAMS_THREAD in channel:
        channel, _, thread = channel.partition(_TEAMS_THREAD)
    elif channel is not None and (match := _SLACK_THREAD.fullmatch(channel)):
        channel, thread = match.group(1), match.group(2)
    return {
        "kind": "channel" if thread is None else "thread",
        "channel_id": channel,
        "thread_id": thread,
        "home": channel is not None and channel == home,
    }


def dm_target(account: uuid.UUID | str | None, *, home: str | None) -> dict[str, Any]:
    """A DM to `account`. `via` is the home channel, where Teams finds the person's roster id."""
    return {"kind": "dm", "account_id": None if account is None else str(account), "via": home}


@dataclass(frozen=True)
class Reach:
    """Where a templated address may land besides a listed destination."""

    destinations: tuple[str, ...] = ()
    #: The chat message the run started from (`workflow_runs.origin`).
    origin: Mapping[str, Any] | None = None
    #: Threads this run's own posts opened.
    opened: frozenset[str] = frozenset()


def origin_target(
    origin: Mapping[str, Any], *, home: str | None, title: str | None = None
) -> dict[str, Any]:
    """The thread the run started from. A Slack or Teams message not in a thread is replied
    under; a Discord one gets a thread on it (`open_on`, named `title`)."""
    platform = origin.get("platform")
    channel, thread = str(origin.get("channel") or ""), origin.get("thread")
    message = origin.get("message")
    live: dict[str, Any] = {"home": home, "mode": "live", "run_as": None, "owner": None}
    if platform == "discord":
        # A Discord thread is a channel: an origin in one names it as its channel.
        target = target_for(str(thread or channel), **live)
        return (
            target
            if thread or not message
            else target | {"open_on": str(message)} | ({"title": title} if title else {})
        )
    if thread is None and (platform != "teams" or _TEAMS_THREAD not in channel) and message:
        thread = message
    if thread is None or _TEAMS_THREAD in channel:
        return target_for(channel, **live)
    under = str(thread).rsplit(":", 1)[-1]
    return target_for(f"{channel}{_TEAMS_THREAD if platform == 'teams' else ':'}{under}", **live)


def _titled(target: dict[str, Any], title: str | None) -> dict[str, Any]:
    return target | {"title": title} if title and target["kind"] == "channel" else target


def _place(target: Mapping[str, Any]) -> tuple[object, object]:
    return target.get("channel_id"), target.get("thread_id")


def address(
    value: str,
    *,
    templated: bool,
    home: str | None,
    mode: str,
    run_as: uuid.UUID | None,
    owner: uuid.UUID | None,
    reach: Reach,
    title: str | None = None,
) -> dict[str, Any]:
    """Where one `to` value points. A templated one outside `reach` is refused here, or checked
    at post time against the listed channels (`within`) when only the platform knows whether
    it is a thread under one of them."""
    if mode == "test":
        return dm_target(run_as, home=home)
    if value == "origin":
        if reach.origin is None:
            return _titled(
                target_for("home", home=home, mode=mode, run_as=run_as, owner=owner), title
            )
        return origin_target(reach.origin, home=home, title=title)
    if value.startswith("person:"):
        target = dm_target(value.removeprefix("person:"), home=home)
        listed = not templated or value in reach.destinations
        return target if listed else target | {"refused": NOT_A_DESTINATION}
    target = _titled(target_for(value, home=home, mode=mode, run_as=run_as, owner=owner), title)
    if not templated or value in _KEYWORDS or value in reach.destinations:
        return target
    origin = None if reach.origin is None else origin_target(reach.origin, home=home)
    if (
        value in reach.opened
        or (origin is not None and _place(target) in (_place(origin), (origin["channel_id"], None)))
        or (target["kind"] == "thread" and target["channel_id"] in reach.destinations)
    ):
        return target
    channels = [d for d in reach.destinations if not d.startswith("person:")]
    return target | ({"within": channels} if channels else {"refused": NOT_A_DESTINATION})


def _flat(value: JsonValue) -> list[str]:
    return [str(v) for v in value if v is not None] if isinstance(value, list) else [str(value)]


async def queue_post(
    session: AsyncSession,
    *,
    run: RunRow,
    workflow: WorkflowRow,
    kind: PostKind,
    targets: Sequence[dict[str, Any]],
    text: str,
    files: list[JsonValue],
    speaker: uuid.UUID | None,
    mention: Sequence[str] = (),
    step_id: uuid.UUID | None = None,
    wait_id: uuid.UUID | None = None,
    extra: Mapping[str, Any] | None = None,
    status: Literal["pending", "skipped"] = "pending",
    note: str | None = None,
) -> tuple[uuid.UUID, list[Event]]:
    """The one writer of a run's own posts (Finish, post step, notice, a routine step's result):
    the first target is the post, any others its `also` DMs. `extra` adds payload fields.
    Returns the post's id and the ledger events."""
    payload: dict[str, Any] = {
        **(extra or {}),
        "text": text,
        "files": files,
        "workflow": workflow.name,
        "run_number": run.number,
        "speaker": None if speaker is None else str(speaker),
        "mention": list(mention),
    }
    ids: list[uuid.UUID] = []
    for index, target in enumerate(targets):
        ids.append(
            await workflow_deliveries.queue_delivery(
                session,
                tenant_id=run.tenant_id,
                workflow_id=run.workflow_id,
                run_id=run.id,
                wait_id=wait_id,
                step_id=step_id if index == 0 else None,
                kind=kind,
                target=target,
                payload=payload if index == 0 else payload | {"mention": []},
                status=status,
                note=note,
            )
        )
    events = [Event("delivery.queued", None, {"delivery_id": str(i), "kind": kind}) for i in ids]
    return ids[0], events


async def queue_deliver(
    session: AsyncSession,
    *,
    run: RunRow,
    workflow: WorkflowRow,
    spec: Spec,
    deliver: Deliver,
    context: dict[str, JsonValue],
    now: datetime,
    kind: Literal["finish", "post"],
    fallback: JsonValue = None,
    step_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, list[Event]]:
    """Render a Finish's or post step's `deliver` and queue it; the message is `fallback`
    unless it names one. Raises `ExpressionError`."""
    home, owner = _home(spec, workflow.home_channel_id), workflow.owner_account_id
    reach = Reach(
        tuple(spec.destinations or ()),
        run.origin,
        frozenset(await workflow_deliveries.opened_threads(session, run.id)),
    )

    def rendered(template: str) -> list[str]:
        return _flat(render(template, context, now=now))

    title = None if deliver.thread is None else " ".join(rendered(deliver.thread))[:100] or None
    to = " ".join(rendered(deliver.to))
    common: dict[str, Any] = {"home": home, "mode": run.mode, "run_as": run.run_as_account_id}
    target = address(
        to,
        templated=bool(expressions_in(deliver.to)),
        owner=owner,
        reach=reach,
        title=title,
        **common,
    )
    also: list[dict[str, Any]] = []
    mention: list[str] = []
    if run.mode == "live":
        for raw in deliver.also or []:
            for value in rendered(raw):
                person = {"owner": owner, "run_as": run.run_as_account_id}.get(value, value)
                to_person = value if value.startswith("person:") else f"person:{person}"
                templated = bool(expressions_in(raw)) and value not in ("owner", "run_as")
                also.append(
                    address(to_person, templated=templated, owner=owner, reach=reach, **common)
                )
        for raw in deliver.mention or []:
            values = rendered(raw)
            if expressions_in(raw) and any(v not in reach.destinations for v in values):
                target = target | {"refused": NOT_A_DESTINATION}
            mention += values
    message = None if deliver.message is None else render(deliver.message, context, now=now)
    files = render(deliver.files, context, now=now)
    return await queue_post(
        session,
        run=run,
        workflow=workflow,
        kind=kind,
        targets=[target, *also[:5]],
        text=_text(fallback if deliver.message is None else message),
        files=cast("list[JsonValue]", files) if isinstance(files, list) else [],
        speaker=run.run_as_account_id,
        mention=mention[:10],
        step_id=step_id,
    )


def _home(spec: Spec, home_channel_id: str | None) -> str | None:
    return spec.home.channel if spec.home is not None else home_channel_id


def _text(value: JsonValue) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)


async def queue_finish(
    session: AsyncSession,
    *,
    run: RunRow,
    workflow: WorkflowRow,
    spec: Spec,
    steps: list[StepView],
    facts: RunFacts,
    node_id: str,
) -> list[Event]:
    """Queue the Finish `node_id`'s post; its message is the output unless it names one."""
    node = spec.nodes.get(node_id)
    if not isinstance(node, FinishNode) or node.deliver is None:
        return []
    output = next((s.output for s in reversed(steps) if s.node_id == node_id), None)
    try:
        _, events = await queue_deliver(
            session,
            run=run,
            workflow=workflow,
            spec=spec,
            deliver=node.deliver,
            context=expression_context(spec, steps, node_id, facts),
            now=facts.now,
            kind="finish",
            fallback=output,
        )
    except ExpressionError:
        # The engine rendered the same templates when the Finish ran, so this is unreachable
        # short of a changed context; the run keeps its output and posts nothing.
        return []
    return events


def _notice_text(workflow_name: str, number: int, end: RunEnd) -> str:
    what = "ran out of time" if end.status == "timed_out" else "failed"
    return f'Run {number} of "{workflow_name}" {what}. Open the run to see why and retry it.'


async def queue_failure_notice(
    session: AsyncSession,
    *,
    run: RunRow,
    workflow: WorkflowRow,
    spec: Spec,
    end: RunEnd,
) -> Event | None:
    """Tell the owner when a live run fails, once per streak of failures."""
    owner = workflow.owner_account_id
    if run.mode != "live" or end.status not in ("failed", "timed_out") or owner is None:
        return None
    if await workflow_runs.previous_end(session, workflow_id=run.workflow_id, before=run.id) in (
        "failed",
        "timed_out",
    ):
        return None
    home = _home(spec, workflow.home_channel_id)
    target = (
        target_for("home", home=home, mode=run.mode, run_as=run.run_as_account_id, owner=owner)
        if home is not None
        else dm_target(owner, home=None)
    )
    _, events = await queue_post(
        session,
        run=run,
        workflow=workflow,
        kind="notice",
        targets=[target],
        text=_notice_text(workflow.name, run.number, end),
        files=[],
        speaker=owner,
    )
    return events[0]


@dataclass(frozen=True)
class WorkflowPost:
    """A workflow post cleared to go, with what the adapter needs to place and send it."""

    row: DeliveryRow
    policy: TenantAccessPolicy
    speaker: str
    channel_id: str
    thread_id: str | None
    #: None for a DM, which no channel rule covers.
    gate: DeliveryGate | None
    link: str | None
    #: A DM's recipient on the platform; `channel_id` is then the target's `via`, or "".
    dm_to: str | None = None
    #: A reminder or an ask's update goes to this earlier post (`reply`, `edit` targets).
    message_id: str | None = None
    #: Under the channel, the thread with this exact title (`titled_thread`).
    title: str | None = None
    #: Discord: the origin message to open the thread on, unless the channel is a thread.
    open_on: str | None = None

    @property
    def notice(self) -> bool:
        return self.row.kind == "notice"

    @property
    def home(self) -> bool:
        return self.row.target.get("home") is True

    @property
    def placed(self) -> bool:
        """A reminder or update in a place an ask already passed the rules for."""
        return self.row.target.get("kind") in ("reply", "edit")


def reach_limits(settings: Settings) -> ReachPlan:
    """The operator's reach limits, which a workflow post stays inside without approval."""
    o = settings.outbound
    return ReachPlan(threads=o.max_threads, places=o.max_places, people=o.max_people)


async def run_link(
    links: WebAt | None, row: DeliveryRow, channel_id: str, thread_id: str | None
) -> str | None:
    """The run's web page, or None where the web app is off or `web_awareness` is Off at the
    channel and thread the post goes to."""
    number = row.payload.get("run_number")
    if links is None or row.run_id is None or number is None:
        return None
    web = await links(row.tenant_id, channel_id, thread_id)
    page = None if web is None else web.workflow(row.workflow_id)
    return None if page is None else f"{page}/runs/{number}"


async def clear_post(
    sessionmaker: async_sessionmaker[AsyncSession],
    row: DeliveryRow,
    *,
    platform: str,
    dm_policies: Mapping[uuid.UUID, DirectMessagePolicy],
    links: WebAt | None = None,
    membership_resolver: MembershipResolver | None = None,
) -> WorkflowPost | str:
    """The post with its policy if it may go at all, else the skip note.

    `dm_policies` is the deprecated env DM policy (`env_admits`): a DM it leaves out is skipped.

    Asked before the adapter resolves or sends anything, and again before each file.
    """
    target = row.target
    kind = target.get("kind")
    async with sessionmaker() as session:
        tenant = await get_tenant(session, row.tenant_id)
        if tenant is None or tenant.archived_at is not None:
            return "tenant_archived"
        if isinstance(refused := target.get("refused"), str):
            return refused
        if row.kind in ("ask", "reminder") and row.wait_id is not None:
            wait = await workflow_waits.get_wait(session, row.wait_id, tenant_id=row.tenant_id)
            if wait is None or wait.status != "waiting":
                return ASK_CLOSED
        dm_to: str | None = None
        if kind == "dm":
            recipient = _uuid(target.get("account_id"))
            if recipient is None:
                return "destination_unavailable"
            if not await dm_enabled(session, tenant_id=row.tenant_id):
                return DM_OFF
            dm_to = (
                await platform_external_ids(
                    session, tenant_id=row.tenant_id, platform=platform, account_ids=[recipient]
                )
            ).get(recipient)
            if dm_to is None:
                return "recipient_not_on_platform"
            if not env_admits(dm_policies, row.tenant_id, dm_to):
                return DM_OFF
            channel_id = str(target.get("via") or "")
        else:
            channel_id = target.get("channel_id")
            if not isinstance(channel_id, str) or not channel_id:
                return "destination_unavailable"
        try:
            policy = await load_access_policy(session, tenant_id=row.tenant_id)
        except AccessPolicyUnreadable:
            return "access_policy_unreadable"
        thread = target.get("thread_id")
        message = target.get("message_id")
        if kind == "edit":
            # Daimon edits its own ask; no one speaks.
            return WorkflowPost(
                row=row,
                policy=policy,
                speaker="",
                channel_id=channel_id,
                thread_id=thread if isinstance(thread, str) else None,
                gate=None,
                link=None,
                message_id=message if isinstance(message, str) else None,
            )
        account_id = _uuid(row.payload.get("speaker"))
        speaker = (
            None
            if account_id is None
            else (
                await platform_external_ids(
                    session, tenant_id=row.tenant_id, platform=platform, account_ids=[account_id]
                )
            ).get(account_id)
        )
        if account_id is None or speaker is None:
            return "speaker_not_on_platform"
        account = await get_account(session, account_id)
    if platform == "discord":
        # As for a routine's creator: their fresh membership, verified, or nothing.
        subject = await resolve_subject(
            sessionmaker,
            membership_resolver,
            tenant_id=row.tenant_id,
            user_id=speaker,
            require_fresh=True,
        )
        if subject.verified_membership is not True or not authorize(
            policy, subject=subject, action=Action.ACT_FOR_CREATOR, surface=Surface.WORKFLOW
        ):
            return "invoker_not_allowed"
    elif refusal := creator_refusal_for(
        policy,
        creator_platform_user_id=speaker,
        creator_is_admin=account is not None and account.role is Role.ADMIN,
    ):
        return refusal
    gate: DeliveryGate | None = None
    if dm_to is None and kind != "reply":
        try:
            gate = await workflow_delivery_gate(
                sessionmaker,
                tenant_id=row.tenant_id,
                platform=platform,
                channel_id=channel_id,
                agent=str(row.payload.get("workflow") or "workflow"),
            )
        except Exception:  # named boundary: an unreadable audience mode refuses the post
            return "audience_unreadable"
    thread_id = thread if isinstance(thread, str) else None
    title, open_on = target.get("title"), target.get("open_on")
    return WorkflowPost(
        row=row,
        policy=policy,
        speaker=speaker,
        channel_id=channel_id,
        thread_id=thread_id,
        gate=gate,
        link=await run_link(links, row, channel_id, thread_id),
        dm_to=dm_to,
        message_id=message if isinstance(message, str) else None,
        title=title if isinstance(title, str) and kind == "channel" else None,
        open_on=open_on if isinstance(open_on, str) and kind == "channel" else None,
    )


def _uuid(raw: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw) if isinstance(raw, str) else None
    except ValueError:
        return None


async def post_refusal(
    post: WorkflowPost,
    *,
    place_id: str,
    parent_channel_id: str | None = None,
    category_id: str | None = None,
    limits: ReachPlan,
    mentioned: tuple[str, ...] = (),
) -> str | None:
    """Why the placed post must not go, or None. `place_id` is the channel or thread posted
    into; a Slack or Teams thread is placed at its channel. A DM or edit has no gate."""
    within = post.row.target.get("within")
    if isinstance(within, list) and not {place_id, parent_channel_id} & set(
        cast("list[str]", within)
    ):
        return NOT_A_DESTINATION
    if post.gate is None:
        return None
    try:
        audience = await post.gate.audience(place_id, parent_channel_id)
    except Exception:  # named boundary: an unreadable audience refuses the post
        return "audience_unreadable"
    decision = authorize(
        post.policy,
        subject=Subject(),
        action=Action.POST,
        surface=Surface.WORKFLOW,
        place=Place(
            channel_id=place_id, parent_channel_id=parent_channel_id, category_id=category_id
        ),
        hold=post.gate.post_hold,
        audience=audience,
    )
    if decision.reason == "audience_contained":
        return "audience_contained"
    if not decision:
        return "protected_channel"
    call = ReachTally(
        places=frozenset() if post.home else frozenset({place_id}), people=frozenset(mentioned)
    )
    verdict = check_reach(
        plan=ReachPlan(), so_far=ReachTally(), call=call, limits=limits, approved=None
    )
    return None if verdict == "ok" else "reach"


async def _mention_tags(
    sm: async_sessionmaker[AsyncSession], row: DeliveryRow, platform: Platform
) -> str:
    """The post's `mention` list as the platform's tags. A role is named, never pinged: every
    post turns role and group pings off (`render_post`), and Teams posts tag no one."""
    raw = cast("list[object]", row.payload.get("mention") or [])
    values = [str(v) for v in raw]
    people = [a for v in values if (a := _uuid(v.removeprefix("person:"))) is not None]
    async with sm() as session:
        ids = await platform_external_ids(
            session, tenant_id=row.tenant_id, platform=platform, account_ids=people
        )
        names = (
            await web_people.get_people_for_accounts(
                session, tenant_id=row.tenant_id, account_ids=set(people)
            )
            if platform == "teams"
            else {}
        )
    tags: list[str] = []
    for value in values:
        account = _uuid(value.removeprefix("person:"))
        if platform == "teams":
            if account is not None and account in names:
                tags.append(f"@{names[account].display_name}")
        elif account is not None and (user := ids.get(account)) is not None:
            tags.append(f"<@{user}>")
        elif value.startswith("role:"):
            role = value.removeprefix("role:")
            tags.append(f"<@&{role}>" if platform == "discord" else f"<!subteam^{role}>")
    return " ".join(tags)


async def post_text(
    sm: async_sessionmaker[AsyncSession], post: WorkflowPost, platform: Platform
) -> RenderedPost:
    """The post's words with its mentions first, every mention rule, the run link and, unless
    it is the owner's notice, the line naming the workflow."""
    words = str(post.row.payload.get("text") or "")
    if tags := await _mention_tags(sm, post.row, platform):
        words = f"{tags} {words}"
    text = await untag_opted_out(sm, tenant_id=post.row.tenant_id, platform=platform, text=words)
    if post.link is not None:
        text = f"{text}\n\n{post.link}"
    name = str(post.row.payload.get("workflow") or "")
    line = None if post.notice else attribution_line(platform, workflow_name=name)
    return render_post(text, platform, attribution=line)


#: Opens a thread with this title under the post's channel; returns the platform's thread id.
ThreadMaker = Callable[[str], Awaitable[str]]
#: Finds an existing thread with exactly this title on the platform (Discord), or None.
ThreadFinder = Callable[[str], Awaitable[str | None]]


async def titled_thread(
    sm: async_sessionmaker[AsyncSession],
    post: WorkflowPost,
    *,
    platform: str,
    create: ThreadMaker,
    find: ThreadFinder | None = None,
) -> str:
    """The thread titled `post.title` under the post's channel: the one this workflow's posts
    used before, else one the platform finds by that name, else a new one. When two posts race,
    the first recorded thread is the one both use."""
    assert post.title is not None, "only a titled post finds its thread"
    row = post.row
    async with sm() as session:
        known = await workflow_threads.find_thread(
            session,
            workflow_id=row.workflow_id,
            platform=platform,
            channel_id=post.channel_id,
            title=post.title,
        )
    if known is not None:
        return known
    made = (None if find is None else await find(post.title)) or await create(post.title)
    async with sm.begin() as session:
        return await workflow_threads.record_thread(
            session,
            tenant_id=row.tenant_id,
            workflow_id=row.workflow_id,
            platform=platform,
            channel_id=post.channel_id,
            title=post.title,
            thread_id=made,
        )


async def result_job(
    sessionmaker: async_sessionmaker[AsyncSession],
    row: DeliveryRow,
    *,
    platform: str,
    settings: Settings | None = None,
) -> DeliveryJob | None:
    """A routine step's claimed `result` as the job its platform's poster takes, from the run's
    version, fenced on that version staying live under the owner it was queued for and on the
    claim. None when the workflow, run or step is gone."""
    async with sessionmaker() as session:
        flow = await workflows.get_workflow(
            session, tenant_id=row.tenant_id, workflow_id=row.workflow_id
        )
        run = None if row.run_id is None else await workflow_runs.get_run(session, row.run_id)
        version = (
            None
            if flow is None or run is None
            else await workflows.get_version(
                session, tenant_id=row.tenant_id, workflow_id=flow.id, number=run.version
            )
        )
    spec = None if version is None else parse_spec(version.spec)
    node = next(
        (n for n in (spec.nodes if spec else {}).values() if isinstance(n, RoutineNode)), None
    )
    if flow is None or run is None or spec is None or node is None:
        return None
    payload = row.payload
    subject = workflow_subject(
        flow,
        spec,
        node,
        run_id=run.id,
        owner=payload.get("owner"),
        agent_id=str(payload.get("agent_id") or ""),
    )
    owner, lease = _uuid(payload.get("speaker")), row.lease_owner or ""

    async def still_current() -> str | None:
        async with sessionmaker() as session:
            live = await workflows.is_live(
                session, workflow_id=flow.id, version=run.version, owner_account_id=owner
            )
            held = await workflow_deliveries.still_claimed(
                session, row.id, owner=lease, now=datetime.now(UTC)
            )
        return "destination_changed" if not live else None if held else INTERRUPTED

    return DeliveryJob(
        **vars(subject),
        header=await routine_header_shown(sessionmaker, subject, platform, settings),
        still_current=still_current,
        delivery_payload=str(payload.get("text") or ""),
        delivery_note=row.note,
        delivery_hold=payload.get("hold"),
        last_fired_at=run.started_at,
        delivery_id=row.id,
        source="workflow",
    )


def result_poster(
    sessionmaker: async_sessionmaker[AsyncSession],
    post: Poster,
    workflow: WorkflowPoster,
    *,
    platform: str,
    settings: Settings | None = None,
) -> WorkflowPoster:
    """`workflow` for a run's own posts; a routine step's `result` goes through `post`, the
    platform's routine poster, as its routine's would."""

    async def post_row(row: DeliveryRow) -> DeliveryOutcome:
        if row.kind != "result":
            return await workflow(row)
        job = await result_job(sessionmaker, row, platform=platform, settings=settings)
        if job is None:
            return DeliveryOutcome(status="skipped", note="destination_changed")
        return await post(job)

    return post_row
