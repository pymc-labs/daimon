"""The tables an account erasure walks, shared by `purge` and the `privacy` preview.

Each `Erasable` names a `PurgeReport` field and says how to count its rows for a
`Subject` (the preview), how to erase them for one principal (the principal pass)
and how to erase what is left for the account (the account pass, after every
principal). Purge runs each pass in `ERASABLE` order, which is FK-safe: rows that
reference a principal go before the principal row. A new table is one entry here
plus its `PurgeReport` and `PurgePreview` fields. Watches, mux state, the account
row and the GitHub user stay hand-written in `purge` and `privacy`.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from daimon.core.data_audience import user_subject
from daimon.core.stores import (
    accounts,
    agent_posts,
    credential_requests,
    direct_messages,
    dm_access,
    dm_carry_overs,
    github_connect,
    held_timers,
    host_call_cards,
    identity,
    lead_intake,
    mcp_tokens,
    monitoring,
    ops_events,
    room_viewers,
    routines,
    secret_shares,
    slack_turn_contexts,
    slack_user_tokens,
    support,
    tag_opt_outs,
    tenants,
    user_skills,
    web_people,
    web_roles,
    wizard_session,
)
from daimon.core.stores import coding_key_claims as key_claims
from daimon.core.stores import connector_tokens as connectors
from daimon.core.stores import gemini_runtime_state as gemini
from daimon.core.stores import message_feedback as feedback
from daimon.core.stores import microsoft_user_tokens as microsoft
from daimon.core.stores import platform_names as names
from daimon.core.stores import selfhosted_resources as selfhosted
from daimon.core.stores import session_settle_state as settle_state
from daimon.core.stores import support_escalation as escalations
from daimon.core.stores import teams_message_admissions as teams_messages
from daimon.core.stores.domain import CliPrincipalRow, PlatformPrincipalRow
from sqlalchemy.ext.asyncio import AsyncSession

Principal = CliPrincipalRow | PlatformPrincipalRow


@dataclass(frozen=True)
class Subject:
    """An account and its principals, as erasure sees them."""

    account_id: uuid.UUID
    cli: tuple[CliPrincipalRow, ...] = ()
    platform: tuple[PlatformPrincipalRow, ...] = ()

    @property
    def principals(self) -> tuple[Principal, ...]:
        return (*self.cli, *self.platform)

    @property
    def tenant_ids(self) -> set[uuid.UUID]:
        return {p.tenant_id for p in self.principals}

    @property
    def user_keys(self) -> list[tuple[uuid.UUID, str]]:
        """(tenant, platform user) keys: rows cast before an account existed carry only these."""
        return [(pp.tenant_id, pp.external_id) for pp in self.platform]


Count = Callable[[AsyncSession, Subject], Awaitable[int]]
Example = Callable[[AsyncSession, Subject], Awaitable[str | None]]
ErasePrincipal = Callable[[AsyncSession, Principal], Awaitable[int]]
EraseAccount = Callable[[AsyncSession, Subject], Awaitable[int]]
UserFn = Callable[[AsyncSession, PlatformPrincipalRow], Awaitable[int]]
AccountFn = Callable[..., Awaitable[int]]


@dataclass(frozen=True)
class Erasable:
    """One `PurgeReport` field: how the preview counts it and each pass erases it."""

    key: str
    count: Count | None = None  # None: not previewed under this key
    principal: ErasePrincipal | None = None
    account: EraseAccount | None = None
    example: Example | None = None


def kind_of(principal: Principal) -> Literal["cli", "platform"]:
    return "platform" if isinstance(principal, PlatformPrincipalRow) else "cli"


def _requester(principal: Principal) -> str:
    return (
        principal.external_id if isinstance(principal, PlatformPrincipalRow) else principal.os_user
    )


def by_account(key: str, *pairs: tuple[AccountFn, AccountFn]) -> Erasable:
    """Rows keyed by account id: (count, delete) pairs, summed, deleted in order."""

    async def count(session: AsyncSession, subject: Subject) -> int:
        return sum([await fn(session, account_id=subject.account_id) for fn, _ in pairs])

    async def erase(session: AsyncSession, subject: Subject) -> int:
        return sum([await fn(session, account_id=subject.account_id) for _, fn in pairs])

    return Erasable(key, count=count, account=erase)


def by_user(
    key: str,
    erase: UserFn,
    *platforms: str,
    count: UserFn | None = None,
    total: Count | None = None,
    account: EraseAccount | None = None,
    example: Example | None = None,
) -> Erasable:
    """Rows keyed by a platform principal's user id, optionally on some platforms only.

    The preview sums `count` over the account's platform principals, or runs `total`.
    """

    def applies(p: Principal) -> bool:
        return isinstance(p, PlatformPrincipalRow) and (not platforms or p.platform in platforms)

    async def summed(session: AsyncSession, subject: Subject) -> int:
        assert count is not None
        return sum([await count(session, pp) for pp in subject.platform if applies(pp)])

    async def one(session: AsyncSession, p: Principal) -> int:
        return await erase(session, p) if isinstance(p, PlatformPrincipalRow) and applies(p) else 0

    return Erasable(
        key,
        count=total or (summed if count else None),
        principal=one,
        account=account,
        example=example,
    )


def by_requester(key: str, count: AccountFn, erase: AccountFn) -> Erasable:
    """Rows keyed by (requester id, tenant) for any principal; the preview dedups keys."""

    async def total(session: AsyncSession, subject: Subject) -> int:
        keys = {(_requester(p), p.tenant_id) for p in subject.principals}
        return sum([await count(session, platform_user_id=u, tenant_id=t) for u, t in keys])

    async def one(session: AsyncSession, p: Principal) -> int:
        return await erase(session, platform_user_id=_requester(p), tenant_id=p.tenant_id)

    return Erasable(key, count=total, principal=one)


def _tpu(fn: AccountFn) -> UserFn:
    """Adapt a (tenant_id, platform, platform_user_id)-keyed store helper."""
    return lambda s, pp: fn(
        s, tenant_id=pp.tenant_id, platform=pp.platform, platform_user_id=pp.external_id
    )


def _tu(fn: AccountFn) -> UserFn:
    """Adapt a (tenant_id, user_id)-keyed store helper."""
    return lambda s, pp: fn(s, tenant_id=pp.tenant_id, user_id=pp.external_id)


def _te(fn: AccountFn) -> UserFn:
    """Adapt a (tenant_id, external_id)-keyed store helper."""
    return lambda s, pp: fn(s, tenant_id=pp.tenant_id, external_id=pp.external_id)


def _who(pp: PlatformPrincipalRow) -> tuple[uuid.UUID, str, str]:
    return pp.tenant_id, pp.platform, pp.external_id


async def _erase_admin(session: AsyncSession, pp: PlatformPrincipalRow) -> int:
    # The grant row is the tenant's: only the person's id leaves it and its audit subject.
    from daimon.core.stores.daimon_admins import erase_user
    from daimon.core.stores.security_audit import erase_admin_subject

    t, platform, user = _who(pp)
    n = await erase_user(session, tenant_id=t, platform=platform, platform_user_id=user)
    await erase_admin_subject(session, tenant_id=t, platform=platform, platform_user_id=user)
    return n


async def _count_admin(session: AsyncSession, pp: PlatformPrincipalRow) -> int:
    from daimon.core.stores.daimon_admins import count_user_rows

    t, platform, user = _who(pp)
    return await count_user_rows(session, tenant_id=t, platform=platform, platform_user_id=user)


async def _slack_token(session: AsyncSession, pp: PlatformPrincipalRow, *, delete: bool) -> int:
    # Keyed by (team, Slack user); the team is the tenant's external id.
    tenant = await tenants.get_tenant(session, pp.tenant_id)
    if tenant is None:
        return 0
    if delete:
        return await slack_user_tokens.delete_slack_user_token(
            session, team_id=tenant.external_id, slack_user_id=pp.external_id
        )
    token = await slack_user_tokens.get_slack_user_token(
        session, team_id=tenant.external_id, slack_user_id=pp.external_id
    )
    return int(token is not None)


async def _room_viewer(session: AsyncSession, pp: PlatformPrincipalRow, *, delete: bool) -> int:
    # Stored as reader subjects, whose workspace is the tenant's external id.
    tenant = await tenants.get_tenant(session, pp.tenant_id)
    if tenant is None:
        return 0
    viewer = user_subject(pp.platform, tenant.external_id, pp.external_id)
    fn = room_viewers.forget_viewer if delete else room_viewers.count_viewer
    return await fn(session, tenant_id=pp.tenant_id, viewer=viewer)


def by_imported_user(key: str, count: AccountFn, erase: AccountFn) -> Erasable:
    """Rows imported before the person had an account, keyed by (tenant, platform, user)."""

    def users(subject: Subject) -> list[tuple[uuid.UUID, str, str]]:
        return [_who(pp) for pp in subject.platform]

    return by_user(
        key,
        lambda s, pp: erase(s, users=[_who(pp)]),
        total=lambda s, sub: count(s, users=users(sub)),
        account=lambda s, sub: erase(s, users=users(sub)),
    )


def by_dm_user(key: str, count: AccountFn, erase: AccountFn) -> Erasable:
    """DM routing rows naming the person by (tenant, platform user)."""
    return by_user(
        key,
        lambda s, pp: erase(s, keys=[(pp.tenant_id, pp.external_id)], now=datetime.now(UTC)),
        total=lambda s, sub: count(s, keys=sub.user_keys),
        account=lambda s, sub: erase(s, keys=sub.user_keys, now=datetime.now(UTC)),
    )


async def _first_routine(session: AsyncSession, subject: Subject) -> str | None:
    for pp in subject.platform:
        first = await routines.get_first_routine_for_principal(
            session, tenant_id=pp.tenant_id, external_id=pp.external_id
        )
        if first is not None:
            return str(first.id)
    return None


async def _first_skill(session: AsyncSession, subject: Subject) -> str | None:
    for p in subject.principals:
        first = await user_skills.get_first_user_skill_for_principal(session, principal_id=p.id)
        if first is not None:
            return first.name
    return None


def _feedback_like(
    key: str, count: AccountFn, by_user_fn: AccountFn, by_account_fn: AccountFn
) -> Erasable:
    """Rows a person may own before having an account (account id NULL): the principal
    pass erases their own tenant's rows, the account pass whatever the account id or any
    of the account's user keys still reach. CLI principals cast none."""

    async def total(session: AsyncSession, subject: Subject) -> int:
        return await count(
            session, account_id=subject.account_id, platform_user_keys=subject.user_keys
        )

    async def one(session: AsyncSession, p: Principal) -> int:
        if not isinstance(p, PlatformPrincipalRow):
            return 0
        return await by_user_fn(session, tenant_id=p.tenant_id, platform_user_id=p.external_id)

    async def rest(session: AsyncSession, subject: Subject) -> int:
        return await by_account_fn(
            session, account_id=subject.account_id, platform_user_keys=subject.user_keys
        )

    return Erasable(key, count=total, principal=one, account=rest)


async def _count_tenant_contexts(session: AsyncSession, subject: Subject) -> int:
    fn = slack_turn_contexts.count_turn_contexts_for_account
    return sum(
        [await fn(session, tenant_id=t, account_id=subject.account_id) for t in subject.tenant_ids]
    )


async def _erase_tenant_contexts(session: AsyncSession, subject: Subject) -> int:
    fn = slack_turn_contexts.delete_turn_contexts_for_account
    return sum(
        [await fn(session, tenant_id=t, account_id=subject.account_id) for t in subject.tenant_ids]
    )


async def _count_links(session: AsyncSession, subject: Subject) -> int:
    # Distinct links: a link between two of the account's principals is deleted once.
    return await identity.count_principal_links_for_account(
        session,
        cli_principal_ids=[p.id for p in subject.cli],
        platform_principal_ids=[p.id for p in subject.platform],
    )


def _principal_row(kind: Literal["cli", "platform"]) -> ErasePrincipal:
    async def erase(session: AsyncSession, p: Principal) -> int:
        if kind_of(p) != kind:
            return 0
        return await identity.delete_for_principal(session, principal_id=p.id, kind=kind)

    return erase


async def _count_limits(session: AsyncSession, subject: Subject) -> int:
    return await support.count_person_limits_for_people(
        session, platform_user_keys=subject.user_keys
    )


async def _count_settled(session: AsyncSession, subject: Subject) -> int:
    keys = [(pp.platform, pp.external_id) for pp in subject.platform]
    return await escalations.count_settled_by(
        session, account_id=subject.account_id, settler_keys=keys
    )


async def _count_skills(session: AsyncSession, subject: Subject) -> int:
    fn = user_skills.count_user_skills_for_principal
    return sum([await fn(session, principal_id=p.id) for p in subject.principals])


async def _count_cards(session: AsyncSession, subject: Subject) -> int:
    # A gated call's card cascades with the account; the principal pass covers a principal
    # purged on its own.
    return await host_call_cards.count_for_account(session, account_id=subject.account_id)


ERASABLE: tuple[Erasable, ...] = (
    by_account(
        "gemini_records", (gemini.count_records_for_account, gemini.delete_records_for_account)
    ),
    by_account(
        "connector_tokens",
        (
            connectors.count_connector_tokens_for_account,
            connectors.delete_connector_tokens_for_account,
        ),
    ),
    by_account(
        "selfhosted_operations",
        (selfhosted.count_operations_for_account, selfhosted.delete_operations_for_account),
    ),
    # The pending codes that trade for a key count with the tokens they would make.
    by_account(
        "mcp_tokens",
        (key_claims.count_claims_for_account, key_claims.delete_claims_for_account),
        (mcp_tokens.count_tokens_for_account, mcp_tokens.delete_tokens_for_account),
    ),
    by_account(
        "github_connect_requests",
        (github_connect.count_requests_for_account, github_connect.delete_requests_for_account),
    ),
    by_account(
        "github_connect_click_intents",
        (
            github_connect.count_click_intents_for_account,
            github_connect.delete_click_intents_for_account,
        ),
    ),
    by_user(
        "lead_intakes",
        lambda s, pp: lead_intake.erase_actor(s, pp.tenant_id, pp.external_id),
        count=lambda s, pp: lead_intake.count_actor(s, pp.tenant_id, pp.external_id),
    ),
    by_user(
        "routines",
        _te(routines.delete_for_principal),
        count=_te(routines.count_routines_for_principal),
        example=_first_routine,
    ),
    by_requester(
        "credential_requests",
        credential_requests.count_credential_requests_for_platform_user,
        credential_requests.delete_credential_requests_for_platform_user,
    ),
    by_user(
        "secret_shares",
        _tpu(secret_shares.delete_shares_for_platform_user),
        count=_tpu(secret_shares.count_shares_for_platform_user),
    ),
    by_requester(
        "wizard_sessions",
        wizard_session.count_wizard_sessions_for_platform_user,
        wizard_session.delete_wizard_sessions_for_platform_user,
    ),
    # A vote and a help request (with their free text) can carry account_id = NULL.
    _feedback_like(
        "message_feedback",
        feedback.count_message_feedback_for_account,
        feedback.delete_message_feedback_for_platform_user,
        feedback.delete_message_feedback_for_account,
    ),
    _feedback_like(
        "support_escalations",
        escalations.count_support_escalations_for_account,
        escalations.delete_support_escalations_for_platform_user,
        escalations.delete_support_escalations_for_account,
    ),
    # A limit row is the shared ledger: the person leaves it, the charge stays.
    _feedback_like(
        "support_requests_anonymized",
        escalations.count_limit_requests_for_account,
        escalations.anonymize_limit_requests_for_platform_user,
        escalations.anonymize_limit_requests_for_account,
    ),
    by_user(
        "support_person_limits",
        lambda s, pp: support.delete_person_limits_for_people(
            s, platform_user_keys=[(pp.tenant_id, pp.external_id)]
        ),
        total=_count_limits,
    ),
    # A support post may sit in another tenant, so a settler matches in any.
    by_user(
        "support_settlers_cleared",
        lambda s, pp: escalations.clear_settler(s, settler_keys=[(pp.platform, pp.external_id)]),
        total=_count_settled,
        account=lambda s, sub: escalations.clear_settler(s, account_id=sub.account_id),
    ),
    by_user("daimon_admins", _erase_admin, count=_count_admin),
    # A turn's posts are the agent's: only the id of who asked leaves them.
    by_user(
        "agent_post_requesters",
        _tpu(agent_posts.clear_requester),
        count=_tpu(agent_posts.count_requester),
    ),
    by_user("host_call_cards", _tpu(host_call_cards.delete_for_platform_user), total=_count_cards),
    # An agent-chat session keeps its driver record, which names no one; where it came
    # from names its creator's account, so that is cleared and the session is not found.
    by_account(
        "agent_chat_session_links",
        (
            settle_state.count_agent_chat_provenance_for_account,
            settle_state.erase_agent_chat_provenance_for_account,
        ),
    ),
    Erasable(
        "user_skills",
        count=_count_skills,
        principal=lambda s, p: user_skills.delete_user_skills_for_principal(s, principal_id=p.id),
        example=_first_skill,
    ),
    by_user(
        "slack_user_tokens",
        lambda s, pp: _slack_token(s, pp, delete=True),
        "slack",
        count=lambda s, pp: _slack_token(s, pp, delete=False),
    ),
    by_user(
        "room_viewers",
        lambda s, pp: _room_viewer(s, pp, delete=True),
        "discord",
        count=lambda s, pp: _room_viewer(s, pp, delete=False),
    ),
    Erasable(
        "principal_links",
        count=_count_links,
        principal=lambda s, p: identity.delete_principal_links_for_principal(
            s, principal_id=p.id, kind=kind_of(p)
        ),
    ),
    Erasable("cli_principals", principal=_principal_row("cli")),
    Erasable("platform_principals", principal=_principal_row("platform")),
    # After the principal row: a racing name write locks that row, so it has either
    # committed (and goes here) or finds no principal. The caller drops the queue.
    by_user(
        "platform_user_names",
        _tpu(names.delete_user_names_for_platform_user),
        count=_tpu(names.count_user_names_for_platform_user),
    ),
    # Receipts lose the person and any unhandled words but stay as the duplicate guard.
    by_user(
        "teams_messages",
        _tu(teams_messages.forget_teams_messages_for_user),
        "teams",
        count=_tu(teams_messages.count_teams_messages_for_user),
    ),
    by_user(
        "microsoft_user_tokens",
        _tu(microsoft.delete_microsoft_user_token),
        "teams",
        count=_tu(microsoft.count_microsoft_user_tokens),
    ),
    Erasable("slack_turn_contexts", count=_count_tenant_contexts, account=_erase_tenant_contexts),
    by_account(
        "user_configs",
        (accounts.count_user_config_for_account, accounts.delete_user_config_for_account),
    ),
    by_account(
        "direct_message_conversations",
        (
            direct_messages.count_conversations_for_account,
            direct_messages.delete_conversations_for_account,
        ),
    ),
    by_imported_user(
        "dm_carry_overs", dm_carry_overs.count_for_users, dm_carry_overs.delete_for_users
    ),
    by_imported_user("held_timers", held_timers.count_for_users, held_timers.delete_for_users),
    by_account("tag_opt_outs", (tag_opt_outs.count_for_account, tag_opt_outs.delete_for_account)),
    # The person's agent picks, personal routing rules and listed-grant entries.
    by_account("dm_picks", (dm_access.count_picks_for_account, dm_access.delete_picks_for_account)),
    by_dm_user("dm_rules", dm_access.count_user_rules, dm_access.delete_user_rules),
    by_dm_user("dm_grant_users", dm_access.count_grants_listing, dm_access.scrub_user_from_grants),
    # Their identities, tokens and browser sessions cascade from the person.
    by_account("web_people", (web_people.count_for_account, web_people.delete_for_account)),
    by_account(
        "web_admin_grants",
        (web_roles.count_admin_grants_for_account, web_roles.delete_admin_grants_for_account),
    ),
    # What the person asked monitored agents, in every workspace; cascades too, but counted.
    by_account("monitored_events", (monitoring.count_for_account, monitoring.delete_for_account)),
    # The failures a person hit, kept for admins; the admin's own resolve or mute only
    # loses their name (ops_issue_states.by_account_id is SET NULL).
    by_account("ops_events", (ops_events.count_for_account, ops_events.delete_for_account)),
)
