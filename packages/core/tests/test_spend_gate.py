"""The one spend gate: check order, subsets, allowances and the unchanged no-allowance path."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from daimon.core import spend_gate as spend_gate_module
from daimon.core.allowances import AllowanceVerdict
from daimon.core.billing import BillingConfig
from daimon.core.spend_gate import (
    AFTER_QUEUE_CHECKS,
    SpendBlock,
    SpendPerson,
    SpendRequest,
    person_verdict,
    resolve_spend_person,
    spend_gate,
)
from daimon.core.stores import (
    spend_allowances,
    tenant_ledger,
    tenant_user_caps,
    usage_events,
    web_identities,
)
from daimon.core.stores.domain import AllowanceWindow
from daimon.testing.factories import (
    make_account,
    make_platform_principal,
    make_tenant,
    make_web_person,
)
from mux.testing.drivers.anthropic.ma_models import ma_model_usage
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

BILLING = BillingConfig(
    secret_key=SecretStr("sk_test"),
    webhook_secret=SecretStr("whsec_test"),
    prices={10: "p10"},
    success_url="http://test/success",
    cancel_url="http://test/cancel",
)
MARKUP = Decimal("1.2")
NOW = datetime.now(UTC)


async def _fund(session: AsyncSession, tenant_id: uuid.UUID, usd: str = "100") -> None:
    await tenant_ledger.insert_entry(
        session,
        tenant_id=tenant_id,
        delta_usd=Decimal(usd),
        reason="topup",
        idempotency_key=f"fund:{tenant_id}:{usd}",
    )


async def _spend(session: AsyncSession, tenant_id: uuid.UUID, user: str, agent: str | None) -> None:
    """One $5 model call (cost before markup)."""
    await usage_events.record(
        session,
        tenant_id=tenant_id,
        platform_user_id=user,
        managed_session_id=f"s-{user}-{agent}",
        model="claude-opus-4-7",
        model_usage=ma_model_usage(input_tokens=1_000_000, output_tokens=0),
        event_id=f"e-{user}-{agent}",
        agent_id=agent,
    )


async def _allow(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    kind: str = "group",
    subject: str = "everyone",
    amount: str = "5",
    agent: str | None = None,
) -> None:
    await spend_allowances.create_allowance(
        session,
        tenant_id=tenant_id,
        subject_kind=kind,  # type: ignore[arg-type]
        subject_id=subject,
        agent_id=agent,
        amount_usd=Decimal(amount),
        window="month",
        starts_at=NOW - timedelta(days=40),
    )


async def _gate(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    user: str | None = "u1",
    agent: str | None = "a1",
    person: SpendPerson | None = None,
    checks: frozenset[SpendBlock] | None = None,
) -> SpendBlock | None:
    request = SpendRequest(
        tenant_id=tenant_id,
        platform_user_id=user,
        platform="discord",
        channel_id=None,
        agent_id=agent,
        person=person,
    )
    if checks is None:
        return await spend_gate(
            session, billing_config=BILLING, request=request, markup=MARKUP, now=NOW
        )
    return await spend_gate(
        session, billing_config=BILLING, request=request, markup=MARKUP, now=NOW, checks=checks
    )


async def test_a_funded_person_with_no_allowance_is_unlimited(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    await _fund(db_session, tenant.id)
    await _spend(db_session, tenant.id, "u1", "a1")
    assert await _gate(db_session, tenant.id) is None, "no allowance: nothing stops a member"


async def test_balance_is_checked_first(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    await _spend(db_session, tenant.id, "u1", "a1")
    await _allow(db_session, tenant.id, amount="1")
    assert await _gate(db_session, tenant.id) == "balance_depleted", (
        "an unfunded workspace is refused on its balance before any allowance"
    )


async def test_cap_comes_before_allowance(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    await _fund(db_session, tenant.id)
    await _spend(db_session, tenant.id, "u1", "a1")
    await tenant_user_caps.set_default(db_session, tenant_id=tenant.id, amount=Decimal("5"))
    await _allow(db_session, tenant.id, amount="1")
    assert await _gate(db_session, tenant.id) == "cap_exceeded", (
        "a person over the cap and over an allowance is refused on the cap"
    )


async def test_an_exhausted_allowance_blocks_on_billed_dollars(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    await _fund(db_session, tenant.id)
    await _spend(db_session, tenant.id, "u1", "a1")  # $5 cost, $6 billed
    await _allow(db_session, tenant.id, amount="5.50")
    assert await _gate(db_session, tenant.id) == "allowance_exhausted", (
        "$6 billed is over a $5.50 allowance although the cost alone ($5) is not"
    )
    assert await _gate(db_session, tenant.id, user="u2") is None, (
        "another person has spent nothing of theirs"
    )


async def test_an_allowance_with_room_left_does_not_block(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    await _fund(db_session, tenant.id)
    await _spend(db_session, tenant.id, "u1", "a1")
    await _allow(db_session, tenant.id, amount="6.50")
    assert await _gate(db_session, tenant.id) is None, "$0.50 is left"


async def test_an_agent_allowance_governs_only_that_agent(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    await _fund(db_session, tenant.id)
    await _spend(db_session, tenant.id, "u1", "a1")
    await _allow(db_session, tenant.id, amount="1", agent="a1")
    assert await _gate(db_session, tenant.id, agent="a1") == "allowance_exhausted", (
        "the agent's own allowance is used up"
    )
    assert await _gate(db_session, tenant.id, agent="a2") is None, (
        "another agent is not covered by it"
    )


async def test_a_visitor_without_an_allowance_is_blocked(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    await _fund(db_session, tenant.id)
    visitor = SpendPerson(is_visitor=True)
    assert await _gate(db_session, tenant.id, person=visitor) == "allowance_exhausted", (
        "a visitor spends only what an allowance grants"
    )
    await _allow(db_session, tenant.id, kind="invite", subject="inv1", amount="5")
    invited = SpendPerson(is_visitor=True, invite_id="inv1")
    assert await _gate(db_session, tenant.id, person=invited) is None, (
        "the invite's allowance lets the visitor in"
    )


async def test_a_visitor_gets_the_invite_allowance_not_the_everyone_group_one(
    db_session: AsyncSession,
) -> None:
    """The gate and the meter read one verdict: an invite's $5 total beats a $40 monthly group."""
    tenant = await make_tenant(db_session)
    await _allow(db_session, tenant.id, amount="40")
    await spend_allowances.create_allowance(
        db_session,
        tenant_id=tenant.id,
        subject_kind="invite",
        subject_id="inv1",
        agent_id=None,
        amount_usd=Decimal(5),
        window="total",
        starts_at=NOW - timedelta(days=1),
    )
    request = SpendRequest(
        tenant_id=tenant.id,
        platform_user_id="v1",
        platform="web",
        channel_id=None,
        agent_id=None,
        person=SpendPerson(is_visitor=True, invite_id="inv1"),
    )
    verdict = await person_verdict(db_session, request=request, markup=MARKUP, now=NOW)
    assert (verdict.left_usd, verdict.total_usd, verdict.window) == (
        Decimal(5),
        Decimal(5),
        "total",
    ), "the visitor's meter reads the invite's $5, not the group's $40"
    await _spend(db_session, tenant.id, "v1", None)
    after = await person_verdict(db_session, request=request, markup=MARKUP, now=NOW)
    assert after.blocked, "$6 billed against the $5 invite blocks the visitor despite the group"


async def _invited_member_verdict(
    db_session: AsyncSession, *, invite_amount: str, invite_window: AllowanceWindow
) -> AllowanceVerdict:
    """A member who came in by an invite and has billed $6, against a $40 monthly everyone."""
    tenant = await make_tenant(db_session)
    await _allow(db_session, tenant.id, amount="40")
    await spend_allowances.create_allowance(
        db_session,
        tenant_id=tenant.id,
        subject_kind="invite",
        subject_id="inv1",
        agent_id=None,
        amount_usd=Decimal(invite_amount),
        window=invite_window,
        starts_at=NOW - timedelta(days=1),
    )
    await _spend(db_session, tenant.id, "m1", None)
    request = SpendRequest(
        tenant_id=tenant.id,
        platform_user_id="m1",
        platform="web",
        channel_id=None,
        agent_id=None,
        person=SpendPerson(invite_id="inv1"),
    )
    return await person_verdict(db_session, request=request, markup=MARKUP, now=NOW)


async def test_a_promoted_visitor_whose_invite_is_spent_gets_the_everyone_group(
    db_session: AsyncSession,
) -> None:
    verdict = await _invited_member_verdict(db_session, invite_amount="5", invite_window="total")
    assert not verdict.blocked and verdict.left_usd == Decimal(34), (
        "a member keeps the invite they came in by, but the spent $5 does not cap them"
    )


async def test_a_member_with_a_small_invite_allowance_gets_the_larger_group_one(
    db_session: AsyncSession,
) -> None:
    verdict = await _invited_member_verdict(db_session, invite_amount="10", invite_window="month")
    assert (verdict.left_usd, verdict.total_usd) == (Decimal(34), Decimal(40)), (
        "invite and group rank together, so the most left wins"
    )


async def test_the_person_is_resolved_only_where_an_allowance_could_apply(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Resolving costs several queries; a chat platform with no allowance rows skips them."""
    resolved: list[str] = []

    async def spy(*_args: object, **_kw: object) -> SpendPerson:
        resolved.append("resolved")
        return SpendPerson(is_visitor=True)

    monkeypatch.setattr(spend_gate_module, "resolve_spend_person", spy)
    tenant = await make_tenant(db_session)

    def request(platform: str) -> SpendRequest:
        return SpendRequest(
            tenant_id=tenant.id,
            platform_user_id="u1",
            platform=platform,
            channel_id=None,
            agent_id=None,
        )

    chat = await person_verdict(db_session, request=request("discord"), markup=MARKUP, now=NOW)
    assert not chat.blocked and resolved == [], "no allowance rows on a chat platform: no lookup"
    web = await person_verdict(db_session, request=request("web"), markup=MARKUP, now=NOW)
    assert web.blocked and resolved == ["resolved"], "the web resolves, so a visitor is blocked"
    await _allow(db_session, tenant.id, kind="person", subject="other", amount="5")
    await person_verdict(db_session, request=request("discord"), markup=MARKUP, now=NOW)
    assert resolved == ["resolved", "resolved"], "with allowance rows the person is resolved"


async def test_the_trusted_path_skips_the_person_checks(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    await _fund(db_session, tenant.id)
    await _allow(db_session, tenant.id, amount="0")
    assert await _gate(db_session, tenant.id, user=None) is None, (
        "an unbilled operator path has no person to count"
    )


async def test_after_queue_checks_leave_out_the_cap(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    await _fund(db_session, tenant.id)
    await _spend(db_session, tenant.id, "u1", "a1")
    await tenant_user_caps.set_default(db_session, tenant_id=tenant.id, amount=Decimal("1"))
    assert await _gate(db_session, tenant.id, checks=AFTER_QUEUE_CHECKS) is None, (
        "a queued turn is not stopped again by the cap it passed at admission"
    )
    await _allow(db_session, tenant.id, amount="1")
    assert await _gate(db_session, tenant.id, checks=AFTER_QUEUE_CHECKS) == "allowance_exhausted", (
        "but an allowance used up while it waited does stop it"
    )


async def test_the_resolver_finds_every_id_and_the_groups_of_one_account(
    db_session: AsyncSession,
) -> None:
    """A chat id resolves to the whole person: both ids and the groups they sign in with."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    person = await make_web_person(db_session, tenant=tenant, account=account)
    for platform, external in (("web", str(person.id)), ("discord", "chat-id")):
        await make_platform_principal(
            db_session, platform=platform, external_id=external, tenant=tenant, account=account
        )
    await web_identities.link_identity(
        db_session,
        tenant_id=tenant.id,
        person_id=person.id,
        provider="oidc",
        issuer="https://idp",
        subject="sub-1",
        label="",
        groups=["team"],
    )
    resolved = await resolve_spend_person(
        db_session, tenant_id=tenant.id, platform_user_id="chat-id", platform="discord"
    )
    assert resolved.identities == {"chat-id", str(person.id)}, "both of the person's ids count"
    assert resolved.groups == {"team"}, "the groups of their sign-in apply on the chat id too"
    assert not resolved.is_visitor, "a member is not a visitor"
    stranger = await resolve_spend_person(
        db_session, tenant_id=tenant.id, platform_user_id="nobody", platform="discord"
    )
    assert stranger == SpendPerson(identities=frozenset({"nobody"})), "an unknown id is itself"


async def test_a_person_allowance_on_the_web_id_governs_and_counts_their_chat_id(
    db_session: AsyncSession,
) -> None:
    """Chat spend and web spend of one person draw on the one allowance named by either id."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    person = await make_web_person(db_session, tenant=tenant, account=account)
    for platform, external in (("web", str(person.id)), ("discord", "chat-id")):
        await make_platform_principal(
            db_session, platform=platform, external_id=external, tenant=tenant, account=account
        )
    await _fund(db_session, tenant.id)
    await _allow(db_session, tenant.id, kind="person", subject=str(person.id), amount="5")
    assert await _gate(db_session, tenant.id, user="chat-id", person=None) is None, (
        "nothing is spent yet, so the allowance has room"
    )
    await _spend(db_session, tenant.id, str(person.id), "a1")
    assert await _gate(db_session, tenant.id, user="chat-id", person=None) == (
        "allowance_exhausted"
    ), "spend under the web id uses up the allowance for the chat id too"
