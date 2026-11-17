"""`/api/support`: Ask a person limits, menus and the request channel, for server admins.

Every write goes through `daimon.core.support_changes` under `SET_SUPPORT`, the rule the CLI
and MCP follow, sending the value last read: a value that moved since is a 409. Every answer
is the whole page, read fresh.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal

from daimon.adapters.web.access import is_admin
from daimon.adapters.web.admin_common import WorkspaceAdmin, listed_agents
from daimon.adapters.web.audit import audit
from daimon.adapters.web.caller import WebCaller
from daimon.adapters.web.channels import channel_refusals, one_row
from daimon.adapters.web.runtime import WebRuntime
from daimon.core.authz import Subject
from daimon.core.channel_changes import ChannelActor, ChannelChangeRefused, Expected
from daimon.core.channel_directory import WEB_ROW_ID, load_channel_directory
from daimon.core.stores import support as store
from daimon.core.stores import support_escalation as ledger
from daimon.core.stores import web_people
from daimon.core.stores.accounts import get_account_with_tenant
from daimon.core.stores.feature_values import list_feature_values
from daimon.core.stores.platform_names import (
    KnownName,
    get_channel_names,
    get_user_names,
    search_user_names,
)
from daimon.core.stores.support_rows import SupportEscalationRow, SupportLimitRow
from daimon.core.stores.teams_channel_subscriptions import get_teams_channel_subscription
from daimon.core.stores.tenants import get_tenant
from daimon.core.stores.workspace_members import list_chat_standings, list_synced
from daimon.core.support import (
    MenuSpec,
    SupportError,
    SupportScope,
    format_credits,
    limit_period,
    limit_state,
    parse_limit_spec,
    parse_menu_key,
    parse_person_id,
    parse_scope,
)
from daimon.core.support_admin import LimitUsage, list_limit_usage, options_text
from daimon.core.support_changes import (
    MENU_IN_USE,
    NO_MENU,
    OWN_CHANNEL_LOCKED,
    ChannelPick,
    clear_limit,
    remove_menu,
    set_limit,
    set_menu,
    set_menu_use,
    set_support_channel,
)
from daimon.core.support_routing import (
    SupportDestination,
    deployment_channel,
    own_channel_allowed,
    support_destination,
)
from daimon.core.windows import WindowState
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

_PLATFORMS = ("discord", "slack", "teams")
_UNAVAILABLE = "Ask a person needs a Discord, Slack or Teams workspace."
_NO_PLACE = "No such place."
_NO_CHANNEL = "No such channel."
_NO_PERSON = "No such person."
_LIMIT_ACTION = "Changed support limit"
_MENU_ACTION = "Changed support menu"
_CHANNEL_ACTION = "Changed support channel"
_RECENT = 5
_OPEN_SHOWN = 500
_UNDELIVERED_SHOWN = 50
_NEITHER = "Set a shared limit"
_PEOPLE_SHOWN = 8
_WEB_PEOPLE_READ = 50

Problem = dict[str, object]
Loc = list[str | int]


# ---------- Request shapes ----------


class LimitFields(BaseModel):
    shared: str | None = None
    each_person: str | None = None
    window: Literal["total", "monthly"] = "total"
    starts_at: str | None = None
    ends_at: str | None = None


class LimitBody(BaseModel):
    scope: SupportScope
    scope_id: str = ""
    person_id: str | None = None
    limit: LimitFields | None
    """None removes the row."""
    expected_revision: int | None
    """The revision last read; None when the row is new."""


class MenuBody(BaseModel):
    menu: dict[str, object] | None
    """None removes the menu."""
    expected_revision: int | None


class MenuUseBody(BaseModel):
    scope: SupportScope
    scope_id: str = ""
    menu: str | None
    """None drops the place's own pick."""
    expected: str | None
    """The key the place's own pick had when read."""


class ChannelBody(BaseModel):
    channel_id: str | None
    """None drops the workspace's own pick: the default applies."""
    expected_revision: int | None


# ---------- Response shapes ----------


class ChargeJson(BaseModel):
    at: str
    person: str
    credits: str
    what: str


class LimitJson(BaseModel):
    scope: SupportScope
    scope_id: str
    name: str | None
    person_id: str | None
    person_name: str | None
    shared: str | None
    each_person: str | None
    window: Literal["total", "monthly"]
    starts_at: str | None
    ends_at: str | None
    used: str
    state: WindowState
    revision: int
    recent: list[ChargeJson]


class PlaceRef(BaseModel):
    scope: SupportScope
    scope_id: str
    name: str


class MenuJson(BaseModel):
    key: str
    name: str
    revision: int
    summary: str
    options: int
    used_in: list[PlaceRef]
    body: dict[str, object]


class MenuUseJson(PlaceRef):
    menu: str | None


class PlaceJson(BaseModel):
    scope: Literal["agent", "channel"]
    id: str
    name: str


class AgentJson(BaseModel):
    id: str
    name: str


class NamedJson(BaseModel):
    id: str
    name: str


class DefaultJson(BaseModel):
    name: str


class OpenJson(BaseModel):
    count: int
    where: str | None


class RequestChannelJson(BaseModel):
    own_allowed: bool
    value: NamedJson | None
    default: DefaultJson | None
    state: Literal["ready", "blocked", "none"]
    detail: str | None
    open: OpenJson
    revision: int | None


class EveryoneJson(BaseModel):
    each_person: str


class PersonHitJson(BaseModel):
    id: str
    """The person's id on the workspace's platform: what a limit row takes as `person_id`."""
    name: str
    handle: str | None


class SupportPageJson(BaseModel):
    platform: str
    everyone: EveryoneJson
    limits: list[LimitJson]
    menus: list[MenuJson]
    menu_use: list[MenuUseJson]
    places: list[PlaceJson]
    agents: list[AgentJson]
    channel: RequestChannelJson
    undelivered: list[ChargeJson]
    """Requests no support channel received, newest first."""


# ---------- Errors ----------


def problem(loc: Loc, msg: str) -> HTTPException:
    """A 422 in the shape the UI reads: `loc` starts at the body key."""
    detail: list[Problem] = [{"loc": loc, "msg": msg}]
    return HTTPException(422, detail)


@contextmanager
def located(*prefix: str | int, fallback: Loc | None = None) -> Iterator[None]:
    """A `SupportError` as a 422 on the input it names, under `prefix`."""
    try:
        yield
    except SupportError as exc:
        if exc.field is None:
            loc: Loc = [*prefix] if fallback is None else fallback
        elif str(exc).startswith(_NEITHER):
            loc = [*prefix]
        else:
            loc = [*prefix, exc.field]
        raise problem(loc, str(exc)) from exc


@contextmanager
def support_refusals() -> Iterator[None]:
    """The refusals of `channel_refusals`, with the menu texts the contract gives."""
    with channel_refusals():
        try:
            yield
        except ChannelChangeRefused as exc:
            text = str(exc)
            if text.startswith(NO_MENU):
                raise HTTPException(404, NO_MENU) from exc
            if text == MENU_IN_USE:
                raise problem(["menu"], text) from exc
            raise


def body_of[M: BaseModel](model: type[M]) -> Callable[[Request], Awaitable[M]]:
    """The JSON body as `model`; a problem has `loc` starting at the body key, no `body`."""

    async def read(request: Request) -> M:
        try:
            return model.model_validate(await request.json())
        except ValidationError as exc:
            raise HTTPException(
                422,
                [
                    {"loc": list(e["loc"]), "msg": e["msg"].removeprefix("Value error, ")}
                    for e in exc.errors()
                ],
            ) from exc
        except ValueError as exc:
            raise problem([], "Send a JSON body.") from exc

    return read


# ---------- Small helpers ----------


def instant(value: datetime | None) -> str | None:
    """`2026-12-18T00:00:00Z`; sub-second parts kept when there are any."""
    if value is None:
        return None
    utc = value.astimezone(UTC)
    text = utc.isoformat(timespec="seconds" if utc.microsecond == 0 else "microseconds")
    return text.removesuffix("+00:00") + "Z"


def credits_or_none(value: Decimal | None) -> str | None:
    return None if value is None else format_credits(value)


def request_channel_state(
    destination: SupportDestination | None, *, own_allowed: bool
) -> tuple[Literal["ready", "blocked", "none"], str | None]:
    """Can Daimon post to where requests go, and what to do if not.

    The per-platform "can post here" check comes later as a core function; until then a
    destination means `ready` and none means `none`. The one place to swap it.
    """
    if destination is not None:
        return "ready", None
    if own_allowed:
        return "none", "Pick a channel for requests."
    return "none", "Ask the operator to set a request channel."


def _actor(caller: WebCaller, *, action: str, target: str, detail: str) -> ChannelActor:
    """The web admin acting, writing an audit row in the change's own transaction."""
    tenant_id = caller.person.tenant_id

    async def audited(db: AsyncSession) -> None:
        await audit(
            db,
            tenant_id=tenant_id,
            actor_account_id=caller.person.account_id,
            action=action,
            target=target,
            detail=detail,
            at=datetime.now(UTC),
        )

    return ChannelActor(
        tenant_id=tenant_id,
        account_id=caller.person.account_id,
        subject=Subject(is_admin=is_admin(caller.role)),
        surface="web",
        on_write=audited,
    )


def support_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/support")

    async def platform_of(caller: WebCaller) -> str:
        async with runtime.sessionmaker() as db:
            tenant = await get_tenant(db, caller.person.tenant_id)
        if tenant is None or tenant.platform not in _PLATFORMS:
            raise HTTPException(404, _UNAVAILABLE)
        return tenant.platform

    async def channel_names(caller: WebCaller, platform: str, ids: set[str]) -> dict[str, str]:
        async with runtime.sessionmaker() as db:
            return dict(
                await get_channel_names(
                    db, tenant_id=caller.person.tenant_id, platform=platform, channel_ids=ids
                )
            )

    async def place_exists(
        caller: WebCaller, scope: SupportScope, scope_id: str, missing: str = _NO_PLACE
    ) -> None:
        """404 for an agent or a channel this workspace does not have."""
        if scope == "agent":
            known = {a.id for a in await listed_agents(runtime, caller.person.tenant_id)}
        elif scope == "channel":
            async with runtime.sessionmaker() as db:
                directory = await load_channel_directory(
                    db,
                    tenant_id=caller.person.tenant_id,
                    default=runtime.deployment_default,
                    now=runtime.now(),
                    markup=runtime.settings.billing.markup,
                )
            known = {r.channel_id for r in directory.rows if r.kind == "channel"}
        else:
            return
        if scope_id not in known:
            raise HTTPException(404, missing)

    async def place_label(
        caller: WebCaller, platform: str, scope: SupportScope, scope_id: str
    ) -> str:
        """The audit target: the channel or agent by name, never a person."""
        if scope == "workspace":
            return "Workspace"
        if scope == "agent":
            agents = await listed_agents(runtime, caller.person.tenant_id)
            return next((a.name for a in agents if a.id == scope_id), scope_id)
        name = (await channel_names(caller, platform, {scope_id})).get(scope_id)
        return f"#{name}" if name else scope_id

    async def person_user(db: AsyncSession, caller: WebCaller, platform: str, raw: str) -> str:
        """The platform user a `person_id` names: a web person's account, else a raw id."""
        tenant_id = caller.person.tenant_id
        try:
            person_id = uuid.UUID(raw)
        except ValueError:
            person_id = None
        if person_id is not None:
            person = await web_people.get_person(db, tenant_id=tenant_id, person_id=person_id)
            if person is not None:
                identity = await get_account_with_tenant(db, account_id=person.account_id)
                if identity is None or identity.platform_user_id is None:
                    raise problem(["person_id"], _NO_PERSON)
                return identity.platform_user_id
        with located("person_id", fallback=["person_id"]):
            try:
                return parse_person_id(platform, raw)
            except SupportError as exc:
                raise SupportError(str(exc)) from exc

    async def page(caller: WebCaller) -> SupportPageJson:
        tenant_id = caller.person.tenant_id
        platform = await platform_of(caller)
        now = runtime.now()
        agents = {a.id: a.name for a in await listed_agents(runtime, tenant_id)}
        async with runtime.sessionmaker() as db:
            usage = await list_limit_usage(db, tenant_id=tenant_id, now=now)
            person_rows = await store.list_limits(db, tenant_id=tenant_id, people=True)
            menus = await store.list_menus(db, tenant_id=tenant_id)
            uses = await store.list_menu_uses(db, tenant_id=tenant_id)
            directory = await load_channel_directory(
                db,
                tenant_id=tenant_id,
                default=runtime.deployment_default,
                now=now,
                markup=runtime.settings.billing.markup,
            )
            overrides = await list_feature_values(
                db, tenant_id=tenant_id, keys=["support_escalation"]
            )
            own = await store.get_support_channel(db, tenant_id=tenant_id)
            destination = await support_destination(
                db, tenant_id=tenant_id, platform=platform, settings=runtime.settings
            )
            open_now = await ledger.list_requests(
                db, tenant_id=tenant_id, status="pending", limit=_OPEN_SHOWN
            )
            lost = await ledger.list_requests(
                db, tenant_id=tenant_id, undelivered=True, limit=_UNDELIVERED_SHOWN
            )
            by_place = {(u.limit.scope, u.limit.scope_id): u for u in usage}

            recent: dict[uuid.UUID, list[SupportEscalationRow]] = {}
            for u in usage:
                recent[u.limit.id] = await ledger.list_requests(
                    db, tenant_id=tenant_id, limit_id=u.limit.id, limit=50
                )

            user_ids = {r.platform_user_id for rows in recent.values() for r in rows}
            user_ids.update(r.platform_user_id for r in lost)
            user_ids.update(p.platform_user_id for p in person_rows if p.platform_user_id)
            known = await get_user_names(
                db, tenant_id=tenant_id, platform=platform, user_ids=user_ids
            )
            people: dict[str, tuple[str, str | None]] = {}
            for uid in user_ids:
                person = await web_people.get_person_for_platform_user(
                    db, tenant_id=tenant_id, platform=platform, platform_user_id=uid
                )
                label = known[uid].label if uid in known else None
                people[uid] = (
                    (str(person.id), person.display_name or label)
                    if person is not None
                    else (uid, label)
                )

            person_used: dict[tuple[uuid.UUID, str], Decimal] = {}
            for p in person_rows:
                place = by_place.get((p.scope, p.scope_id))
                if place is not None and p.platform_user_id is not None:
                    _, person_used[(p.id, p.platform_user_id)] = await ledger.limit_spent(
                        db,
                        limit=place.limit,
                        platform_user_id=p.platform_user_id,
                        as_of=now,
                        person_period=limit_period(p, as_of=now),
                    )

        channels = {
            r.channel_id: (r.name.name if r.name is not None else r.channel_id)
            for r in directory.rows
            if r.kind == "channel" and r.channel_id != WEB_ROW_ID
        }
        extra = {
            p.scope_id for p in [u.limit for u in usage] + person_rows if p.scope == "channel"
        } - channels.keys()
        if extra:
            channels |= await channel_names(caller, platform, extra)
        extra_destination = {
            c for c in (own.channel_id if own else None, _default_channel(platform)) if c
        }
        names = channels | {
            k: v
            for k, v in (
                await channel_names(caller, platform, extra_destination - channels.keys())
            ).items()
        }

        def place_name(scope: SupportScope, scope_id: str) -> str | None:
            if scope == "workspace":
                return None
            if scope == "agent":
                return agents.get(scope_id)
            return names.get(scope_id, scope_id)

        def place_text(scope: SupportScope, scope_id: str) -> str:
            return place_name(scope, scope_id) or "Workspace"

        def charge(row: SupportEscalationRow) -> ChargeJson:
            _, name = people.get(row.platform_user_id, (row.platform_user_id, None))
            return ChargeJson(
                at=instant(row.created_at) or "",
                person=name or row.platform_user_id,
                credits=format_credits(row.counted),
                what=row.option.label if row.option else "Request",
            )

        limits: list[LimitJson] = []
        for u in usage:
            limits.append(_place_limit(u, name=place_name(u.limit.scope, u.limit.scope_id)))
            limits[-1].recent = [charge(r) for r in recent[u.limit.id][:_RECENT]]
        for p in person_rows:
            assert p.platform_user_id is not None
            person_id, person_name = people.get(p.platform_user_id, (p.platform_user_id, None))
            place = by_place.get((p.scope, p.scope_id))
            mine = (
                [r for r in recent[place.limit.id] if r.platform_user_id == p.platform_user_id]
                if place is not None
                else []
            )
            limits.append(
                _limit_json(
                    p,
                    name=place_name(p.scope, p.scope_id),
                    used=person_used.get((p.id, p.platform_user_id), Decimal(0)),
                    person=(person_id, person_name),
                    now=now,
                    recent=[charge(r) for r in mine[:_RECENT]],
                )
            )

        key_of = {m.id: m.key for m in menus}
        use_key = {(u.scope, u.scope_id): key_of.get(u.menu_id) for u in uses}
        places = [PlaceRef(scope="workspace", scope_id="", name="Workspace")]
        places += [PlaceRef(scope="agent", scope_id=i, name=n) for i, n in agents.items()]
        places += [PlaceRef(scope="channel", scope_id=i, name=n) for i, n in channels.items()]

        own_allowed = own_channel_allowed(runtime.settings)
        mine_channel = own if own is not None and own.platform == platform else None
        default_id = _default_channel(platform)
        state, detail = request_channel_state(destination, own_allowed=own_allowed)
        placed = next((r.support_channel_id for r in open_now if r.support_channel_id), None)
        return SupportPageJson(
            platform=platform,
            everyone=EveryoneJson(
                each_person=format_credits(Decimal(runtime.settings.support.credits_per_user))
            ),
            limits=limits,
            menus=[
                MenuJson(
                    key=m.key,
                    name=m.spec.name,
                    revision=m.revision,
                    summary=options_text(m.spec),
                    options=len(m.spec.options),
                    used_in=[
                        PlaceRef(
                            scope=u.scope, scope_id=u.scope_id, name=place_text(u.scope, u.scope_id)
                        )
                        for u in uses
                        if u.menu_id == m.id
                    ],
                    body=m.spec.model_dump(mode="json", exclude_none=True),
                )
                for m in menus
            ],
            menu_use=[
                MenuUseJson(
                    scope=p.scope,
                    scope_id=p.scope_id,
                    name=p.name,
                    menu=use_key.get((p.scope, p.scope_id)),
                )
                for p in places
            ],
            places=[
                PlaceJson(
                    scope=o.tier, id=o.agent_id if o.tier == "agent" else o.channel_id, name=n
                )
                for o in overrides
                if o.tier in ("agent", "channel")
                and (n := place_name(o.tier, o.agent_id if o.tier == "agent" else o.channel_id))
            ],
            agents=[AgentJson(id=i, name=n) for i, n in agents.items()],
            channel=RequestChannelJson(
                own_allowed=own_allowed,
                value=(
                    NamedJson(
                        id=mine_channel.channel_id,
                        name=names.get(mine_channel.channel_id, mine_channel.channel_id),
                    )
                    if own_allowed and mine_channel is not None
                    else None
                ),
                default=(
                    None
                    if default_id is None
                    else DefaultJson(name=names.get(default_id, default_id))
                ),
                state=state,
                detail=detail,
                open=OpenJson(
                    count=len(open_now), where=names.get(placed, placed) if placed else None
                ),
                revision=mine_channel.revision if own_allowed and mine_channel else None,
            ),
            undelivered=[charge(r) for r in lost],
        )

    def _default_channel(platform: str) -> str | None:
        return deployment_channel(runtime.settings.support, platform)

    def _limit_json(
        row: SupportLimitRow,
        *,
        name: str | None,
        used: Decimal,
        person: tuple[str, str | None] | None,
        now: datetime,
        recent: list[ChargeJson],
    ) -> LimitJson:
        return LimitJson(
            scope=row.scope,
            scope_id=row.scope_id,
            name=name,
            person_id=None if person is None else person[0],
            person_name=None if person is None else person[1],
            shared=credits_or_none(row.shared),
            each_person=credits_or_none(row.each_person),
            window=row.window,
            starts_at=instant(row.starts_at),
            ends_at=instant(row.ends_at),
            used=format_credits(used),
            state=limit_state(row, now=now),
            revision=row.revision,
            recent=recent,
        )

    def _place_limit(u: LimitUsage, *, name: str | None) -> LimitJson:
        return _limit_json(
            u.limit, name=name, used=u.shared_used, person=None, now=runtime.now(), recent=[]
        )

    @router.get("")
    async def get_page(caller: WorkspaceAdmin) -> SupportPageJson:  # pyright: ignore[reportUnusedFunction]
        return await page(caller)

    @router.get("/people")
    async def find_people(  # pyright: ignore[reportUnusedFunction]
        caller: WorkspaceAdmin, q: Annotated[str, Query(min_length=1, max_length=100)]
    ) -> list[PersonHitJson]:
        """People on the workspace's platform whose name or handle contains `q`: web people
        with a chat account and everyone the chat has seen, each once by platform id.

        Where the member list was loaded in full only members are offered; where it was not,
        nobody named is hidden.
        """
        tenant_id = caller.person.tenant_id
        platform = await platform_of(caller)
        needle = q.strip()
        if not needle:
            return []
        async with runtime.sessionmaker() as db:
            synced = tenant_id in await list_synced(db, tenant_ids=[tenant_id])
            web = await web_people.list_people(
                db, tenant_id=tenant_id, limit=_WEB_PEOPLE_READ, search=needle
            )
            standings = await list_chat_standings(
                db, people=[(tenant_id, w.account_id) for w in web]
            )
            hits: dict[str, str] = {}
            for w in web:
                standing = standings.get((tenant_id, w.account_id))
                if (
                    standing is not None
                    and standing.platform == platform
                    and (standing.member or not synced)
                ):
                    hits[standing.platform_user_id] = w.display_name
            named = await search_user_names(
                db,
                tenant_id=tenant_id,
                platform=platform,
                query=needle,
                members_only=synced,
                limit=_PEOPLE_SHOWN,
            )
            # Handles of web people come from the same table; the ones just read keep theirs.
            known: dict[str, KnownName] = named | await get_user_names(
                db, tenant_id=tenant_id, platform=platform, user_ids=hits.keys() - named.keys()
            )
        found = {
            uid: (hits.get(uid) or (known[uid].label if uid in known else None) or uid)
            for uid in hits.keys() | named.keys()
        }
        ordered = sorted(found, key=lambda uid: (found[uid].casefold(), uid))[:_PEOPLE_SHOWN]
        return [
            PersonHitJson(
                id=uid, name=found[uid], handle=known[uid].handle if uid in known else None
            )
            for uid in ordered
        ]

    @router.put("/limits")
    async def put_limit(  # pyright: ignore[reportUnusedFunction]
        caller: WorkspaceAdmin, body: Annotated[LimitBody, Depends(body_of(LimitBody))]
    ) -> SupportPageJson:
        platform = await platform_of(caller)
        expected = Expected(body.expected_revision)
        with support_refusals():
            with located("scope", fallback=["scope"]):
                scope, scope_id = parse_scope(body.scope, body.scope_id.strip() or None)
            if body.limit is not None:
                await place_exists(caller, scope, scope_id)
            async with runtime.sessionmaker() as db:
                user = (
                    None
                    if body.person_id is None
                    else await person_user(db, caller, platform, body.person_id)
                )
            actor = _actor(
                caller,
                action=_LIMIT_ACTION,
                target=await place_label(caller, platform, scope, scope_id),
                detail="Limit",
            )
            if body.limit is None:
                await clear_limit(
                    runtime.sessionmaker,
                    actor,
                    platform=platform,
                    scope=scope,
                    scope_id=scope_id,
                    platform_user_id=user,
                    expected=expected,
                )
            else:
                fields = body.limit
                with located("limit"):
                    spec = parse_limit_spec(
                        scope=scope,
                        scope_id=scope_id or None,
                        platform_user_id=user,
                        shared=fields.shared,
                        each_person=fields.each_person,
                        window=fields.window,
                        starts_at=fields.starts_at,
                        ends_at=fields.ends_at,
                    )
                await set_limit(
                    runtime.sessionmaker, actor, platform=platform, spec=spec, expected=expected
                )
        return await page(caller)

    @router.put("/menus/use")
    async def put_menu_use(  # pyright: ignore[reportUnusedFunction]
        caller: WorkspaceAdmin, body: Annotated[MenuUseBody, Depends(body_of(MenuUseBody))]
    ) -> SupportPageJson:
        platform = await platform_of(caller)
        with support_refusals():
            with located("scope", fallback=["scope"]):
                scope, scope_id = parse_scope(body.scope, body.scope_id.strip() or None)
            if body.menu is not None:
                await place_exists(caller, scope, scope_id)
            actor = _actor(
                caller,
                action=_MENU_ACTION,
                target=await place_label(caller, platform, scope, scope_id),
                detail="Menu choice",
            )
            await set_menu_use(
                runtime.sessionmaker,
                actor,
                platform=platform,
                scope=scope,
                scope_id=scope_id,
                key=body.menu,
                expected=Expected(body.expected),
            )
        return await page(caller)

    @router.put("/menus/{key}")
    async def put_menu(  # pyright: ignore[reportUnusedFunction]
        caller: WorkspaceAdmin,
        key: str,
        body: Annotated[MenuBody, Depends(body_of(MenuBody))],
    ) -> SupportPageJson:
        platform = await platform_of(caller)
        expected = Expected(body.expected_revision)
        with support_refusals():
            with located("path", "key"):
                key = parse_menu_key(key)
            actor = _actor(caller, action=_MENU_ACTION, target=key, detail="Menu")
            if body.menu is None:
                await remove_menu(
                    runtime.sessionmaker, actor, platform=platform, key=key, expected=expected
                )
            else:
                try:
                    spec = MenuSpec.model_validate(body.menu)
                except ValidationError as exc:
                    raise HTTPException(
                        422,
                        [
                            {
                                "loc": ["menu", *e["loc"]],
                                "msg": e["msg"].removeprefix("Value error, "),
                            }
                            for e in exc.errors()
                        ],
                    ) from exc
                await set_menu(
                    runtime.sessionmaker,
                    actor,
                    platform=platform,
                    key=key,
                    spec=spec,
                    expected=expected,
                )
        return await page(caller)

    @router.put("/channel")
    async def put_channel(  # pyright: ignore[reportUnusedFunction]
        caller: WorkspaceAdmin, body: Annotated[ChannelBody, Depends(body_of(ChannelBody))]
    ) -> SupportPageJson:
        platform = await platform_of(caller)
        own_allowed = own_channel_allowed(runtime.settings)
        if not own_allowed:
            raise HTTPException(403, OWN_CHANNEL_LOCKED)
        pick: ChannelPick | None = None
        label = "Default"
        with support_refusals():
            if body.channel_id is not None:
                await place_exists(caller, "channel", body.channel_id, _NO_CHANNEL)
                row = await one_row(runtime, caller, body.channel_id)
                if row.kind != "channel" or row.channel_id == WEB_ROW_ID:
                    raise HTTPException(404, _NO_CHANNEL)
                async with runtime.sessionmaker() as db:
                    tenant = await get_tenant(db, caller.person.tenant_id)
                    team = next((n.id for n in row.path if n.kind == "team"), None)
                    sub = (
                        await get_teams_channel_subscription(
                            db, tenant_id=caller.person.tenant_id, channel_id=row.channel_id
                        )
                        if platform == "teams"
                        else None
                    )
                pick = ChannelPick(
                    platform=platform,
                    channel_id=row.channel_id,
                    slack_team_id=tenant.external_id if tenant and platform == "slack" else None,
                    teams_team_id=team if platform == "teams" else None,
                    teams_service_url=sub.service_url if sub else None,
                )
                label = f"#{row.name.name}" if row.name else row.channel_id
            actor = _actor(caller, action=_CHANNEL_ACTION, target=label, detail="Request channel")
            await set_support_channel(
                runtime.sessionmaker,
                actor,
                pick=pick,
                own_allowed=own_allowed,
                expected=Expected(body.expected_revision),
            )
        return await page(caller)

    return router
