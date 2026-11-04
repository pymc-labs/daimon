"""`/api/credits/*`: a person's own meter, and the workspace's balance, spend and allowances.

Every figure is billed dollars (model cost times the workspace markup), the basis the
ledger and allowances count. Everything but `mine` is for admins (`credits.manage`).
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import Collection, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from anthropic import APIStatusError
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.web.access import Role
from daimon.adapters.web.audit import (
    audit,
    usd,
)
from daimon.adapters.web.caller import WebCaller, current_caller, groups_of, requires
from daimon.adapters.web.channel_names import named_spend_channels
from daimon.adapters.web.chat_json import AgentRef, agent_ref
from daimon.adapters.web.faces import avatars_of
from daimon.adapters.web.runtime import WebRuntime
from daimon.core import credit_report, credit_views, web_allowances
from daimon.core.allowances import window_start
from daimon.core.billing import (
    CheckoutAmountUnavailable,
    create_checkout_session,
    manual_credit,
)
from daimon.core.ma_resolver import retrieve_agent_cached
from daimon.core.panel_audit import record_panel_write
from daimon.core.promo_codes import describe_refusal
from daimon.core.promo_credit import PromoRedeemed, PromoRedeemRefused, redeem_in_transaction
from daimon.core.spend_gate import (
    SpendRequest,
    person_verdict,
    resolve_spend_person,
    spend_person_of_account,
)
from daimon.core.stores import spend_allowances, tenant_ledger
from daimon.core.stores import web_identities as identities_store
from daimon.core.stores import web_people as people_store
from daimon.core.stores.domain import SpendAllowanceRow, WebPersonRow
from daimon.core.web_admin import WebAdmin, WebAdminError
from daimon.core.web_identity import linked_identity, principal_of, with_spend_person
from fastapi import APIRouter, Depends, HTTPException
from mux.errors import ScopeViolation
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

#: Amounts, in whole dollars, a card checkout may buy.
CHECKOUT_AMOUNTS = frozenset({10, 25, 50, 100})
#: Refusals worded for the web app, where there is no channel to redeem in.
_REDEEM_WORDS = {
    "needs_channel": "That code adds to a channel's budget. Redeem it in that channel.",
}
_NO_AGENT = credit_report.NO_AGENT


def _omit_none(value: object) -> bool:
    return value is None


_usd = credit_report.usd_of


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
    spent_usd: float
    """The last 30 days."""
    turns: int
    spent_month_usd: float
    projected_month_usd: float
    daily: list[DailySpend]
    ledger: list[LedgerEntry]
    topup: Literal["checkout", "manual"] | None
    funding: Literal["prepaid", "operator_funded"]
    """Who pays for turns: the workspace's own credit, or the operator."""


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
    cap: bool
    """A monthly ceiling on top of the allowance that governs, not an allowance."""


class AgentChoice(BaseModel):
    id: str = Field(min_length=1, max_length=200)


class NewAllowance(BaseModel):
    subject: Subject
    agent: AgentChoice | None = None
    amount_usd: Decimal = Field(gt=0, le=1_000_000, decimal_places=2)
    window: Literal["month", "total"]
    expires_at: datetime | None = None
    cap: bool = False
    """A cap is monthly, for everyone or one person, for every agent, with no end date."""


class EditAllowance(BaseModel):
    amount_usd: Decimal = Field(gt=0, le=1_000_000, decimal_places=2)


class Redeem(BaseModel):
    code: str = Field(min_length=1, max_length=100)


class Redeemed(BaseModel):
    balance_usd: float
    amount_usd: float
    """The credit the code adds."""
    starts_at: str | None
    """When a code that has not started yet adds it."""


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

    def spend_row(entry: credit_report.SpendEntry) -> SpendRow:
        return SpendRow(
            id=entry.id,
            name=entry.name,
            avatar=entry.avatar,
            kind=entry.kind,
            turns=entry.turns,
            usd=entry.usd,
        )

    async def agent_labels(
        tenant_id: uuid.UUID, agent_ids: Collection[str]
    ) -> dict[str, credit_report.AgentLabel]:
        refs = await agents_by_id(tenant_id, agent_ids)
        return {i: credit_report.AgentLabel(r.name, r.avatar) for i, r in refs.items()}

    @router.get("/meter")
    async def meter(  # pyright: ignore[reportUnusedFunction]
        caller: Annotated[WebCaller, Depends(current_caller)],
    ) -> CreditMeter | None:
        """Only where the caller stands against their allowance: what `/api/me` carries as
        `credit`, for a client that wants it fresh without the rest."""
        return await credit_meter(runtime, caller)

    @router.get("/mine")
    async def mine(  # pyright: ignore[reportUnusedFunction]
        caller: Member, period: credit_views.Period = "30d"
    ) -> Mine:
        tenant_id = caller.person.tenant_id
        async with runtime.sessionmaker() as db:
            _, user_id = await linked_identity(db, caller.person)
        report = await credit_report.my_credits(
            runtime.sessionmaker,
            tenant_id=tenant_id,
            account_id=caller.person.account_id,
            platform_user_id=user_id,
            period=period,
            now=runtime.now(),
            markup=markup,
            agent_labels=lambda ids: agent_labels(tenant_id, ids),
        )
        refs = await agents_by_id(tenant_id, {t.agent_id for t in report.turns if t.agent_id})
        return Mine(
            by_agent=[spend_row(e) for e in report.by_agent],
            turns=[
                Turn(
                    id=t.id,
                    at=t.at,
                    agent=refs[t.agent_id] if t.agent_id else agent_ref(None, _NO_AGENT),
                    title=t.title,
                    platform=t.platform,
                    usd=t.usd,
                )
                for t in report.turns
            ],
            spent_usd=report.spent_usd,
            turn_count=report.turn_count,
        )

    @router.get("/overview")
    async def overview(caller: Admin) -> Overview:  # pyright: ignore[reportUnusedFunction]
        report = await credit_report.overview_report(
            runtime.sessionmaker,
            tenant_id=caller.person.tenant_id,
            now=runtime.now(),
            markup=markup,
        )
        return Overview(
            balance_usd=report.balance_usd,
            spent_usd=report.spent_usd,
            turns=report.turns,
            spent_month_usd=report.spent_month_usd,
            projected_month_usd=report.projected_month_usd,
            daily=[DailySpend(day=d.day, usd=d.usd) for d in report.daily],
            ledger=[
                LedgerEntry(
                    id=e.id, at=e.at, kind=e.kind, amount_usd=e.amount_usd, by=e.by, note=e.note
                )
                for e in report.ledger
            ],
            topup=topup_mode(),
            funding=report.funding,
        )

    @router.get("/spend")
    async def spend(  # pyright: ignore[reportUnusedFunction]
        caller: Admin,
        by: Literal["person", "agent", "channel"] = "person",
        period: credit_views.Period = "30d",
    ) -> list[SpendRow]:
        tenant_id = caller.person.tenant_id

        async def channel_names(
            names: dict[tuple[str, str], str], channels: Sequence[tuple[str, str]]
        ) -> dict[tuple[str, str], str]:
            return await named_spend_channels(runtime, tenant_id, names, channels)

        rows = await credit_report.spend_breakdown(
            runtime.sessionmaker,
            tenant_id=tenant_id,
            by=by,
            period=period,
            now=runtime.now(),
            markup=markup,
            agent_labels=lambda ids: agent_labels(tenant_id, ids),
            channel_names=channel_names,
        )
        return [spend_row(e) for e in rows]

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
            cap=row.is_cap,
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

    def admin_of(caller: WebCaller) -> WebAdmin:
        return WebAdmin(
            tenant_id=caller.person.tenant_id,
            account_id=caller.person.account_id,
            role=caller.role,
        )

    async def agent_names_of(tenant_id: uuid.UUID, agent_ids: Collection[str]) -> dict[str, str]:
        return {i: ref.name for i, ref in (await agents_by_id(tenant_id, agent_ids)).items()}

    @router.post("/allowances")
    async def add_allowance(body: NewAllowance, caller: Admin) -> Allowance:  # pyright: ignore[reportUnusedFunction]
        tenant_id = caller.person.tenant_id
        agent_id = body.agent.id if body.agent else None
        names = await agent_names_of(tenant_id, [agent_id] if agent_id else [])
        try:
            async with runtime.sessionmaker.begin() as db:
                row = await web_allowances.create_allowance(
                    db,
                    admin_of(caller),
                    runtime.now(),
                    subject_kind=body.subject.kind,
                    subject_id=body.subject.id,
                    agent_id=agent_id,
                    agent_names=names,
                    amount_usd=body.amount_usd,
                    window=body.window,
                    expires_at=body.expires_at,
                    cap=body.cap,
                )
                [made] = await allowances_for(db, tenant_id, [row])
                return made
        except WebAdminError as error:
            raise HTTPException(status_code=error.status, detail=str(error)) from error

    @router.patch("/allowances/{allowance_id}")
    async def edit_allowance(  # pyright: ignore[reportUnusedFunction]
        allowance_id: uuid.UUID, body: EditAllowance, caller: Admin
    ) -> Allowance:
        tenant_id = caller.person.tenant_id
        try:
            async with runtime.sessionmaker.begin() as db:
                current = next(
                    (
                        r
                        for r in await spend_allowances.list_for_tenant(db, tenant_id=tenant_id)
                        if r.id == allowance_id
                    ),
                    None,
                )
                names = await agent_names_of(
                    tenant_id, [current.agent_id] if current and current.agent_id else []
                )
                row = await web_allowances.set_allowance_amount(
                    db,
                    admin_of(caller),
                    runtime.now(),
                    allowance_id,
                    amount_usd=body.amount_usd,
                    agent_names=names,
                )
                [made] = await allowances_for(db, tenant_id, [row])
                return made
        except WebAdminError as error:
            raise HTTPException(status_code=error.status, detail=str(error)) from error

    @router.delete("/allowances/{allowance_id}")
    async def remove_allowance(allowance_id: uuid.UUID, caller: Admin) -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
        tenant_id = caller.person.tenant_id
        try:
            async with runtime.sessionmaker.begin() as db:
                current = next(
                    (
                        r
                        for r in await spend_allowances.list_for_tenant(db, tenant_id=tenant_id)
                        if r.id == allowance_id
                    ),
                    None,
                )
                names = await agent_names_of(
                    tenant_id, [current.agent_id] if current and current.agent_id else []
                )
                await web_allowances.delete_allowance(
                    db, admin_of(caller), runtime.now(), allowance_id, agent_names=names
                )
        except WebAdminError as error:
            raise HTTPException(status_code=error.status, detail=str(error)) from error
        return {"ok": True}

    @router.post("/redeem")
    async def redeem(body: Redeem, caller: Admin) -> Redeemed:  # pyright: ignore[reportUnusedFunction]
        """Redeem a promo code for the workspace, as the chat panels do, and audit it."""
        tenant_id, account_id, now = (
            caller.person.tenant_id,
            caller.person.account_id,
            runtime.now(),
        )
        async with runtime.sessionmaker.begin() as db:
            principal = await caller.principal(db)
            result = await redeem_in_transaction(
                db,
                tenant_id=tenant_id,
                account_id=account_id,
                code=body.code,
                now=now,
                channel=None,
                is_admin=True,
            )
            if isinstance(result, PromoRedeemed):
                await audit(
                    db,
                    tenant_id=tenant_id,
                    actor_account_id=account_id,
                    action="Redeemed promo code",
                    target="Balance",
                    detail=usd(result.amount_usd),
                    at=now,
                )
        await record_panel_write(
            runtime.sessionmaker,
            tenant_id=tenant_id,
            platform=principal.platform,
            platform_user_id=principal.external_id,
            op="promo_redeem",
            outcome="denied" if isinstance(result, PromoRedeemRefused) else "allowed",
            reason=result.reason if isinstance(result, PromoRedeemRefused) else "completed",
        )
        if isinstance(result, PromoRedeemRefused):
            raise HTTPException(
                status_code=429 if result.reason == "throttled" else 422,
                detail=_REDEEM_WORDS.get(result.reason) or describe_refusal(result.reason),
            )
        return Redeemed(
            balance_usd=_usd(result.balance_usd),
            amount_usd=_usd(result.amount_usd),
            starts_at=(
                None
                if result.granted or result.credit_starts_at is None
                else result.credit_starts_at.isoformat()
            ),
        )

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
