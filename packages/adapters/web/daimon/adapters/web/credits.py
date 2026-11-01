"""`/api/credits/*`: a person's own meter, and the workspace's balance, spend and allowances.

Every figure is billed dollars (model cost times the workspace markup), the basis the
ledger and allowances count. Everything but `mine` is for admins (`credits.manage`).
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from anthropic import APIStatusError
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.web.access import Role
from daimon.adapters.web.audit import (
    INVITE_LABEL,
    PERSON_LABEL,
    AuditTarget,
    audit,
    invite_target,
    person_target,
    usd,
)
from daimon.adapters.web.caller import WebCaller, current_caller, groups_of, requires
from daimon.adapters.web.channel_names import named_spend_channels
from daimon.adapters.web.chat_json import AgentRef, agent_ref
from daimon.adapters.web.faces import avatars_of
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import credit_views
from daimon.core.allowances import window_start
from daimon.core.billing import (
    CheckoutAmountUnavailable,
    create_checkout_session,
    manual_credit,
    month_start,
)
from daimon.core.ma_resolver import retrieve_agent_cached
from daimon.core.spend_gate import (
    SpendRequest,
    person_verdict,
    resolve_spend_person,
    spend_person_of_account,
)
from daimon.core.stores import spend_allowances, tenant_ledger, web_invites
from daimon.core.stores import web_identities as identities_store
from daimon.core.stores import web_people as people_store
from daimon.core.stores.domain import SpendAllowanceRow, WebPersonRow
from daimon.core.web_identity import linked_identity, principal_of, with_spend_person
from fastapi import APIRouter, Depends, HTTPException
from mux.errors import ScopeViolation
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

#: Amounts, in whole dollars, a card checkout may buy.
CHECKOUT_AMOUNTS = frozenset({10, 25, 50, 100})
_LEDGER_LIMIT = 50
_RECENT_TURNS = 12
_CENT = Decimal("0.01")
_NO_AGENT = "other"


def _omit_none(value: object) -> bool:
    return value is None


def _usd(value: Decimal) -> float:
    return float(value.quantize(_CENT))


class CreditMeter(BaseModel):
    left_usd: float
    total_usd: float
    window: Literal["month", "total"]
    resets_at: str | None


class SpendRow(BaseModel):
    id: str
    name: str
    avatar: str | None = Field(default=None, exclude_if=_omit_none)
    kind: str | None = Field(default=None, exclude_if=_omit_none)
    turns: int
    usd: float


class Turn(BaseModel):
    id: str
    at: str
    agent: AgentRef
    title: str
    platform: str
    usd: float


class Mine(BaseModel):
    by_agent: list[SpendRow]
    turns: list[Turn]
    spent_usd: float
    turn_count: int


class LedgerEntry(BaseModel):
    id: str
    at: str
    kind: Literal["topup", "promo", "adjustment"]
    amount_usd: float
    by: str
    note: str


class DailySpend(BaseModel):
    day: str
    usd: float


class Overview(BaseModel):
    balance_usd: float
    spent_month_usd: float
    projected_month_usd: float
    turns_month: int
    daily: list[DailySpend]
    ledger: list[LedgerEntry]
    topup: Literal["checkout", "manual"] | None


class Subject(BaseModel):
    kind: Literal["person", "group", "invite"]
    id: str = Field(min_length=1, max_length=200)
    name: str = ""


class Allowance(BaseModel):
    id: str
    subject: Subject
    agent: AgentRef | None
    amount_usd: float
    used_usd: float
    members: int | None = Field(default=None, exclude_if=_omit_none)
    window: Literal["month", "total"]
    expires_at: str | None


class AgentChoice(BaseModel):
    id: str = Field(min_length=1, max_length=200)


class NewAllowance(BaseModel):
    subject: Subject
    agent: AgentChoice | None = None
    amount_usd: Decimal = Field(gt=0, le=1_000_000, decimal_places=2)
    window: Literal["month", "total"]
    expires_at: datetime | None = None


class TopUp(BaseModel):
    amount_usd: Decimal = Field(gt=0, le=100_000, decimal_places=2)
    request_id: str | None = Field(default=None, max_length=100)


Member = Annotated[WebCaller, Depends(requires("credits.view_own"))]
Admin = Annotated[WebCaller, Depends(requires("credits.manage"))]


async def credit_meter(runtime: WebRuntime, caller: WebCaller) -> CreditMeter | None:
    """Where the caller stands against the allowance that governs them."""
    return await person_meter(runtime, caller.person, caller.role, groups_of(caller))


async def person_meter(
    runtime: WebRuntime, person: WebPersonRow, role: Role, groups: frozenset[str]
) -> CreditMeter | None:
    """Where a person stands against the allowance that governs them.

    Counted under the id and groups their turns run as, so the meter and the gate agree.
    None when nothing limits them and they are not a visitor.
    """
    async with runtime.sessionmaker() as db:
        principal = await with_spend_person(
            db, person, await principal_of(db, person, role, groups=groups)
        )
        verdict = await person_verdict(
            db,
            request=SpendRequest(
                tenant_id=person.tenant_id,
                platform_user_id=principal.external_id,
                platform="web",
                channel_id=None,
                agent_id=None,
                person=principal.spend_person,
            ),
            markup=runtime.settings.billing.markup,
            now=runtime.now(),
        )
    if verdict.left_usd is None or verdict.total_usd is None:
        return None
    return CreditMeter(
        left_usd=float(verdict.left_usd),
        total_usd=float(verdict.total_usd),
        window=verdict.window or "total",
        resets_at=verdict.resets_at.isoformat() if verdict.resets_at else None,
    )


def _ledger_wording(line: credit_views.LedgerLine, actor: str | None) -> tuple[str, str]:
    """Who and what a ledger row reads as."""
    if line.actor_account_id is not None:
        return actor or "An admin", "Manual grant"
    named = {
        "topup": ("Stripe", "Card payment"),
        "promo_credit": ("Daimon", "Promo code"),
        "trial": ("Daimon", "Welcome credit"),
        "manual_credit": ("Support", "Manual credit"),
    }
    return named.get(line.reason, ("Daimon", line.reason.replace("_", " ").capitalize()))


def credits_router(runtime: WebRuntime) -> APIRouter:
    router = APIRouter(prefix="/api/credits")
    markup = runtime.settings.billing.markup

    def topup_mode() -> Literal["checkout", "manual"] | None:
        if runtime.stripe is not None and runtime.billing_config is not None:
            return "checkout"
        return "manual" if runtime.web.manual_topup else None

    async def agents_by_id(tenant_id: uuid.UUID, agent_ids: Collection[str]) -> dict[str, AgentRef]:
        """Each agent as the app shows it. An agent that is gone keeps its id only."""
        turns = runtime.turns

        async def one(agent_id: str) -> BetaManagedAgentsAgent | None:
            if turns is None:
                return None
            try:
                return await retrieve_agent_cached(
                    turns.deps.anthropic, turns.deps.resolver_cache, tenant_id, agent_id
                )
            except (APIStatusError, ScopeViolation):
                return None  # deleted, or not this workspace's: shown as removed

        ids = sorted(agent_ids)
        found = await asyncio.gather(*(one(i) for i in ids))
        avatars = await avatars_of(runtime, tenant_id, [a for a in found if a is not None])
        return {
            i: agent_ref(agent, i, avatars.get(i, "")) for i, agent in zip(ids, found, strict=True)
        }

    def spend_rows(rows: list[SpendRow]) -> list[SpendRow]:
        """One row per id, summed, biggest first."""
        merged: dict[str, SpendRow] = {}
        for row in rows:
            seen = merged.get(row.id)
            merged[row.id] = (
                row
                if seen is None
                else seen.model_copy(
                    update={"turns": seen.turns + row.turns, "usd": seen.usd + row.usd}
                )
            )
        return sorted(merged.values(), key=lambda r: (-r.usd, r.name))

    async def agent_rows(
        tenant_id: uuid.UUID, spend: list[credit_views.AgentSpend]
    ) -> list[SpendRow]:
        refs = await agents_by_id(tenant_id, {s.agent_id for s in spend if s.agent_id})
        return spend_rows(
            [
                SpendRow(
                    id=s.agent_id or _NO_AGENT,
                    name=refs[s.agent_id].name if s.agent_id else "Other",
                    avatar=refs[s.agent_id].avatar if s.agent_id else "",
                    turns=s.turns,
                    usd=_usd(s.usd),
                )
                for s in spend
            ]
        )

    @router.get("/meter")
    async def meter(  # pyright: ignore[reportUnusedFunction]
        caller: Annotated[WebCaller, Depends(current_caller)],
    ) -> CreditMeter | None:
        """Only where the caller stands against their allowance: what `/api/me` carries as
        `credit`, for a client that wants it fresh without the rest."""
        return await credit_meter(runtime, caller)

    @router.get("/mine")
    async def mine(caller: Member) -> Mine:  # pyright: ignore[reportUnusedFunction]
        now, tenant_id = runtime.now(), caller.person.tenant_id
        async with runtime.sessionmaker() as db:
            _, user_id = await linked_identity(db, caller.person)
            spend = await credit_views.spend_by_agent_for_user(
                db,
                tenant_id=tenant_id,
                platform_user_id=user_id,
                markup=markup,
                since=month_start(now),
            )
            charges = await credit_views.recent_turns(
                db,
                tenant_id=tenant_id,
                account_id=caller.person.account_id,
                markup=markup,
                since=now - timedelta(days=30),
                limit=_RECENT_TURNS,
            )
        by_agent = await agent_rows(tenant_id, spend)
        refs = await agents_by_id(tenant_id, {c.agent_id for c in charges if c.agent_id})
        return Mine(
            by_agent=by_agent,
            turns=[
                Turn(
                    id=str(c.id),
                    at=c.at.isoformat(),
                    agent=refs[c.agent_id] if c.agent_id else agent_ref(None, _NO_AGENT),
                    title=c.title or "Conversation",
                    platform=c.platform,
                    usd=_usd(c.usd),
                )
                for c in charges
            ],
            spent_usd=_usd(sum((s.usd for s in spend), Decimal(0))),
            turn_count=sum(r.turns for r in by_agent),
        )

    @router.get("/overview")
    async def overview(caller: Admin) -> Overview:  # pyright: ignore[reportUnusedFunction]
        now, tenant_id = runtime.now(), caller.person.tenant_id
        async with runtime.sessionmaker() as db:
            month = await credit_views.month_overview(
                db, tenant_id=tenant_id, now=now, markup=markup
            )
            lines = await credit_views.ledger_history(db, tenant_id=tenant_id, limit=_LEDGER_LIMIT)
            actors = await credit_views.web_people_by_account(
                db,
                tenant_id=tenant_id,
                account_ids={x.actor_account_id for x in lines if x.actor_account_id},
            )
        ledger: list[LedgerEntry] = []
        for line in lines:
            actor = actors.get(line.actor_account_id) if line.actor_account_id else None
            by, note = _ledger_wording(line, actor.display_name if actor else None)
            ledger.append(
                LedgerEntry(
                    id=str(line.id),
                    at=line.at.isoformat(),
                    kind=line.kind,
                    amount_usd=_usd(line.usd),
                    by=by,
                    note=note,
                )
            )
        return Overview(
            balance_usd=_usd(month.balance_usd),
            spent_month_usd=_usd(month.spent_usd),
            projected_month_usd=_usd(credit_views.projected_month(month.spent_usd, now=now)),
            turns_month=month.turns,
            daily=[DailySpend(day=d.day.isoformat(), usd=_usd(d.usd)) for d in month.daily],
            ledger=ledger,
            topup=topup_mode(),
        )

    @router.get("/spend")
    async def spend(  # pyright: ignore[reportUnusedFunction]
        caller: Admin,
        by: Literal["person", "agent", "channel"] = "person",
        period: credit_views.Period = "this",
    ) -> list[SpendRow]:
        tenant_id = caller.person.tenant_id
        since, until = credit_views.period_window(period, now=runtime.now())
        if by == "channel":
            async with runtime.sessionmaker() as db:
                channels = await credit_views.spend_by_channel(
                    db, tenant_id=tenant_id, markup=markup, since=since, until=until
                )
                names = await credit_views.channel_names(db, tenant_id=tenant_id, rows=channels)
            # Asked outside the read: a platform answer can take a few seconds.
            names = await named_spend_channels(
                runtime,
                tenant_id,
                names,
                [(c.platform, c.channel_id) for c in channels if c.channel_id],
            )
            return spend_rows([_channel_row(c, names) for c in channels])
        async with runtime.sessionmaker() as db:
            if by == "agent":
                by_agent = await credit_views.spend_by_agent_in_tenant(
                    db, tenant_id=tenant_id, markup=markup, since=since, until=until
                )
                return await agent_rows(tenant_id, by_agent)
            people = await credit_views.spend_by_person(
                db, tenant_id=tenant_id, markup=markup, since=since, until=until
            )
            labels = await credit_views.label_people(
                db, tenant_id=tenant_id, user_ids=[p.user_id for p in people]
            )
        rows: list[SpendRow] = []
        for person in people:
            label = labels.get(person.user_id)
            rows.append(
                SpendRow(
                    id=str(label.person_id) if label and label.person_id else person.user_id,
                    name=label.name if label else "Unknown person",
                    kind="Visitor" if label and label.visitor else None,
                    turns=person.turns,
                    usd=_usd(person.usd),
                )
            )
        return spend_rows(rows)

    async def allowance_json(
        db: AsyncSession,
        row: SpendAllowanceRow,
        *,
        now: datetime,
        agents: dict[str, AgentRef],
        people: dict[str, credit_views.PersonLabel],
    ) -> Allowance:
        """One allowance with what its person has used. A group's use is per member."""
        subject, used, members = Subject(kind=row.subject_kind, id=row.subject_id), Decimal(0), None
        since = window_start(row, now=now)
        if row.subject_kind == "person":
            label = people.get(row.subject_id)
            subject = Subject(
                kind="person",
                id=str(label.person_id) if label and label.person_id else row.subject_id,
                name=label.name if label else "Unknown person",
            )
            spenders = [
                await resolve_spend_person(
                    db, tenant_id=row.tenant_id, platform_user_id=row.subject_id
                )
            ]
        elif row.subject_kind == "group":
            everyone = row.subject_id == "everyone"
            subject = Subject(
                kind="group", id=row.subject_id, name="Everyone" if everyone else row.subject_id
            )
            members = (
                await people_store.count_members(db, tenant_id=row.tenant_id)
                if everyone
                else await identities_store.count_people_in_group(
                    db, tenant_id=row.tenant_id, group=row.subject_id
                )
            )
            spenders = []
        else:
            visitors = await _invited(db, row)
            subject = Subject(
                kind="invite",
                id=row.subject_id,
                name=visitors[0].display_name if visitors else "Invite",
            )
            spenders = [
                await spend_person_of_account(
                    db,
                    tenant_id=row.tenant_id,
                    account_id=v.account_id,
                    platform_user_id=str(v.id),
                )
                for v in visitors
            ]
        for spender in spenders:
            used += await spend_allowances.used_since(
                db,
                tenant_id=row.tenant_id,
                platform_user_ids=spender.identities,
                agent_id=row.agent_id,
                since=since,
                markup=markup,
            )
        return Allowance(
            id=str(row.id),
            subject=subject,
            agent=agents[row.agent_id] if row.agent_id else None,
            amount_usd=float(row.amount_usd),
            used_usd=_usd(used),
            members=members,
            window=row.window,
            expires_at=row.expires_at.isoformat() if row.expires_at else None,
        )

    async def allowances_for(
        db: AsyncSession, tenant_id: uuid.UUID, rows: list[SpendAllowanceRow]
    ) -> list[Allowance]:
        now = runtime.now()
        agents = await agents_by_id(tenant_id, {r.agent_id for r in rows if r.agent_id})
        people = await credit_views.label_people(
            db,
            tenant_id=tenant_id,
            user_ids=[r.subject_id for r in rows if r.subject_kind == "person"],
        )
        return [await allowance_json(db, r, now=now, agents=agents, people=people) for r in rows]

    @router.get("/allowances")
    async def allowances(caller: Admin) -> list[Allowance]:  # pyright: ignore[reportUnusedFunction]
        tenant_id = caller.person.tenant_id
        async with runtime.sessionmaker() as db:
            rows = await spend_allowances.list_for_tenant(db, tenant_id=tenant_id)
            return list(reversed(await allowances_for(db, tenant_id, rows)))

    @router.post("/allowances")
    async def add_allowance(body: NewAllowance, caller: Admin) -> Allowance:  # pyright: ignore[reportUnusedFunction]
        tenant_id, now = caller.person.tenant_id, runtime.now()
        if body.expires_at is not None and body.expires_at.astimezone(UTC) <= now:
            raise HTTPException(status_code=422, detail="Pick a date in the future.")
        async with runtime.sessionmaker.begin() as db:
            subject_id = await _subject_id(db, tenant_id, body.subject)
            row = await spend_allowances.create_allowance(
                db,
                tenant_id=tenant_id,
                subject_kind=body.subject.kind,
                subject_id=subject_id,
                agent_id=body.agent.id if body.agent else None,
                amount_usd=body.amount_usd,
                window=body.window,
                starts_at=now,
                expires_at=body.expires_at.astimezone(UTC) if body.expires_at else None,
                created_by_account_id=caller.person.account_id,
            )
            [made] = await allowances_for(db, tenant_id, [row])
            await audit(
                db,
                tenant_id=tenant_id,
                actor_account_id=caller.person.account_id,
                action="Added allowance",
                target=await _audit_target(db, tenant_id, made.subject),
                detail=_allowance_words(made),
                at=now,
            )
            return made

    @router.delete("/allowances/{allowance_id}")
    async def remove_allowance(allowance_id: uuid.UUID, caller: Admin) -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        target = allowance_id
        tenant_id = caller.person.tenant_id
        async with runtime.sessionmaker.begin() as db:
            rows = await spend_allowances.list_for_tenant(db, tenant_id=tenant_id)
            row = next((r for r in rows if r.id == target), None)
            if row is None or not await spend_allowances.delete_allowance(
                db, tenant_id=tenant_id, allowance_id=target
            ):
                raise HTTPException(status_code=404, detail="This allowance no longer exists.")
            [gone] = await allowances_for(db, tenant_id, [row])
            await audit(
                db,
                tenant_id=tenant_id,
                actor_account_id=caller.person.account_id,
                action="Removed allowance",
                target=await _audit_target(db, tenant_id, gone.subject),
                detail=_allowance_words(gone),
                at=runtime.now(),
            )
        return {"ok": True}

    @router.post("/topup")
    async def topup(body: TopUp, caller: Admin) -> dict[str, str | float]:  # pyright: ignore[reportUnusedFunction]
        tenant_id = caller.person.tenant_id
        mode = topup_mode()
        if mode == "checkout" and runtime.stripe and runtime.billing_config:
            if body.amount_usd not in CHECKOUT_AMOUNTS:
                raise HTTPException(status_code=422, detail="Choose $10, $25, $50 or $100.")
            site = str(runtime.web.public_url).rstrip("/")
            config = dataclasses.replace(
                runtime.billing_config,
                success_url=f"{site}/credits?topup=ok",
                cancel_url=f"{site}/credits",
            )
            try:
                url = await create_checkout_session(
                    runtime.stripe, config, tenant_id=tenant_id, amount=int(body.amount_usd)
                )
            except CheckoutAmountUnavailable:
                raise HTTPException(
                    status_code=422, detail="That amount isn't available."
                ) from None
            if url is None:
                raise HTTPException(status_code=502, detail="Checkout isn't available right now.")
            return {"checkout_url": url}
        if mode == "manual":
            if not body.request_id:
                raise HTTPException(status_code=422, detail="Send a request id with the top-up.")
            async with runtime.sessionmaker.begin() as db:
                credited = await manual_credit(
                    db,
                    tenant_id=tenant_id,
                    amount=body.amount_usd,
                    note="web top-up",
                    request_id=body.request_id,
                    actor_account_id=caller.person.account_id,
                )
                if credited.inserted:
                    await audit(
                        db,
                        tenant_id=tenant_id,
                        actor_account_id=caller.person.account_id,
                        action="Topped up",
                        target="Balance",
                        detail=usd(body.amount_usd),
                        at=runtime.now(),
                    )
                balance = await tenant_ledger.get_balance(db, tenant_id=tenant_id)
            return {"balance_usd": _usd(balance)}
        # Neither is set up here, so there is nothing to top up with: not found, not a conflict.
        raise HTTPException(status_code=404, detail="Adding credit isn't available here.")

    return router


async def _audit_target(db: AsyncSession, tenant_id: uuid.UUID, subject: Subject) -> AuditTarget:
    """A group by name; a person or invite by id, which the audit log shows by name while
    they exist. An id that matches no one (a chat-only person) stays a generic label."""
    if subject.kind == "group":
        label = "Everyone" if subject.id == "everyone" else f"{subject.name or subject.id} group"
        return AuditTarget(label)
    try:
        subject_id = uuid.UUID(subject.id)
    except ValueError:
        subject_id = None
    if subject.kind == "person":
        known = subject_id and await people_store.get_person(
            db, tenant_id=tenant_id, person_id=subject_id
        )
        return person_target(known.id) if known else AuditTarget(PERSON_LABEL)
    known = subject_id and await web_invites.get_invite(
        db, tenant_id=tenant_id, invite_id=subject_id
    )
    return invite_target(known.id) if known else AuditTarget(INVITE_LABEL)


def _allowance_words(allowance: Allowance) -> str:
    window = "a month" if allowance.window == "month" else "in total"
    words = f"{usd(allowance.amount_usd)} {window}"
    return f"{words}, {allowance.agent.name}" if allowance.agent else words


def _channel_row(spend: credit_views.ChannelSpend, names: dict[tuple[str, str], str]) -> SpendRow:
    if spend.origin == "routine":
        row_id, name, kind = "routines", "Routines", "routine"
    elif credit_views.is_web_spend(spend.platform, spend.channel_id):
        row_id, name, kind = "web", "Web", "web"
    else:
        row_id = f"{spend.platform}:{spend.channel_id}"
        name = names.get((spend.platform, spend.channel_id or ""), "Unnamed channel")
        kind = spend.platform
    return SpendRow(id=row_id, name=name, kind=kind, turns=spend.turns, usd=_usd(spend.usd))


async def _invited(db: AsyncSession, row: SpendAllowanceRow) -> list[WebPersonRow]:
    """The visitors who came in by the invite the allowance names."""
    try:
        invite_id = uuid.UUID(row.subject_id)
    except ValueError:
        return []
    return [
        p
        for p in await people_store.list_people(db, tenant_id=row.tenant_id)
        if p.invite_id == invite_id
    ]


async def _subject_id(db: AsyncSession, tenant_id: uuid.UUID, subject: Subject) -> str:
    """What the gate matches the subject on. A person is the id their turns run as."""
    if subject.kind != "person":
        return subject.id
    try:
        person_id = uuid.UUID(subject.id)
    except ValueError:
        raise HTTPException(status_code=404, detail="This person isn't in the workspace.") from None
    person = await people_store.get_person(db, tenant_id=tenant_id, person_id=person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="This person isn't in the workspace.")
    return (await linked_identity(db, person))[1]
