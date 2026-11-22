"""What an admin reads of ops events: the overview and the grouped issues.

The shapers are pure; the loaders read through the stores for one workspace (`tenant_id` is
required and comes from the signed-in person, the operator token or the CLI argument). The web
API, the CLI and the MCP tools all return these models, so `--json` and the web read the same.

An issue is open, resolved or muted. Resolved hides it until a newer event arrives, which
reopens it as `regressed`; muted hides it until reopened. Active means seen in the last
24 hours, new means first seen then.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final, Literal, TypedDict, get_args
from urllib.parse import quote, urlencode

from daimon.core import credit_views
from daimon.core.cursor import Cursor
from daimon.core.ops_catalog import (
    AREA_LABELS,
    CATALOG,
    OPS_AREAS,
    OpsArea,
    OpsCode,
    OpsSeverity,
    SubjectKind,
    reason_label,
    subject_label,
)
from daimon.core.stores import ops_events as store
from daimon.core.stores import platform_names, turn_outcomes, web_people
from daimon.core.stores.direct_messages import list_dm_channel_ids
from daimon.core.stores.domain import (
    FundingMode,
    OpsEventRow,
    OpsIssueGroupRow,
    TenantRow,
    WebPersonRow,
)
from daimon.core.stores.ops_events import SubjectNames
from daimon.core.stores.scoped_config_read import list_propagations_for_tenant
from daimon.core.stores.tenants import get_tenant
from daimon.core.web_admin import audit
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

OpsPeriod = Literal["1h", "24h", "7d", "30d"]
IssueState = Literal["open", "resolved", "muted"]
IssueAction = Literal["resolve", "mute", "reopen"]
IssueStatus = Literal["active", "quiet", "resolved", "muted", "all"]
IssueSort = Literal["last_seen", "events"]
OpsStatus = Literal["healthy", "degraded", "failing"]

OPS_PERIODS: Final[tuple[OpsPeriod, ...]] = get_args(OpsPeriod)
#: Each period as the span of one chart bucket and how many there are.
_BUCKETS: Final[Mapping[OpsPeriod, tuple[timedelta, int]]] = {
    "1h": (timedelta(minutes=5), 12),
    "24h": (timedelta(hours=1), 24),
    "7d": (timedelta(days=1), 7),
    "30d": (timedelta(days=1), 30),
}
ACTIVE_WINDOW: Final = timedelta(hours=24)
ATTENTION_ISSUES: Final = 8
FAILING_RATE: Final = 0.2
FAILING_MIN_ANSWERS: Final = 20
FAILING_PEOPLE: Final = 3
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_AUDIT_ACTIONS: Final[Mapping[IssueAction, str]] = {
    "resolve": "Resolved issue",
    "mute": "Muted issue",
    "reopen": "Reopened issue",
}
_WINDOW = re.compile(r"(\d{1,4})([mhd])")
_UNITS: Final = {"m": timedelta(minutes=1), "h": timedelta(hours=1), "d": timedelta(days=1)}


def parse_window(text: str) -> timedelta:
    """`15m`, `24h` or `7d` as a length of time; ValueError for anything else."""
    found = _WINDOW.fullmatch(text.strip().lower())
    if found is None or int(found[1]) == 0:
        raise ValueError(f"not a window like 15m, 24h or 7d: {text!r}")
    return int(found[1]) * _UNITS[found[2]]


class _Shape(BaseModel):
    model_config = ConfigDict(frozen=True)


class IssueSubject(_Shape):
    kind: SubjectKind
    id: str | None
    name: str


class Person(_Shape):
    id: str
    name: str


class _IssueBase(_Shape):
    fingerprint: str
    code: OpsCode
    title: str
    area: OpsArea
    severity: OpsSeverity
    subject: IssueSubject | None
    reason: str | None
    reason_label: str | None
    events: int
    first_seen: datetime
    last_seen: datetime
    buckets: list[int]
    """Event counts per chart bucket of the period, oldest first, ending now."""
    new: bool
    active: bool
    state: IssueState
    regressed: bool
    state_by: Person | None
    state_at: datetime | None


class _IssueFields(TypedDict):
    fingerprint: str
    code: OpsCode
    title: str
    area: OpsArea
    severity: OpsSeverity
    subject: IssueSubject | None
    reason: str | None
    reason_label: str | None
    events: int
    first_seen: datetime
    last_seen: datetime
    buckets: list[int]
    new: bool
    active: bool
    state: IssueState
    regressed: bool
    state_by: Person | None
    state_at: datetime | None


class Issue(_IssueBase):
    people: int


class IssueDetail(_IssueBase):
    people: list[Person]
    agent: Person | None
    release: str | None


class EventPerson(_Shape):
    id: str | None
    """None for an account with no person in this workspace."""
    name: str


class IssueEvent(_Shape):
    at: datetime
    count: int
    person: EventPerson | None
    platform: str | None
    where_label: str | None
    reason_label: str | None
    thread_url: str | None
    note: str | None


class SeriesPoint(_Shape):
    at: datetime
    answered: int
    """Answers that did not fail, so `answered + failed` is every answer."""
    failed: int


class AreaCount(_Shape):
    area: OpsArea
    label: str
    events: int


class AnswerFacts(_Shape):
    p50_ms: int | None
    p95_ms: int | None
    busy: int
    recovered: int
    cost_per_answer_usd: float | None


class WorkspaceFacts(_Shape):
    agents: int | None
    """None where the caller cannot list the workspace's agents."""
    people: int
    channels: int
    routines_on: int
    routines_total: int
    watches_on: int
    watches_total: int
    watches_failing: int
    funding_mode: FundingMode


class OpsOverview(_Shape):
    updated_at: datetime
    status: OpsStatus
    answers: int
    failed: int
    failed_rate: float
    dm_failures: int
    routines_failing: int
    routines_total: int
    balance_usd: float
    runway_days: float | None
    series: list[SeriesPoint]
    by_area: list[AreaCount]
    attention: list[Issue]
    answers_facts: AnswerFacts
    workspace: WorkspaceFacts


# -- pure shapers -----------------------------------------------------------------------


def state_of(group: OpsIssueGroupRow) -> tuple[IssueState, bool]:
    """The issue's state and whether it came back: a resolved issue seen again is open."""
    held = group.state
    if held is None:
        return "open", False
    if held.state == "muted":
        return "muted", False
    if group.last_seen > held.at:
        return "open", True
    return "resolved", False


def is_open(group: OpsIssueGroupRow) -> bool:
    return state_of(group)[0] == "open"


def subject_of(
    event: OpsEventRow, names: SubjectNames, agent_names: Mapping[str, str]
) -> IssueSubject | None:
    """What the issue is about, named as the workspace knows it."""
    kind = CATALOG[event.code].subject
    if kind == "agent" and event.agent_id:
        name = agent_names.get(event.agent_id) or names.agents.get(event.agent_id) or "Agent"
        return IssueSubject(kind=kind, id=event.agent_id, name=name)
    if kind == "routine" and event.routine_id:
        title = names.routines.get(event.routine_id)
        return IssueSubject(kind=kind, id=str(event.routine_id), name=title or "Routine")
    if kind == "watch" and event.watch_id:
        owner = names.watches.get(event.watch_id)
        return IssueSubject(
            kind=kind, id=str(event.watch_id), name=f"Watch by {owner}" if owner else "Watch"
        )
    key = event.platform if kind == "platform" else event.subject_key
    if kind in ("agent", "routine", "watch") or not key:
        return None
    return IssueSubject(kind=kind, id=key, name=subject_label(kind, key))


def person_of(
    account_id: uuid.UUID | None, people: Mapping[uuid.UUID, WebPersonRow]
) -> Person | None:
    if account_id is None:
        return None
    found = people.get(account_id)
    return Person(id=str(found.id), name=found.display_name) if found else None


def issue_of(
    group: OpsIssueGroupRow,
    *,
    buckets: list[int],
    now: datetime,
    names: SubjectNames,
    agent_names: Mapping[str, str],
    people: Mapping[uuid.UUID, WebPersonRow],
) -> Issue:
    return Issue(**_fields(group, buckets, now, names, agent_names, people), people=group.people)


def _fields(
    group: OpsIssueGroupRow,
    buckets: list[int],
    now: datetime,
    names: SubjectNames,
    agent_names: Mapping[str, str],
    people: Mapping[uuid.UUID, WebPersonRow],
) -> _IssueFields:
    latest = group.latest
    state, regressed = state_of(group)
    held = group.state
    by = person_of(held.by_account_id, people) if held else None
    return {
        "fingerprint": group.fingerprint,
        "code": latest.code,
        "title": CATALOG[latest.code].title,
        "area": latest.area,
        "severity": latest.severity,
        "subject": subject_of(latest, names, agent_names),
        "reason": latest.reason,
        "reason_label": reason_label(latest.reason),
        "events": group.events,
        "first_seen": group.first_seen,
        "last_seen": group.last_seen,
        "buckets": buckets,
        "new": group.first_seen >= now - ACTIVE_WINDOW,
        "active": group.last_seen >= now - ACTIVE_WINDOW,
        "state": state,
        "regressed": regressed,
        "state_by": by if state != "open" else None,
        "state_at": held.at if held and state != "open" else None,
    }


def matches_status(issue: Issue, status: IssueStatus) -> bool:
    """Active and quiet are among open issues only."""
    if status == "all":
        return True
    if status in ("active", "quiet"):
        return issue.state == "open" and issue.active == (status == "active")
    return issue.state == status


def matches_query(issue: Issue, query: str) -> bool:
    text = f"{issue.title} {issue.subject.name if issue.subject else ''} {issue.reason_label or ''}"
    return query.lower() in text.lower()


def sorted_issues(issues: Sequence[Issue], sort: IssueSort) -> list[Issue]:
    """Newest first (or most events first), ties by fingerprint, so a page is stable."""
    if sort == "events":
        return sorted(issues, key=lambda i: (-i.events, -i.last_seen.timestamp(), i.fingerprint))
    return sorted(issues, key=lambda i: (i.last_seen, i.fingerprint), reverse=True)


def issue_cursor(issue: Issue) -> Cursor:
    return issue.last_seen, uuid.UUID(issue.fingerprint)


def after_cursor(issues: Sequence[Issue], sort: IssueSort, cursor: Cursor | None) -> list[Issue]:
    """The sorted issues after the one the cursor names. For the events order the issue is
    found by its fingerprint; one that has since gone leaves nothing after it."""
    if cursor is None:
        return list(issues)
    if sort == "events":
        at = next((i for i, x in enumerate(issues) if issue_cursor(x)[1] == cursor[1]), None)
        return [] if at is None else list(issues[at + 1 :])
    return [x for x in issues if issue_cursor(x) < cursor]


def attention_of(issues: Sequence[Issue]) -> list[Issue]:
    """The open issues seen in the last day: errors first, then newest."""
    live = [i for i in issues if i.state == "open" and i.active]
    live.sort(key=lambda i: (i.last_seen, i.fingerprint), reverse=True)
    live.sort(key=lambda i: i.severity != "error")
    return live[:ATTENTION_ISSUES]


def status_of(*, answers: int, failed: int, hour: Sequence[OpsIssueGroupRow]) -> OpsStatus:
    """From the last hour: failing when over 20% of at least 20 answers failed or an open error
    issue reached 3 people; degraded when any open error issue was seen."""
    errors = [g for g in hour if is_open(g) and g.latest.severity == "error"]
    if (answers >= FAILING_MIN_ANSWERS and failed / answers > FAILING_RATE) or any(
        g.people >= FAILING_PEOPLE for g in errors
    ):
        return "failing"
    return "degraded" if errors else "healthy"


_SLACK_ID = re.compile(r"[A-Z][A-Z0-9]{2,}")
_SLACK_TS = re.compile(r"\d+\.\d+")
_TEAMS_ROOT = ";messageid="


def _slack_url(team: str, event: OpsEventRow) -> str | None:
    channel = event.channel_id
    if not channel or not _SLACK_ID.fullmatch(channel) or not _SLACK_ID.fullmatch(team):
        return None
    url = f"https://app.slack.com/client/{team}/{channel}"
    ts = event.thread_id
    return f"{url}/thread/{channel}-{ts}" if ts and _SLACK_TS.fullmatch(ts) else url


def _teams_url(entra_tenant: str, event: OpsEventRow) -> str | None:
    """A channel thread; chats and direct conversations have no link others can open."""
    channel = (event.channel_id or "").split(";", 1)[0]
    if "@thread.tacv2" not in channel:
        return None
    root = (event.thread_id or "").partition(_TEAMS_ROOT)[2]
    if not root.isdigit():
        return None
    query = urlencode({"tenantId": entra_tenant, "parentMessageId": root})
    return f"https://teams.microsoft.com/l/message/{quote(channel, safe=':@')}/{root}?{query}"


def thread_url(tenant: TenantRow | None, event: OpsEventRow) -> str | None:
    """A link to the conversation, built from the ids stored on the event."""
    if tenant is None or tenant.platform != event.platform:
        return None
    if tenant.platform == "slack":
        return _slack_url(tenant.external_id, event)
    if tenant.platform == "teams":
        return _teams_url(tenant.external_id, event)
    if tenant.platform != "discord":
        return None
    target = event.thread_id or event.channel_id
    if not target or not target.isdigit() or not tenant.external_id.isdigit():
        return None
    return f"https://discord.com/channels/{tenant.external_id}/{target}"


def where_of(event: OpsEventRow, channels: Mapping[str, str]) -> str | None:
    """Where it happened: a direct message, a named channel, or the platform."""
    if event.area == "dm":
        return "Direct message"
    named = channels.get(event.channel_id or "")
    if named:
        return named if named.startswith("#") else f"#{named}"
    return event.platform.capitalize() if event.platform else None


def event_of(
    event: OpsEventRow,
    *,
    tenant: TenantRow | None,
    people: Mapping[uuid.UUID, WebPersonRow],
    channels: Mapping[str, str],
) -> IssueEvent:
    found = people.get(event.account_id) if event.account_id else None
    person = (
        EventPerson(
            id=str(found.id) if found else None, name=found.display_name if found else "Someone"
        )
        if event.account_id
        else None
    )
    return IssueEvent(
        at=event.occurred_at,
        count=event.count,
        person=person,
        platform=event.platform,
        where_label=where_of(event, channels),
        reason_label=reason_label(event.reason),
        thread_url=thread_url(tenant, event),
        note=event.note,
    )


def period_span(period: OpsPeriod) -> tuple[timedelta, int]:
    """One chart bucket's length and the number of buckets."""
    return _BUCKETS[period]


# -- loaders ----------------------------------------------------------------------------


async def _context(
    session: AsyncSession, tenant_id: uuid.UUID, groups: Sequence[OpsIssueGroupRow]
) -> tuple[SubjectNames, dict[uuid.UUID, WebPersonRow]]:
    names = await store.subject_names(
        session,
        tenant_id=tenant_id,
        routine_ids={g.latest.routine_id for g in groups if g.latest.routine_id},
        watch_ids={g.latest.watch_id for g in groups if g.latest.watch_id},
    )
    accounts = {g.state.by_account_id for g in groups if g.state and g.state.by_account_id}
    people = await web_people.get_people_for_accounts(
        session, tenant_id=tenant_id, account_ids=accounts
    )
    return names, people


async def _issues(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    now: datetime,
    period: OpsPeriod,
    agent_names: Mapping[str, str],
    window: timedelta | None = None,
    area: OpsArea | None = None,
    severity: OpsSeverity | None = None,
    agent_id: str | None = None,
) -> list[Issue]:
    span, buckets = period_span(period)
    groups = await store.list_issue_groups(
        session,
        tenant_id=tenant_id,
        since=now - (window or span * buckets),
        area=area,
        severity=severity,
        agent_id=agent_id,
    )
    names, people = await _context(session, tenant_id, groups)
    series = await store.issue_buckets(
        session,
        tenant_id=tenant_id,
        fingerprints=[g.fingerprint for g in groups],
        now=now,
        span=span,
        buckets=buckets,
    )
    return [
        issue_of(
            g,
            buckets=series[g.fingerprint],
            now=now,
            names=names,
            agent_names=agent_names,
            people=people,
        )
        for g in groups
    ]


async def load_issues(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    now: datetime,
    period: OpsPeriod = "24h",
    status: IssueStatus = "active",
    area: OpsArea | None = None,
    severity: OpsSeverity | None = None,
    agent_id: str | None = None,
    query: str | None = None,
    sort: IssueSort = "last_seen",
    agent_names: Mapping[str, str] | None = None,
    window: timedelta | None = None,
) -> list[Issue]:
    """The workspace's issues seen in `period` (or in the last `window`, with the period's
    trend), filtered and sorted; the caller pages them."""
    issues = await _issues(
        session,
        tenant_id=tenant_id,
        now=now,
        period=period,
        agent_names=agent_names or {},
        window=window,
        area=area,
        severity=severity,
        agent_id=agent_id,
    )
    kept = [
        i
        for i in issues
        if matches_status(i, status) and (not query or matches_query(i, query.strip()))
    ]
    return sorted_issues(kept, sort)


async def _one(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    fingerprint: str,
    now: datetime,
    period: OpsPeriod,
    agent_names: Mapping[str, str],
) -> tuple[OpsIssueGroupRow, _IssueFields, SubjectNames] | None:
    found = await store.list_issue_groups(
        session, tenant_id=tenant_id, since=_EPOCH, fingerprint=fingerprint, limit=1
    )
    if not found:
        return None
    span, count = period_span(period)
    buckets = await store.issue_buckets(
        session, tenant_id=tenant_id, fingerprints=[fingerprint], now=now, span=span, buckets=count
    )
    names, people = await _context(session, tenant_id, found)
    fields = _fields(found[0], buckets[fingerprint], now, names, agent_names, people)
    return found[0], fields, names


async def load_issue_summary(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    fingerprint: str,
    now: datetime,
    period: OpsPeriod = "24h",
    agent_names: Mapping[str, str] | None = None,
) -> Issue | None:
    """One issue as the list shows it, over its whole life; None if the workspace has none."""
    one = await _one(
        session,
        tenant_id=tenant_id,
        fingerprint=fingerprint,
        now=now,
        period=period,
        agent_names=agent_names or {},
    )
    return None if one is None else Issue(**one[1], people=one[0].people)


async def load_issue(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    fingerprint: str,
    now: datetime,
    period: OpsPeriod = "24h",
    agent_names: Mapping[str, str] | None = None,
) -> IssueDetail | None:
    """One issue over its whole life, with its people; None if the workspace has none."""
    agents = agent_names or {}
    one = await _one(
        session,
        tenant_id=tenant_id,
        fingerprint=fingerprint,
        now=now,
        period=period,
        agent_names=agents,
    )
    if one is None:
        return None
    group, fields, names = one
    account_ids = await store.list_issue_account_ids(
        session, tenant_id=tenant_id, fingerprint=fingerprint
    )
    everyone = await web_people.get_people_for_accounts(
        session, tenant_id=tenant_id, account_ids=account_ids
    )
    agent_id = group.latest.agent_id
    return IssueDetail(
        **fields,
        people=[p for a in account_ids if (p := person_of(a, everyone))],
        agent=Person(
            id=agent_id, name=agents.get(agent_id) or names.agents.get(agent_id) or "Agent"
        )
        if agent_id
        else None,
        release=group.latest.release,
    )


async def load_issue_events(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    fingerprint: str,
    before: Cursor | None,
    limit: int,
) -> list[tuple[Cursor, IssueEvent]]:
    """One issue's events with each one's cursor, newest first, `limit` after `before`."""
    rows = await store.list_issue_events(
        session, tenant_id=tenant_id, fingerprint=fingerprint, before=before, limit=limit
    )
    tenant = await get_tenant(session, tenant_id)
    people = await web_people.get_people_for_accounts(
        session, tenant_id=tenant_id, account_ids={r.account_id for r in rows if r.account_id}
    )
    channels = (
        await platform_names.get_channel_names(
            session,
            tenant_id=tenant_id,
            platform=tenant.platform,
            channel_ids={r.channel_id for r in rows if r.channel_id},
        )
        if tenant
        else {}
    )
    return [
        ((r.occurred_at, r.id), event_of(r, tenant=tenant, people=people, channels=channels))
        for r in rows
    ]


async def change_issue_state(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    fingerprint: str,
    action: IssueAction,
    by_account_id: uuid.UUID | None,
    now: datetime,
) -> Issue | None:
    """Resolve, mute or reopen an issue and record it in the audit trail; the issue as it now
    stands, or None if the workspace has no such issue. The caller commits."""
    if (
        await load_issue_summary(session, tenant_id=tenant_id, fingerprint=fingerprint, now=now)
        is None
    ):
        return None
    if action == "reopen":
        await store.clear_issue_state(session, tenant_id=tenant_id, fingerprint=fingerprint)
    else:
        await store.set_issue_state(
            session,
            tenant_id=tenant_id,
            fingerprint=fingerprint,
            state="resolved" if action == "resolve" else "muted",
            by_account_id=by_account_id,
            at=now,
        )
    current = await load_issue_summary(
        session, tenant_id=tenant_id, fingerprint=fingerprint, now=now
    )
    assert current is not None, "the issue was just found"
    await audit(
        session,
        tenant_id=tenant_id,
        actor_account_id=by_account_id,
        action=_AUDIT_ACTIONS[action],
        target=current.title,
        detail=current.subject.name if current.subject else None,
        at=now,
    )
    return current


async def load_ops_overview(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    now: datetime,
    period: OpsPeriod,
    markup: Decimal,
    agents: int | None = None,
    agent_names: Mapping[str, str] | None = None,
) -> OpsOverview:
    """The workspace's health at a glance: answers, failures, issues and the facts around them."""
    span, count = period_span(period)
    since = now - span * count
    issues = await _issues(
        session, tenant_id=tenant_id, now=now, period=period, agent_names=agent_names or {}
    )
    hour = await store.list_issue_groups(
        session, tenant_id=tenant_id, since=now - timedelta(hours=1)
    )
    stats = await turn_outcomes.answer_stats(session, tenant_id=tenant_id, since=since, until=now)
    series = await turn_outcomes.answer_series(
        session, tenant_id=tenant_id, now=now, span=span, buckets=count
    )
    last_hour = await turn_outcomes.answer_stats(
        session, tenant_id=tenant_id, since=now - timedelta(hours=1), until=now
    )
    by_area = await store.count_by_area(session, tenant_id=tenant_id, since=since)
    counts = await store.workspace_counts(session, tenant_id=tenant_id)
    spend = await credit_views.spend_overview(session, tenant_id=tenant_id, now=now, markup=markup)
    tenant = await get_tenant(session, tenant_id)
    funding: FundingMode = tenant.funding_mode if tenant else "prepaid"
    _, channel_rows = await list_propagations_for_tenant(session, tenant_id=tenant_id)
    direct = await list_dm_channel_ids(session, tenant_id=tenant_id)
    per_day = spend.spent_usd / 30
    return OpsOverview(
        updated_at=now,
        status=status_of(answers=last_hour.answers, failed=last_hour.failed, hour=hour),
        answers=stats.answers,
        failed=stats.failed,
        failed_rate=stats.failed / stats.answers if stats.answers else 0.0,
        dm_failures=sum(i.events for i in issues if i.area == "dm" and i.state == "open"),
        routines_failing=counts.routines_failing,
        routines_total=counts.routines_total,
        balance_usd=float(spend.balance_usd),
        runway_days=float(spend.balance_usd / per_day)
        if funding == "prepaid" and per_day > 0 and spend.balance_usd > 0
        else None,
        series=[
            SeriesPoint(at=now - span * (count - i), answered=answers - failed, failed=failed)
            for i, (answers, failed) in enumerate(series)
        ],
        by_area=[
            AreaCount(area=a, label=AREA_LABELS[a], events=by_area.get(a, 0)) for a in OPS_AREAS
        ],
        attention=attention_of(issues),
        answers_facts=AnswerFacts(
            p50_ms=None if stats.p50_ms is None else round(stats.p50_ms),
            p95_ms=None if stats.p95_ms is None else round(stats.p95_ms),
            busy=stats.busy,
            recovered=stats.recovered,
            cost_per_answer_usd=float(stats.cost_usd / stats.costed) if stats.costed else None,
        ),
        workspace=WorkspaceFacts(
            agents=agents,
            people=await web_people.count_people(session, tenant_id=tenant_id),
            channels=len({row.channel_id for row in channel_rows} - direct),
            routines_on=counts.routines_on,
            routines_total=counts.routines_total,
            watches_on=counts.watches_on,
            watches_total=counts.watches_total,
            watches_failing=counts.watches_failing,
            funding_mode=funding,
        ),
    )
