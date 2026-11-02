"""The one spend gate every surface asks before a person's request spends money.

Chat turns, thread participation, routines, MCP tool calls and the web all stop on
the same four checks, in this order: the workspace's prepaid balance, the person's
monthly cap, the channel's budget, and the person's spend allowances. Each surface
words its own refusal; this module only decides. Exceptions propagate: the gate
fails closed.

A caller that must not re-run every check (a queued turn that was admitted moments
ago) passes `checks` to run only some, so a surface's behaviour changes only where it
asked for a check it did not have.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, get_args

from daimon.core.allowances import AllowanceVerdict, allowance_verdict, applicable, window_start
from daimon.core.billing import BillingConfig, is_over_cap
from daimon.core.channel_budget import is_over_channel_budget
from daimon.core.stores import identity, spend_allowances, web_identities, web_people
from daimon.core.tenant_balance import is_over_balance
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SpendBlock = Literal["balance_depleted", "cap_exceeded", "budget_exceeded", "allowance_exhausted"]

ALL_CHECKS: frozenset[SpendBlock] = frozenset(get_args(SpendBlock))
AFTER_QUEUE_CHECKS: frozenset[SpendBlock] = frozenset({"balance_depleted", "allowance_exhausted"})
"""What a queued turn is checked against again once it holds its slot: the money that
can run out while it waited. The cap and the channel budget were decided at admission."""


@dataclass(frozen=True)
class SpendPerson:
    """Who is spending, as far as allowances are concerned.

    Allowances name a person by any platform user id of their account (`identities`),
    a group the person is in, or the invite a visitor came in by. A visitor with no
    allowance is blocked; a member with none is not limited.
    """

    groups: frozenset[str] = field(default_factory=frozenset[str])
    invite_id: str | None = None
    is_visitor: bool = False
    identities: frozenset[str] = field(default_factory=frozenset[str])


async def spend_person_of_account(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    platform_user_id: str | None = None,
) -> SpendPerson:
    """The person behind an account: every id they spend under, groups, invite, visitor.

    One person runs under several ids (a chat account and a web one), and an allowance
    names any of them, so what they spent under all of them counts together.
    """
    principals = await identity.list_platform_principals_for_account(session, account_id=account_id)
    ids = {p.external_id for p in principals if p.tenant_id == tenant_id}
    if platform_user_id is not None:
        ids.add(platform_user_id)
    person = await web_people.get_person_for_account(
        session, tenant_id=tenant_id, account_id=account_id
    )
    if person is None:
        return SpendPerson(identities=frozenset(ids))
    sign_ins = await web_identities.list_for_person(session, person_id=person.id)
    return SpendPerson(
        groups=frozenset(g for sign_in in sign_ins for g in sign_in.groups),
        invite_id=None if person.invite_id is None else str(person.invite_id),
        is_visitor=person.kind == "visitor",
        identities=frozenset(ids),
    )


async def resolve_spend_person(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    platform_user_id: str,
    platform: str | None = None,
) -> SpendPerson:
    """`spend_person_of_account` for the account a platform user id belongs to.

    An id nothing is known of is just itself. `platform` narrows the lookup when two
    platforms could hand out the same id.
    """
    principals = await identity.list_principals_by_external_ids(
        session, tenant_id=tenant_id, external_ids=[platform_user_id]
    )
    for principal in principals:
        if platform is None or principal.platform == platform:
            return await spend_person_of_account(
                session,
                tenant_id=tenant_id,
                account_id=principal.account_id,
                platform_user_id=platform_user_id,
            )
    return SpendPerson(identities=frozenset({platform_user_id}))


@dataclass(frozen=True)
class SpendRequest:
    """One request to spend: whose, on which agent, counted against which channel.

    `platform_user_id=None` is the trusted, unbilled path (operator tokens): only the
    workspace balance and the channel budget apply to it, never a cap or an allowance.
    `channel_id` is the channel whose budget the spend counts toward, None for none.
    `person=None` has the gate resolve who the user is from their account.
    """

    tenant_id: uuid.UUID
    platform_user_id: str | None
    platform: str
    channel_id: str | None
    agent_id: str | None
    person: SpendPerson | None = None


async def person_verdict(
    session: AsyncSession, *, request: SpendRequest, markup: Decimal, now: datetime
) -> AllowanceVerdict:
    """Where the person stands against the allowance that governs this request.

    Unlimited (`blocked=False`, nothing left to report) for a person nothing applies
    to; blocked for a visitor nothing applies to.
    """
    user_id = request.platform_user_id
    if user_id is None:
        return allowance_verdict([], used={}, now=now, is_visitor=False)
    rows = await spend_allowances.list_for_tenant(session, tenant_id=request.tenant_id)
    # Resolving the person costs several queries. Without allowances only a web visitor
    # (blocked when nothing allows them) can be limited, so only the web resolves.
    if not rows and request.person is None and request.platform != "web":
        return allowance_verdict([], used={}, now=now, is_visitor=False)
    person = request.person or await resolve_spend_person(
        session,
        tenant_id=request.tenant_id,
        platform_user_id=user_id,
        platform=request.platform or None,
    )
    governing = applicable(
        rows,
        person_ids=person.identities | {user_id},
        groups=person.groups,
        invite_id=person.invite_id,
        agent_id=request.agent_id,
        now=now,
        is_visitor=person.is_visitor,
    )
    used = {
        row.id: await spend_allowances.used_since(
            session,
            tenant_id=request.tenant_id,
            platform_user_ids=person.identities | {user_id},
            agent_id=row.agent_id,
            since=window_start(row, now=now),
            markup=markup,
        )
        for row in governing
    }
    return allowance_verdict(governing, used=used, now=now, is_visitor=person.is_visitor)


async def spend_gate(
    session: AsyncSession,
    *,
    billing_config: BillingConfig | None,
    request: SpendRequest,
    markup: Decimal,
    now: datetime,
    checks: frozenset[SpendBlock] = ALL_CHECKS,
) -> SpendBlock | None:
    """The first check that stops this request, or None when it may spend."""
    if "balance_depleted" in checks and await is_over_balance(
        tenant_id=request.tenant_id, session=session
    ):
        return "balance_depleted"
    if (
        "cap_exceeded" in checks
        and request.platform_user_id is not None
        and await is_over_cap(
            billing_config=billing_config,
            tenant_id=request.tenant_id,
            user_id=request.platform_user_id,
            now=now,
            session=session,
        )
    ):
        return "cap_exceeded"
    if "budget_exceeded" in checks and await is_over_channel_budget(
        tenant_id=request.tenant_id,
        platform=request.platform,
        channel_id=request.channel_id,
        now=now,
        session=session,
    ):
        return "budget_exceeded"
    if (
        "allowance_exhausted" in checks
        and request.platform_user_id is not None
        and (await person_verdict(session, request=request, markup=markup, now=now)).blocked
    ):
        return "allowance_exhausted"
    return None


async def spend_gate_in_new_session(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    billing_config: BillingConfig | None,
    request: SpendRequest,
    markup: Decimal,
    now: datetime | None = None,
    checks: frozenset[SpendBlock] = ALL_CHECKS,
) -> SpendBlock | None:
    """`spend_gate` for a caller that holds no session; `now` defaults to the present."""
    async with sessionmaker() as session:
        return await spend_gate(
            session,
            billing_config=billing_config,
            request=request,
            markup=markup,
            now=now or datetime.now(UTC),
            checks=checks,
        )
