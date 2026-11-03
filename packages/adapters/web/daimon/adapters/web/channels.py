"""`/api/channels/*`: every channel of the workspace, one channel, its changes and history.

Admins (`channels.manage`) see every channel and change each section through
`daimon.core.channel_changes`, sending the value they last read: a value that moved
since is a 409. Members (`channels.view`) read only channels the platform shows
them (`channel_visibility`), never one kept to its own agents, and no history.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal, cast

from anthropic import AsyncAnthropic
from daimon.adapters.web.access import is_admin
from daimon.adapters.web.admin_common import listed_agents, names_by_account
from daimon.adapters.web.audit import audit
from daimon.adapters.web.caller import WebCaller, requires
from daimon.adapters.web.channel_agents import AgentRefJson, agent_refs
from daimon.adapters.web.channel_visibility import readable_channels
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.access_policy import ChannelReaders, ChannelRule, ChannelWriters
from daimon.core.agent_pins import agent_pin_names
from daimon.core.authz import Subject
from daimon.core.channel_budget import ChannelBudgetError, ChannelBudgetStatus, parse_budget_spec
from daimon.core.channel_changes import (
    BudgetValue,
    ChannelActor,
    ChannelChangeRefused,
    Expected,
    clear_budget,
    clear_default_agent,
    keep_agent,
    raise_budget,
    release_agent,
    set_access,
    set_budget,
    set_default_agent,
)
from daimon.core.channel_directory import (
    WEB_ROW_ID,
    ChannelDirectory,
    ChannelKind,
    ChannelRow,
    ChannelStatusName,
    Sourced,
    ValueSource,
    load_channel_directory,
    load_channel_row,
)
from daimon.core.channel_rules import READERS_LABELS, WRITERS_LABELS, ChannelRuleRefused
from daimon.core.errors import DaimonError, StaleChannelValue
from daimon.core.stores.access_policy import AccessPolicyUnreadable
from daimon.core.stores.agent_tool_rules import list_channel_rule_history
from daimon.core.stores.channel_changes import WORKSPACE_ID, list_changes
from daimon.core.stores.domain import ChannelSetting
from daimon.core.stores.platform_names import get_channel_names
from daimon.core.stores.tenants import get_tenant
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

ChannelViewer = Annotated[WebCaller, Depends(requires("channels.view"))]
ChannelAdmin = Annotated[WebCaller, Depends(requires("channels.manage"))]

_CENT = Decimal("0.01")
_UNREADABLE = "The access settings could not be read. Try again later."
_NOT_FOUND = "No such channel."
_NO_WEB_BUDGET = "Web chat has no channel budget."
_BAD_BUDGET = "Enter a budget in dollars and pick a window."
_AUDIT_DETAIL = {
    "default_agent": "Default agent",
    "kept_agents": "Agents kept here",
    "access": "Who can read and post",
    "budget": "Budget",
    "participation": "Following threads",
    "progress_plans": "Progress plans",
    "environment": "Sandbox",
    "skills": "Skills",
    "tool_rule": "Tool rules",
    "provider_pin": "Provider",
    "channel_copy": "Channel copy",
    "thread": "Thread settings",
}


def _usd(value: Decimal) -> float:
    return float(value.quantize(_CENT))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class SourcedJson(BaseModel):
    value: str | None
    source: ValueSource | None


class ParentJson(BaseModel):
    id: str
    name: str | None


class BudgetJson(BaseModel):
    limit_usd: str
    window: Literal["monthly", "total", "fixed"]
    starts_at: str | None
    ends_at: str | None
    spent_usd: float
    remaining_usd: float
    percent_used: int
    active: bool
    exceeded: bool


class SpendJson(BaseModel):
    month_usd: float
    last_30_days_usd: float


class PathNodeJson(BaseModel):
    id: str
    name: str | None
    kind: ChannelKind


class ChannelJson(BaseModel):
    id: str
    platform: str
    name: str | None
    kind: ChannelKind
    status: ChannelStatusName
    parent: ParentJson | None
    path: list[PathNodeJson]
    """Ancestors, root first: category and channel, or team."""
    last_seen_at: str | None
    agent: SourcedJson
    environment: SourcedJson
    readers: ChannelReaders
    writers: ChannelWriters
    kept_agents: list[str]
    tags: list[Literal["kept_inside", "locked", "shared"]]
    budget: BudgetJson | None
    """None for a member, like `spend`: money figures are an admin's."""
    spend: SpendJson | None
    last_active_at: str | None


class ChoiceJson(BaseModel):
    value: str
    label: str


class ChannelDetailJson(ChannelJson):
    readers_label: str
    writers_label: str
    readers_options: list[ChoiceJson]
    writers_options: list[ChoiceJson]
    can_manage: bool
    budget_editable: bool
    agents: dict[str, AgentRefJson]
    """The agents the row's keys name (its answering and kept agents), by key."""


class ChannelListJson(BaseModel):
    platform: str
    default_agent: SourcedJson
    default_environment: SourcedJson
    channels: list[ChannelJson]
    agents: dict[str, AgentRefJson]
    """The agents the list's keys name (the default, answering and kept agents), by key."""


class ChangeResultJson(BaseModel):
    channel: ChannelDetailJson
    notes: list[str]


class HistoryActorJson(BaseModel):
    id: str
    name: str | None


class HistoryEntryJson(BaseModel):
    at: str
    setting: ChannelSetting | Literal["tool_rule"]
    before: object
    after: object
    actor: HistoryActorJson | None
    surface: Literal["web", "chat", "panel", "cli"] | None


class HistoryJson(BaseModel):
    entries: list[HistoryEntryJson]
    agents: dict[str, AgentRefJson]
    """The agents the entries' keys name, by key."""


class DefaultAgentChange(BaseModel):
    agent: str | None
    expected: str | None


class KeptAgentChange(BaseModel):
    action: Literal["add", "remove"]
    agent: str
    expected: list[str]


class RuleJson(BaseModel):
    readers: ChannelReaders
    writers: ChannelWriters


class AccessChange(BaseModel):
    readers: ChannelReaders | None = None
    writers: ChannelWriters | None = None
    expected: RuleJson
    copy_agent: bool = False
    copy_from: str | None = None
    release_agents: bool = False
    keep_rules: bool = False


class BudgetExpected(BaseModel):
    limit_usd: str
    window: Literal["monthly", "total", "fixed"]
    starts_at: str | None = None
    ends_at: str | None = None


class BudgetChange(BaseModel):
    action: Literal["set", "raise", "clear"]
    limit_usd: str | None = None
    window: Literal["monthly", "total", "fixed"] | None = None
    starts_at: str | None = None
    ends_at: str | None = None
    amount_usd: str | None = None
    expected: BudgetExpected | None


def _sourced(value: Sourced) -> SourcedJson:
    return SourcedJson(value=value.value, source=value.source)


def _budget(status: ChannelBudgetStatus | None) -> BudgetJson | None:
    if status is None:
        return None
    budget = status.budget
    return BudgetJson(
        limit_usd=f"{budget.limit_usd:.2f}",
        window=budget.window,
        starts_at=_iso(budget.starts_at),
        ends_at=_iso(budget.ends_at),
        spent_usd=_usd(status.spent_usd),
        remaining_usd=_usd(status.remaining_usd),
        percent_used=status.percent_used,
        active=status.is_active,
        exceeded=status.is_exceeded,
    )


def channel_json(row: ChannelRow, *, figures: bool) -> ChannelJson:
    """`figures` adds the channel's budget and spend, which members never read."""
    name = row.name
    parent = (
        ParentJson(id=name.parent_id, name=name.parent_name)
        if name is not None and name.parent_id is not None
        else None
    )
    return ChannelJson(
        id=row.channel_id,
        platform=row.platform,
        name="Web chat" if row.channel_id == WEB_ROW_ID else name.name or None if name else None,
        kind=row.kind,
        status=row.status,
        parent=parent,
        path=[PathNodeJson(id=n.id, name=n.name or None, kind=n.kind) for n in row.path],
        last_seen_at=_iso(name.last_seen_at) if name else None,
        agent=_sourced(row.agent),
        environment=_sourced(row.environment),
        readers=row.readers,
        writers=row.writers,
        kept_agents=list(row.kept_agents),
        tags=list(row.tags),
        budget=_budget(row.budget) if figures else None,
        spend=SpendJson(
            month_usd=_usd(row.spend.month_usd), last_30_days_usd=_usd(row.spend.recent_usd)
        )
        if figures
        else None,
        last_active_at=_iso(row.spend.last_active_at),
    )


def row_agent_keys(row: ChannelRow) -> list[str | None]:
    return [row.agent.value, *row.kept_agents]


def detail_json(
    row: ChannelRow, *, manage: bool, figures: bool, agents: dict[str, AgentRefJson]
) -> ChannelDetailJson:
    return ChannelDetailJson(
        **channel_json(row, figures=figures).model_dump(),
        readers_label=READERS_LABELS[row.readers],
        writers_label=WRITERS_LABELS[row.writers],
        readers_options=[ChoiceJson(value=k, label=v) for k, v in READERS_LABELS.items()],
        writers_options=[ChoiceJson(value=k, label=v) for k, v in WRITERS_LABELS.items()],
        can_manage=manage,
        budget_editable=manage and row.channel_id != WEB_ROW_ID,
        agents=agents,
    )


def _history_keys(value: object) -> list[str]:
    """The agent keys inside a change's before or after: a key, a list of keys, or a rule."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in cast("list[object]", value) if isinstance(item, str)]
    if isinstance(value, dict):
        agent = cast("dict[str, object]", value).get("agent")
        return [agent] if isinstance(agent, str) else []
    return []


def _budget_value(expected: BudgetExpected | None) -> BudgetValue | None:
    if expected is None:
        return None
    try:
        return BudgetValue(
            limit_usd=Decimal(expected.limit_usd),
            window=expected.window,
            starts_at=datetime.fromisoformat(expected.starts_at) if expected.starts_at else None,
            ends_at=datetime.fromisoformat(expected.ends_at) if expected.ends_at else None,
        )
    except (InvalidOperation, ValueError) as exc:
        raise HTTPException(422, "The budget you read is not valid.") from exc


async def detail_of(
    runtime: WebRuntime, caller: WebCaller, row: ChannelRow, *, manage: bool, figures: bool
) -> ChannelDetailJson:
    """`detail_json` with the agents its keys name."""
    refs = await agent_refs(runtime, caller, row_agent_keys(row))
    return detail_json(row, manage=manage, figures=figures, agents=refs)


async def platform_user(db: AsyncSession, caller: WebCaller, platform: str) -> str | None:
    principal = await caller.principal(db)
    return principal.external_id if principal.platform == platform else None


async def member_visible(
    runtime: WebRuntime, caller: WebCaller, rows: tuple[ChannelRow, ...]
) -> set[str]:
    """The rows a member may read: web chat, and channels the platform shows them."""
    tenant_id = caller.person.tenant_id
    async with runtime.sessionmaker() as db:
        tenant = await get_tenant(db, tenant_id)
        if tenant is None:
            return set()
        user_id = await platform_user(db, caller, tenant.platform)
    candidates = [r.channel_id for r in rows if r.channel_id != WEB_ROW_ID and not r.confidential]
    visible = await readable_channels(
        runtime,
        tenant_id=tenant_id,
        platform=tenant.platform,
        workspace_id=tenant.external_id,
        user_id=user_id,
        channel_ids=candidates,
    )
    return {WEB_ROW_ID, *visible}


async def one_row(runtime: WebRuntime, caller: WebCaller, channel_id: str) -> ChannelRow:
    """The channel's row as the caller may read it; 404 when they may not."""
    try:
        async with runtime.sessionmaker() as db:
            row = await load_channel_row(
                db,
                tenant_id=caller.person.tenant_id,
                channel_id=channel_id,
                default=runtime.deployment_default,
                now=datetime.now(UTC),
                markup=runtime.settings.billing.markup,
            )
    except AccessPolicyUnreadable as exc:
        raise HTTPException(503, _UNREADABLE) from exc
    if row is None:
        raise HTTPException(404, _NOT_FOUND)
    if not is_admin(caller.role) and channel_id not in await member_visible(
        runtime, caller, (row,)
    ):
        raise HTTPException(404, _NOT_FOUND)
    return row


def actor_of(caller: WebCaller, channel_id: str, platform: str, setting: str) -> ChannelActor:
    """The web admin acting, writing an audit row in the change's own transaction."""
    tenant_id = caller.person.tenant_id

    async def audited(db: AsyncSession) -> None:
        names = await get_channel_names(
            db, tenant_id=tenant_id, platform=platform, channel_ids=(channel_id,)
        )
        name = names.get(channel_id)
        await audit(
            db,
            tenant_id=tenant_id,
            actor_account_id=caller.person.account_id,
            action="Changed channel",
            target=f"#{name}"
            if name
            else "Workspace"
            if channel_id == WORKSPACE_ID
            else channel_id,
            detail=_AUDIT_DETAIL[setting],
            at=datetime.now(UTC),
        )

    return ChannelActor(
        tenant_id=tenant_id,
        account_id=caller.person.account_id,
        subject=Subject(is_admin=is_admin(caller.role)),
        surface="web",
        on_write=audited,
    )


def channels_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/channels")

    async def directory(caller: WebCaller) -> ChannelDirectory:
        try:
            async with runtime.sessionmaker() as db:
                return await load_channel_directory(
                    db,
                    tenant_id=caller.person.tenant_id,
                    default=runtime.deployment_default,
                    now=datetime.now(UTC),
                    markup=runtime.settings.billing.markup,
                )
        except AccessPolicyUnreadable as exc:
            raise HTTPException(503, _UNREADABLE) from exc

    async def readable(caller: WebCaller, channel_id: str) -> ChannelDetailJson:
        row = await one_row(runtime, caller, channel_id)
        return await detail_of(
            runtime,
            caller,
            row,
            manage="channels.manage" in caller.capabilities,
            figures=is_admin(caller.role),
        )

    async def agent_names(caller: WebCaller, name: str) -> tuple[str, ...]:
        """Every name the workspace agent called `name` answers to; 422 when none is."""
        for agent in await listed_agents(runtime, caller.person.tenant_id):
            names = tuple(n for n in agent_pin_names(agent.name, agent.metadata) if n)
            if name in names:
                return names
        raise HTTPException(422, "Pick an agent from this workspace.")

    def anthropic() -> AsyncAnthropic:
        if runtime.anthropic is None:
            raise HTTPException(503, "Agents are not available right now.")
        return runtime.anthropic

    async def changed(caller: WebCaller, channel_id: str, notes: list[str]) -> ChangeResultJson:
        return ChangeResultJson(channel=await readable(caller, channel_id), notes=notes)

    @router.get("")
    async def list_channels(caller: ChannelViewer) -> ChannelListJson:  # pyright: ignore[reportUnusedFunction]
        found = await directory(caller)
        rows = found.rows
        if not is_admin(caller.role):
            visible = await member_visible(runtime, caller, rows)
            rows = tuple(r for r in rows if r.channel_id in visible)
        refs = await agent_refs(
            runtime,
            caller,
            [found.default_agent.value, *(k for r in rows for k in row_agent_keys(r))],
        )
        return ChannelListJson(
            platform=found.platform,
            default_agent=_sourced(found.default_agent),
            default_environment=_sourced(found.default_environment),
            channels=[channel_json(r, figures=is_admin(caller.role)) for r in rows],
            agents=refs,
        )

    @router.get("/{channel_id}")
    async def get_channel(caller: ChannelViewer, channel_id: str) -> ChannelDetailJson:  # pyright: ignore[reportUnusedFunction]
        return await readable(caller, channel_id)

    @router.put("/{channel_id}/default-agent")
    async def put_default_agent(  # pyright: ignore[reportUnusedFunction]
        caller: ChannelAdmin, channel_id: str, body: DefaultAgentChange
    ) -> ChangeResultJson:
        if channel_id == WEB_ROW_ID:
            raise HTTPException(422, "Web chat uses the workspace default agent.")
        platform = (await one_row(runtime, caller, channel_id)).platform
        actor = actor_of(caller, channel_id, platform, "default_agent")
        with channel_refusals():
            if body.agent is None:
                await clear_default_agent(
                    runtime.sessionmaker,
                    actor,
                    platform=platform,
                    channel_id=channel_id,
                    expected=Expected(body.expected),
                )
            else:
                names = await agent_names(caller, body.agent)
                await set_default_agent(
                    runtime.sessionmaker,
                    actor,
                    platform=platform,
                    channel_id=channel_id,
                    agent_name=body.agent,
                    agent_names=names,
                    expected=Expected(body.expected),
                )
        return await changed(caller, channel_id, [])

    @router.post("/{channel_id}/kept-agents")
    async def post_kept_agent(  # pyright: ignore[reportUnusedFunction]
        caller: ChannelAdmin, channel_id: str, body: KeptAgentChange
    ) -> ChangeResultJson:
        if channel_id == WEB_ROW_ID:
            raise HTTPException(422, "Agents are kept to channels, not web chat.")
        client = anthropic()
        platform = (await one_row(runtime, caller, channel_id)).platform
        change = keep_agent if body.action == "add" else release_agent
        with channel_refusals():
            result = await change(
                client,
                runtime.sessionmaker,
                actor_of(caller, channel_id, platform, "kept_agents"),
                platform=platform,
                channel_id=channel_id,
                agent_name=body.agent,
                default=runtime.deployment_default,
                expected=Expected(tuple(body.expected)),
            )
        return await changed(caller, channel_id, list(result.notes))

    @router.put("/{channel_id}/access")
    async def put_access(  # pyright: ignore[reportUnusedFunction]
        caller: ChannelAdmin, channel_id: str, body: AccessChange
    ) -> ChangeResultJson:
        if channel_id == WEB_ROW_ID:
            raise HTTPException(422, "Web chat has no channel rule.")
        client = anthropic()
        row = await one_row(runtime, caller, channel_id)
        platform = row.platform
        public_url = runtime.settings.mcp.public_url
        with channel_refusals():
            result = await set_access(
                client,
                runtime.sessionmaker,
                actor_of(caller, channel_id, platform, "access"),
                platform=platform,
                channel_id=channel_id,
                readers=body.readers,
                writers=body.writers,
                default=runtime.deployment_default,
                expected=Expected(
                    ChannelRule(readers=body.expected.readers, writers=body.expected.writers)
                ),
                copy=body.copy_agent,
                copy_from=body.copy_from,
                channel_label=row.name.name if row.name is not None else None,
                public_url=str(public_url) if public_url is not None else None,
                release_agents=body.release_agents,
                keep_rules=body.keep_rules,
            )
        return await changed(caller, channel_id, list(result.notes))

    @router.put("/{channel_id}/budget")
    async def put_budget(  # pyright: ignore[reportUnusedFunction]
        caller: ChannelAdmin, channel_id: str, body: BudgetChange
    ) -> ChangeResultJson:
        if channel_id == WEB_ROW_ID:
            raise HTTPException(422, _NO_WEB_BUDGET)
        platform = (await one_row(runtime, caller, channel_id)).platform
        actor = actor_of(caller, channel_id, platform, "budget")
        expected = Expected(_budget_value(body.expected))
        with channel_refusals():
            if body.action == "clear":
                await clear_budget(
                    runtime.sessionmaker,
                    actor,
                    platform=platform,
                    channel_id=channel_id,
                    expected=expected,
                )
            elif body.action == "raise":
                try:
                    amount = Decimal(body.amount_usd or "")
                except InvalidOperation as exc:
                    raise HTTPException(422, "Enter an amount in dollars.") from exc
                await raise_budget(
                    runtime.sessionmaker,
                    actor,
                    platform=platform,
                    channel_id=channel_id,
                    amount_usd=amount,
                    expected=expected,
                )
            else:
                try:
                    spec = parse_budget_spec(
                        limit_usd=body.limit_usd or "",
                        window=body.window or "",
                        starts_at=body.starts_at,
                        ends_at=body.ends_at,
                    )
                except ChannelBudgetError as exc:
                    raise HTTPException(422, _BAD_BUDGET) from exc
                await set_budget(
                    runtime.sessionmaker,
                    actor,
                    platform=platform,
                    channel_id=channel_id,
                    spec=spec,
                    expected=expected,
                )
        return await changed(caller, channel_id, [])

    @router.get("/{channel_id}/history")
    async def history(caller: ChannelAdmin, channel_id: str) -> HistoryJson:  # pyright: ignore[reportUnusedFunction]
        tenant_id = caller.person.tenant_id
        async with runtime.sessionmaker() as db:
            changes = await list_changes(db, tenant_id=tenant_id, channel_id=channel_id)
            tool_rules = await list_channel_rule_history(
                db, tenant_id=tenant_id, channel_id=channel_id
            )
            names = await names_by_account(db, tenant_id)

        def actor(account_id: uuid.UUID | None) -> HistoryActorJson | None:
            if account_id is None:
                return None
            return HistoryActorJson(id=str(account_id), name=names.get(account_id))

        def account(set_by: str) -> uuid.UUID | None:
            try:
                return uuid.UUID(set_by)
            except ValueError:
                return None

        entries = [
            HistoryEntryJson(
                at=c.at.isoformat(),
                setting=c.setting,
                before=c.before,
                after=c.after,
                actor=actor(c.actor_account_id),
                surface=c.surface,
            )
            for c in changes
        ] + [
            HistoryEntryJson(
                at=r.set_at.isoformat(),
                setting="tool_rule",
                before={"agent": r.agent_name, "tool": r.tool_key, "rule": r.before_rule}
                if r.before_rule is not None
                else None,
                after={"agent": r.agent_name, "tool": r.tool_key, "rule": r.after_rule}
                if r.after_rule is not None
                else None,
                actor=actor(account(r.set_by)),
                surface=None,
            )
            for r in tool_rules
        ]
        entries.sort(key=lambda e: e.at, reverse=True)
        keys = [k for e in entries for v in (e.before, e.after) for k in _history_keys(v)]
        return HistoryJson(entries=entries, agents=await agent_refs(runtime, caller, keys))

    return router


@contextmanager
def channel_refusals() -> Iterator[None]:
    """Map a core refusal to the HTTP answer the web gives it."""
    try:
        yield
    except StaleChannelValue as exc:
        raise HTTPException(409, str(exc)) from exc
    except ChannelChangeRefused as exc:
        status = (
            428
            if exc.reason == "needs_confirm"
            else {"forbidden": 403, "invalid": 422, "not_found": 404}[exc.kind]
        )
        raise HTTPException(status, str(exc)) from exc
    except ChannelRuleRefused as exc:
        status = 403 if exc.reason == "admin_required" else 422
        raise HTTPException(status, str(exc)) from exc
    except AccessPolicyUnreadable as exc:
        raise HTTPException(503, _UNREADABLE) from exc
    except DaimonError as exc:
        raise HTTPException(422, str(exc)) from exc
