"""Workflow posts: what a run queues when it ends, and the rules each post passes.

A Finish with `deliver` queues one `finish` post when its run succeeds. A live run that
fails or times out queues one `notice` to the owner, unless the run before it failed
too. A Finish speaks for the run-as person, a notice for the owner. At post time the
rules of a routine's result apply (`clear_post`, then `post_refusal` once the adapter has
placed the channel): a live workspace, a readable access policy, a speaker still allowed
to invoke, no write protection, the audience gate, and reach. The adapter checks that
the speaker may post in the channel themselves.

A post to a person's DM (`owner`, `run_as`, an ask's addressee, and every test run, which
delivers to the tester) goes on the workspace's platform when it has DMs on and the person
is linked there; the channel rules then have no channel to check.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Final

from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.authz import Action, Place, Subject, Surface, authorize
from daimon.core.config import Settings, WebSettings
from daimon.core.membership import MembershipResolver
from daimon.core.membership_subject import resolve_subject
from daimon.core.outbound.mentions import Platform, RenderedPost, attribution_line, render_post
from daimon.core.outbound.opt_out import untag_opted_out
from daimon.core.outbound.reach import ReachPlan, ReachTally, check_reach
from daimon.core.routine_delivery import creator_refusal_for
from daimon.core.routine_hold import DeliveryGate, workflow_delivery_gate
from daimon.core.stores import workflow_deliveries, workflow_runs, workflow_waits
from daimon.core.stores.access_policy import AccessPolicyUnreadable, load_access_policy
from daimon.core.stores.accounts import get_account
from daimon.core.stores.direct_messages import dm_enabled
from daimon.core.stores.domain import Role
from daimon.core.stores.identity import platform_external_ids
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.workflow_deliveries import DeliveryRow
from daimon.core.stores.workflow_runs import RunRow
from daimon.core.stores.workflows import WorkflowRow
from daimon.core.web_links import WebLinks
from daimon.core.workflows.engine import Event, RunEnd, RunFacts, StepView, expression_context
from daimon.core.workflows.expressions import ExpressionError, render
from daimon.core.workflows.spec import FinishNode, Spec
from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

__all__ = [
    "ASK_CLOSED",
    "DM_OFF",
    "WorkflowPost",
    "clear_post",
    "post_refusal",
    "post_text",
    "queue_failure_notice",
    "queue_finish",
    "reach_limits",
    "target_for",
]

DM_OFF: Final[str] = "dm_disabled"
#: An ask or reminder whose wait closed before it went: nothing to post, no one to tell.
ASK_CLOSED: Final[str] = "ask_closed"
NOTICE_TITLE: Final[str] = "Workflow run failed"
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
) -> Event | None:
    """Queue the Finish `node_id`'s post; its message is the output unless it names one."""
    owner, home = workflow.owner_account_id, _home(spec, workflow.home_channel_id)
    node = spec.nodes.get(node_id)
    if not isinstance(node, FinishNode) or node.deliver is None:
        return None
    output = next((s.output for s in reversed(steps) if s.node_id == node_id), None)
    context = expression_context(spec, steps, node_id, facts)
    try:
        message = render(node.deliver.message, context, now=facts.now)
        files = render(node.deliver.files, context, now=facts.now)
    except ExpressionError:
        # The engine rendered the same templates when the Finish ran, so this is unreachable
        # short of a changed context; the run keeps its output and posts nothing.
        return None
    target = target_for(
        node.deliver.to, home=home, mode=run.mode, run_as=run.run_as_account_id, owner=owner
    )
    delivery_id = await workflow_deliveries.queue_delivery(
        session,
        tenant_id=run.tenant_id,
        workflow_id=run.workflow_id,
        run_id=run.id,
        kind="finish",
        target=target,
        payload={
            "text": _text(output if node.deliver.message is None else message),
            "files": files if isinstance(files, list) else [],
            "workflow": workflow.name,
            "run_number": run.number,
            "speaker": None if run.run_as_account_id is None else str(run.run_as_account_id),
        },
    )
    return Event("delivery.queued", None, {"delivery_id": str(delivery_id), "kind": "finish"})


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
    delivery_id = await workflow_deliveries.queue_delivery(
        session,
        tenant_id=run.tenant_id,
        workflow_id=run.workflow_id,
        run_id=run.id,
        kind="notice",
        target=target,
        payload={
            "text": _notice_text(workflow.name, run.number, end),
            "files": [],
            "workflow": workflow.name,
            "run_number": run.number,
            "speaker": str(owner),
        },
    )
    return Event("delivery.queued", None, {"delivery_id": str(delivery_id), "kind": "notice"})


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


def run_link(web: WebSettings | None, row: DeliveryRow) -> str | None:
    links = WebLinks.from_settings(web)
    number = row.payload.get("run_number")
    if links is None or "workflows" not in links.modules or row.run_id is None or number is None:
        return None
    root = links.for_tenant(row.tenant_id, admin=False).root()
    return f"{root}/workflows/{row.workflow_id}/runs/{number}"


async def clear_post(
    sessionmaker: async_sessionmaker[AsyncSession],
    row: DeliveryRow,
    *,
    platform: str,
    web: WebSettings | None = None,
    membership_resolver: MembershipResolver | None = None,
) -> WorkflowPost | str:
    """The post with its policy if it may go at all, else the skip note.

    Asked before the adapter resolves or sends anything, and again before each file.
    """
    target = row.target
    kind = target.get("kind")
    async with sessionmaker() as session:
        tenant = await get_tenant(session, row.tenant_id)
        if tenant is None or tenant.archived_at is not None:
            return "tenant_archived"
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
    return WorkflowPost(
        row=row,
        policy=policy,
        speaker=speaker,
        channel_id=channel_id,
        thread_id=thread if isinstance(thread, str) else None,
        gate=gate,
        link=run_link(web, row),
        dm_to=dm_to,
        message_id=message if isinstance(message, str) else None,
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


async def post_text(
    sessionmaker: async_sessionmaker[AsyncSession], post: WorkflowPost, platform: Platform
) -> RenderedPost:
    """The post's words with every mention rule, the run link and, unless it is the owner's
    notice, the line naming the workflow."""
    text = await untag_opted_out(
        sessionmaker,
        tenant_id=post.row.tenant_id,
        platform=platform,
        text=str(post.row.payload.get("text") or ""),
    )
    if post.link is not None:
        text = f"{text}\n\n{post.link}"
    name = str(post.row.payload.get("workflow") or "")
    line = None if post.notice else attribution_line(platform, workflow_name=name)
    return render_post(text, platform, attribution=line)
