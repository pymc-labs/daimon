"""Integration tests for `daimon.core.privacy.collect_purge_preview`.

Mirrors `tests/test_purge.py`: factories build account + principals + routines,
real Postgres via the per-test schema fixture. The critical drift-guard test
`test_collect_purge_preview_matches_purge_account_coverage_field_for_field`
fails CI if `PurgeReport` grows a category that `PurgePreview` does not mirror
(right-to-erasure undercount).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from daimon.core._models import (
    Account,
    AgentGithubBinding,
    Base,
    CliPrincipal,
    McpToken,
    MessageFeedback,
    PlatformPrincipal,
    Routine,
    UserConfig,
)
from daimon.core.credential_requests import mint_request_token
from daimon.core.privacy import (
    PurgePreview,
    PurgePreviewRow,
    collect_purge_preview,
)
from daimon.core.purge import PurgeReport, purge_account
from daimon.core.stores import agent_github_binding as agent_github_binding_store
from daimon.core.stores import credential_requests as credential_requests_store
from daimon.core.stores import github_credentials as github_credentials_store
from daimon.core.stores import github_oauth_states as github_oauth_states_store
from daimon.core.stores import mcp_tokens as mcp_tokens_store
from daimon.core.stores import message_feedback as message_feedback_store
from daimon.core.stores import routines as routines_store
from daimon.core.stores import slack_turn_contexts as slack_turn_contexts_store
from daimon.core.stores import slack_user_tokens as slack_user_tokens_store
from daimon.core.stores import user_skills as user_skills_store
from daimon.testing.factories import (
    link_principals,
    make_account,
    make_cli_principal,
    make_platform_principal,
    make_tenant,
    make_wizard_session,
)
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .factories.github import make_oauth_state


async def test_collect_purge_preview_returns_zero_counts_when_account_has_no_data(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()

    preview = await collect_purge_preview(sm=db_session_factory, account_id=account.id)

    assert preview.linked_principals.count == 0, "no principals seeded"
    assert preview.linked_principals.example is None, "example must be None when count==0"
    assert preview.principal_links.count == 0, "no links seeded"
    assert preview.principal_links.example is None
    assert preview.routines.count == 0, "no routines seeded"
    assert preview.routines.example is None
    assert preview.user_configs.count == 0, "no user_config seeded"
    assert preview.user_configs.example is None
    assert preview.account.count == 1, "account row exists -> account.count == 1"
    assert preview.account.example is None
    assert preview.user_skills.count == 0, "no user_skills seeded"
    assert preview.user_skills.example is None, "example must be None when count==0"
    assert preview.github_credentials.count == 0, "no github_credentials seeded"
    assert preview.github_credentials.example is None
    assert preview.github_oauth_states.count == 0, "no github_oauth_states seeded"
    assert preview.github_oauth_states.example is None


async def test_collect_purge_preview_account_count_is_zero_when_account_does_not_exist(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    preview = await collect_purge_preview(sm=db_session_factory, account_id=uuid.uuid4())

    assert preview.account.count == 0, "no account row -> account.count == 0"
    assert preview.linked_principals.count == 0
    assert preview.principal_links.count == 0
    assert preview.routines.count == 0
    assert preview.user_configs.count == 0


async def test_collect_purge_preview_counts_linked_principals_under_account(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session,
        platform="discord",
        external_id="1234",
        tenant=tenant,
        account=account,
    )
    await make_cli_principal(db_session, os_user="alice", tenant=tenant, account=account)
    await db_session.commit()

    preview = await collect_purge_preview(sm=db_session_factory, account_id=account.id)

    assert preview.linked_principals.count == 2, "one platform + one cli principal == 2"


async def test_collect_purge_preview_lists_principals_as_example_when_account_has_multiple_links(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session,
        platform="discord",
        external_id="1234",
        tenant=tenant,
        account=account,
    )
    await make_cli_principal(db_session, os_user="alice", tenant=tenant, account=account)
    await db_session.commit()

    preview = await collect_purge_preview(sm=db_session_factory, account_id=account.id)

    assert preview.linked_principals.example == "Discord:1234, CLI:os_user=alice", (
        "example must enumerate platform principals first, then CLI principals, comma-joined"
    )


async def test_collect_purge_preview_linked_principals_example_uses_platform_capitalization(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session,
        platform="discord",
        external_id="42",
        tenant=tenant,
        account=account,
    )
    await db_session.commit()

    preview = await collect_purge_preview(sm=db_session_factory, account_id=account.id)

    assert preview.linked_principals.example is not None
    assert preview.linked_principals.example.startswith("Discord:"), (
        "platform name must be capitalized in display"
    )


async def test_collect_purge_preview_counts_routines_for_platform_principal(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session,
        platform="discord",
        external_id="user-r",
        tenant=tenant,
        account=account,
    )
    await routines_store.create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="user-r",
        agent_id="a1",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="m1",
    )
    await routines_store.create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="user-r",
        agent_id="a1",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="m2",
    )
    await db_session.commit()

    preview = await collect_purge_preview(sm=db_session_factory, account_id=account.id)

    assert preview.routines.count == 2, "both routines under the principal must be counted"
    assert preview.routines.example is not None, "example must be populated when count > 0"


async def test_collect_purge_preview_counts_routines_across_multiple_platform_principals(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session,
        platform="discord",
        external_id="user-a",
        tenant=tenant,
        account=account,
    )
    await make_platform_principal(
        db_session,
        platform="discord",
        external_id="user-b",
        tenant=tenant,
        account=account,
    )
    await routines_store.create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="user-a",
        agent_id="a1",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="ma",
    )
    await routines_store.create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="user-b",
        agent_id="a1",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="mb",
    )
    await db_session.commit()

    preview = await collect_purge_preview(sm=db_session_factory, account_id=account.id)

    assert preview.routines.count == 2, "routines under each platform principal must be summed"


async def test_collect_purge_preview_returns_pure_read_does_not_mutate_db(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    pp = await make_platform_principal(
        db_session,
        platform="discord",
        external_id="user-pure",
        tenant=tenant,
        account=account,
    )
    cli = await make_cli_principal(db_session, os_user="cli-pure", tenant=tenant, account=account)
    await link_principals(db_session, cli=cli, platform=pp)
    await routines_store.create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="user-pure",
        agent_id="a1",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="m",
    )
    db_session.add(UserConfig(account_id=account.id, agent_name="a", environment_name="e"))
    await db_session.flush()
    await db_session.commit()

    accounts_before = (
        await db_session.execute(select(func.count()).select_from(Account))
    ).scalar_one()
    pp_before = (
        await db_session.execute(select(func.count()).select_from(PlatformPrincipal))
    ).scalar_one()
    cli_before = (
        await db_session.execute(select(func.count()).select_from(CliPrincipal))
    ).scalar_one()
    routines_before = (
        await db_session.execute(select(func.count()).select_from(Routine))
    ).scalar_one()

    await collect_purge_preview(sm=db_session_factory, account_id=account.id)

    accounts_after = (
        await db_session.execute(select(func.count()).select_from(Account))
    ).scalar_one()
    pp_after = (
        await db_session.execute(select(func.count()).select_from(PlatformPrincipal))
    ).scalar_one()
    cli_after = (
        await db_session.execute(select(func.count()).select_from(CliPrincipal))
    ).scalar_one()
    routines_after = (
        await db_session.execute(select(func.count()).select_from(Routine))
    ).scalar_one()

    assert accounts_before == accounts_after, "collect_purge_preview must not touch accounts"
    assert pp_before == pp_after, "collect_purge_preview must not touch platform_principals"
    assert cli_before == cli_after, "collect_purge_preview must not touch cli_principals"
    assert routines_before == routines_after, "collect_purge_preview must not touch routines"


async def test_collect_purge_preview_cli_oauth_states_excludes_same_os_user_in_other_tenant(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Preview mirrors the tenant-scoped CLI oauth-state predicate (parity with purge).

    A same-os_user handshake row belonging to a different tenant (a different
    person) must not be counted in this account's preview.
    """
    tenant_a = await make_tenant(db_session, workspace_id="pv-osuser-a")
    tenant_b = await make_tenant(db_session, workspace_id="pv-osuser-b")
    account = await make_account(db_session, tenant=tenant_a)
    await make_cli_principal(db_session, os_user="ubuntu", tenant=tenant_a, account=account)
    await make_oauth_state(
        db_session,
        platform="cli",
        platform_user_id="ubuntu",
        scopes=("repo",),
        tenant_id=tenant_a.id,
    )
    # A DIFFERENT person's row: same os_user, other tenant.
    await make_oauth_state(
        db_session,
        platform="cli",
        platform_user_id="ubuntu",
        scopes=("repo",),
        tenant_id=tenant_b.id,
    )
    await db_session.commit()

    preview = await collect_purge_preview(sm=db_session_factory, account_id=account.id)
    report = await purge_account(sm=db_session_factory, account_id=account.id)

    assert preview.github_oauth_states.count == 1, (
        "preview must count only the account's own tenant-scoped CLI handshake row"
    )
    assert preview.github_oauth_states.count == report.db.github_oauth_states, (
        "preview and purge must agree on the tenant-scoped CLI oauth-state predicate"
    )


async def test_preview_oauth_state_count_matches_the_tenant_scoped_purge(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Preview mirrors the tenant-scoped platform-principal oauth-state predicate.

    A folded rider: the same platform external_id can belong to two different
    people in two different tenants (a Discord user id is workspace-scoped).
    The preview must count only the account's own tenant's handshake row, not
    the cross-tenant total — otherwise a user is told their data will be
    deleted when it will not.
    """
    tenant_a = await make_tenant(db_session, workspace_id="pv-oauth-tenant-a")
    tenant_b = await make_tenant(db_session, workspace_id="pv-oauth-tenant-b")
    account = await make_account(db_session, tenant=tenant_a)
    await make_platform_principal(
        db_session,
        platform="discord",
        external_id="SHARED_EXTERNAL_ID",
        tenant=tenant_a,
        account=account,
    )
    # A DIFFERENT person: same external_id, other tenant (the folded rider).
    await make_platform_principal(
        db_session,
        platform="discord",
        external_id="SHARED_EXTERNAL_ID",
        tenant=tenant_b,
    )
    await make_oauth_state(
        db_session,
        platform="discord",
        platform_user_id="SHARED_EXTERNAL_ID",
        scopes=("repo",),
        tenant_id=tenant_a.id,
    )
    await make_oauth_state(
        db_session,
        platform="discord",
        platform_user_id="SHARED_EXTERNAL_ID",
        scopes=("repo",),
        tenant_id=tenant_b.id,
    )
    await db_session.commit()

    preview = await collect_purge_preview(sm=db_session_factory, account_id=account.id)
    report = await purge_account(sm=db_session_factory, account_id=account.id)

    assert preview.github_oauth_states.count == 1, (
        "preview must count only the account's own tenant-scoped platform handshake row, "
        "not the cross-tenant total for the colliding external_id"
    )
    assert preview.github_oauth_states.count == report.db.github_oauth_states, (
        "preview and purge must agree on the tenant-scoped platform oauth-state predicate"
    )

    surviving = await github_oauth_states_store.count_states_for_platform_user(
        db_session, platform="discord", platform_user_id="SHARED_EXTERNAL_ID", tenant_id=tenant_b.id
    )
    assert surviving == 1, "tenant_b's identically-external-id row must survive the purge"


async def test_collect_purge_preview_counts_slack_user_tokens_and_turn_contexts(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Preview counts slack_user_tokens (Slack principal) + slack_turn_contexts
    (account-scoped) and agrees with purge_account's actual deletion counts."""
    from datetime import UTC, datetime

    tenant = await make_tenant(db_session, platform="slack", workspace_id="PV_SLACK_01")
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session,
        platform="slack",
        external_id="U_PV_TARGET",
        tenant=tenant,
        account=account,
    )
    await slack_user_tokens_store.upsert_slack_user_token(
        db_session,
        team_id="PV_SLACK_01",
        slack_user_id="U_PV_TARGET",
        encrypted_token=b"tok",
        scopes="identity.basic",
    )
    await slack_turn_contexts_store.create_slack_turn_context(
        db_session,
        tenant_id=tenant.id,
        account_id=account.id,
        channel_id="C1",
        thread_ts="1.1",
        started_at=datetime.now(tz=UTC),
    )
    await db_session.commit()

    preview = await collect_purge_preview(sm=db_session_factory, account_id=account.id)
    report = await purge_account(sm=db_session_factory, account_id=account.id)

    assert preview.slack_user_tokens.count == 1, "must count the Slack principal's token row"
    assert preview.slack_user_tokens.count == report.db.slack_user_tokens, (
        "preview and purge must agree on slack_user_tokens"
    )
    assert preview.slack_turn_contexts.count == 1, "must count the account's turn-context row"
    assert preview.slack_turn_contexts.count == report.db.slack_turn_contexts, (
        "preview and purge must agree on slack_turn_contexts"
    )


async def test_collect_purge_preview_credential_requests_matches_purge_account(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Preview counts credential_requests and agrees with purge_account's
    actual deletion count — the right-to-erasure parity contract."""
    tenant = await make_tenant(db_session, workspace_id="pv-cred-req")
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session,
        platform="discord",
        external_id="PV_CRED_TARGET",
        tenant=tenant,
        account=account,
    )
    await credential_requests_store.create_credential_request(
        db_session,
        token=mint_request_token(),
        kind="env",
        tenant_id=tenant.id,
        agent_id=uuid.uuid4(),
        account_id=account.id,
        target="OPENAI_API_KEY",
        mcp_server_url=None,
        requester_platform_user_id="PV_CRED_TARGET",
        channel_id="C1",
        expires_at=datetime.now(tz=UTC) + timedelta(minutes=30),
    )
    await db_session.commit()

    preview = await collect_purge_preview(sm=db_session_factory, account_id=account.id)
    report = await purge_account(sm=db_session_factory, account_id=account.id)

    assert preview.credential_requests.count == 1, (
        "must count the platform principal's credential-request row"
    )
    assert preview.credential_requests.count == report.db.credential_requests, (
        "preview and purge must agree on credential_requests"
    )


async def test_collect_purge_preview_wizard_sessions_matches_purge_account(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Preview counts wizard_sessions and agrees with purge_account's actual
    deletion count — the right-to-erasure parity contract."""
    tenant = await make_tenant(db_session, workspace_id="pv-wizard")
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session,
        platform="discord",
        external_id="PV_WIZARD_TARGET",
        tenant=tenant,
        account=account,
    )
    await make_wizard_session(
        db_session,
        tenant=tenant,
        account=account,
        requester_platform_user_id="PV_WIZARD_TARGET",
    )
    await db_session.commit()

    preview = await collect_purge_preview(sm=db_session_factory, account_id=account.id)
    report = await purge_account(sm=db_session_factory, account_id=account.id)

    assert preview.wizard_sessions.count == 1, (
        "must count the platform principal's wizard_session row"
    )
    assert preview.wizard_sessions.count == report.db.wizard_sessions, (
        "preview and purge must agree on wizard_sessions"
    )


async def test_purge_erases_submitted_and_abandoned_wizard_sessions(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Erasure must not apply a lifecycle filter: open, submitted, and
    abandoned wizard_session rows all carry the user's typed answers and must
    all be gone after purge_account."""
    tenant = await make_tenant(db_session, workspace_id="pv-wizard-lifecycle")
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session,
        platform="discord",
        external_id="PV_WIZARD_LIFECYCLE",
        tenant=tenant,
        account=account,
    )
    for status in ("open", "submitted", "abandoned"):
        await make_wizard_session(
            db_session,
            tenant=tenant,
            account=account,
            requester_platform_user_id="PV_WIZARD_LIFECYCLE",
            status=status,
        )
    await db_session.commit()

    report = await purge_account(sm=db_session_factory, account_id=account.id)

    assert report.db.wizard_sessions == 3, (
        "purge must delete open, submitted, AND abandoned wizard_session rows"
    )


async def test_collect_purge_preview_message_feedback_matches_purge_account_including_null_account(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Preview counts message_feedback and agrees with purge_account's actual
    deletion count — including the null-account-id vote, the whole point of
    this test."""
    tenant = await make_tenant(db_session, workspace_id="pv-msg-feedback")
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session,
        platform="discord",
        external_id="PV_FEEDBACK_TARGET",
        tenant=tenant,
        account=account,
    )
    await message_feedback_store.record_vote(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        message_id="pv-msg-1",
        channel_id="C1",
        platform_user_id="PV_FEEDBACK_TARGET",
        account_id=account.id,
        ma_session_id=None,
        vote="up",
    )
    await message_feedback_store.record_vote(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        message_id="pv-msg-2",
        channel_id="C1",
        platform_user_id="PV_FEEDBACK_TARGET",
        account_id=None,
        ma_session_id=None,
        vote="down",
    )
    await db_session.commit()

    preview = await collect_purge_preview(sm=db_session_factory, account_id=account.id)
    report = await purge_account(sm=db_session_factory, account_id=account.id)

    assert preview.message_feedback.count == 2, (
        "preview must count both the account-keyed vote and the null-account "
        "vote reachable via the platform-user key"
    )
    assert preview.message_feedback.count == report.db.message_feedback, (
        "preview and purge must agree on message_feedback"
    )

    remaining = (
        await db_session.execute(
            select(func.count())
            .select_from(MessageFeedback)
            .where(MessageFeedback.tenant_id == tenant.id)
        )
    ).scalar_one()
    assert remaining == 0, (
        "purge must erase every message_feedback row for this account, including "
        "the one whose account_id was NULL at vote time"
    )


async def test_a_vote_in_a_tenant_without_a_principal_is_erased_by_a_purge_run_there(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Pins the documented remediation for the one erasure residue class.

    A bystander's vote never mints a principal, so a person with an account in
    guild A who reacts in guild B leaves a row attributable only by
    (tenant_B, user) — out of reach of an erasure invoked from guild A. The
    carve-out in `daimon.core.purge`'s docstring says the remedy is to run the
    purge inside guild B, which mints a principal there. This test is what
    keeps that promise honest.
    """
    tenant_a = await make_tenant(db_session, workspace_id="pv-residue-guild-a")
    tenant_b = await make_tenant(db_session, workspace_id="pv-residue-guild-b")
    account = await make_account(db_session, tenant=tenant_a)
    await make_platform_principal(
        db_session,
        platform="discord",
        external_id="PV_RESIDUE_USER",
        tenant=tenant_a,
        account=account,
    )
    await message_feedback_store.record_vote(
        db_session,
        tenant_id=tenant_b.id,
        platform="discord",
        message_id="pv-residue-1",
        channel_id="C1",
        platform_user_id="PV_RESIDUE_USER",
        account_id=None,
        ma_session_id=None,
        vote="down",
    )
    await db_session.commit()

    from_guild_a = await purge_account(sm=db_session_factory, account_id=account.id)

    assert from_guild_a.db.message_feedback == 0, (
        "a purge invoked from guild A cannot reach a guild-B vote with no principal there"
    )

    # The documented remediation: taking any action in guild B that mints a
    # principal brings (tenant_B, user) into the key set.
    remediation_account = await make_account(db_session, tenant=tenant_b)
    await make_platform_principal(
        db_session,
        platform="discord",
        external_id="PV_RESIDUE_USER",
        tenant=tenant_b,
        account=remediation_account,
    )
    await db_session.commit()

    preview = await collect_purge_preview(sm=db_session_factory, account_id=remediation_account.id)
    from_guild_b = await purge_account(sm=db_session_factory, account_id=remediation_account.id)

    assert preview.message_feedback.count == 1, (
        "the guild-local preview must show the residual vote the person can now erase"
    )
    assert from_guild_b.db.message_feedback == 1, "the guild-local purge must erase it"
    remaining = (
        await db_session.execute(
            select(func.count())
            .select_from(MessageFeedback)
            .where(MessageFeedback.tenant_id == tenant_b.id)
        )
    ).scalar_one()
    assert remaining == 0, "no residue may survive the documented remediation path"


async def test_collect_purge_preview_matches_purge_account_coverage_field_for_field() -> None:
    """If `PurgeReport` grows a new int field, `PurgePreview` MUST mirror it.

    The mapping documents the intentional folding of `cli_principals` +
    `platform_principals` into `linked_principals`. Any NEW `PurgeReport` field
    without a corresponding `PurgePreview` row breaks this assertion — which is
    the whole point: undercounting the cascade preview violates the right-to-erasure
    contract the /privacy surface relies on.
    """
    preview_fields = set(PurgePreview.model_fields.keys())
    report_fields = set(PurgeReport.model_fields.keys())

    # Mapping: PurgeReport field name -> PurgePreview field name.
    mapping: dict[str, str] = {
        "cli_principals": "linked_principals",
        "platform_principals": "linked_principals",
        "principal_links": "principal_links",
        "routines": "routines",
        "user_configs": "user_configs",
        "accounts": "account",
        "user_skills": "user_skills",
        "github_credentials": "github_credentials",
        "github_oauth_states": "github_oauth_states",
        "mcp_tokens": "mcp_tokens",
        "agent_github_binding": "agent_github_binding",
        "slack_user_tokens": "slack_user_tokens",
        "slack_turn_contexts": "slack_turn_contexts",
        "credential_requests": "credential_requests",
        "wizard_sessions": "wizard_sessions",
        "message_feedback": "message_feedback",
        "support_escalations": "support_escalations",
    }

    uncovered = report_fields - set(mapping.keys())
    assert not uncovered, (
        f"PurgeReport has new field(s) {uncovered} not mapped to PurgePreview. "
        f"Update daimon.core.privacy.PurgePreview (and collect_purge_preview) "
        f"to mirror them — preview would otherwise undercount the cascade."
    )

    targets = set(mapping.values())
    missing_targets = targets - preview_fields
    assert not missing_targets, (
        f"PurgePreview is missing target fields {missing_targets} that the mapping "
        f"declares — fix the mapping or add the fields."
    )

    # PurgePreviewRow shape sanity — every preview field must be a PurgePreviewRow.
    for name in preview_fields:
        annotation = PurgePreview.model_fields[name].annotation
        assert annotation is PurgePreviewRow, (
            f"PurgePreview.{name} must be a PurgePreviewRow, got {annotation}"
        )


async def test_purge_covers_every_account_or_principal_scoped_table() -> None:
    """Schema-reflecting drift guard: any table an account purge could reach must
    be deleted by the orchestrator or on the documented carve-out allowlist.

    Replaces a field-parity-only guard that was structurally blind to a table the
    purge never knew about — exactly how mcp_tokens slipped through. The reflected
    schema is the source of truth; PURGED and ALLOWLIST are the human dispositions.
    A table is in scope if it has an FK to accounts.id OR a principal_id column.
    """
    # Tables the orchestrator deletes, keyed by why they are in scope.
    purged: dict[str, str] = {
        "cli_principals": "account_id FK -> accounts.id",
        "platform_principals": "account_id FK -> accounts.id",
        "user_config": "account_id PK/FK -> accounts.id",
        "user_skills": "principal_id",
        "github_credentials": "principal_id",
        "agent_github_binding": "principal_id",
        "mcp_tokens": "account_id FK -> accounts.id",
        "wizard_session": "account_id FK -> accounts.id",
        "message_feedback": "account_id FK -> accounts.id",
        "support_escalations": "account_id FK -> accounts.id",
    }
    # Intentional exclusions, each justified inline.
    allowlist: frozenset[str] = frozenset(
        {
            # Tenant-scoped (PK tenant_id); the only accounts.id FK is the nullable
            # provenance column agent_name_set_by_account_id with ON DELETE SET NULL,
            # so an account purge auto-severs the link without deleting the
            # tenant-shared config row (deleting it would erase co-tenants' data).
            # Not an erasure gap.
            "channel_config",
            "tenant_config",
            # Same shape: PK is (tenant_id, agent_id) and the only accounts.id FK is
            # the nullable provenance column proof_account_id with ON DELETE SET NULL.
            # An account purge severs who-proved-it while leaving the binding and its
            # proof kind/timestamp intact — deleting the row would drop a whole
            # tenant's repo mount because one member erased their account.
            "agent_repo_binding",
            # Tenant/agent-scoped, no account/principal column — "purge account X"
            # is undefined for them; deferred to a future tenant-purge path.
            "agent_files",
            "agent_google_binding",
            "thread_sessions",
            # Billing carve-outs retained for integrity.
            "usage_events",
            "tenant_user_caps",
        }
    )

    in_scope: dict[str, str] = {}
    for name, table in Base.metadata.tables.items():
        reasons: list[str] = []
        if any(fk.target_fullname == "accounts.id" for fk in table.foreign_keys):
            reasons.append("FK->accounts.id")
        if "principal_id" in table.columns:
            reasons.append("principal_id column")
        if reasons:
            in_scope[name] = ", ".join(reasons)

    uncovered = {
        name: why for name, why in in_scope.items() if name not in purged and name not in allowlist
    }
    assert not uncovered, (
        f"Account/principal-scoped table(s) {sorted(uncovered)} are neither purged by "
        f"daimon.core.purge nor on the documented carve-out allowlist. Wire a delete "
        f"into purge.py (+ PurgeReport/PurgePreview) or add the table to the allowlist "
        f"in this test with a justification. In-scope because: {uncovered}"
    )


async def test_collect_purge_preview_end_to_end_matches_purge_account_counts(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Runtime parity: preview counts must equal what purge_account actually deletes."""
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    pp = await make_platform_principal(
        db_session,
        platform="discord",
        external_id="user-e2e",
        tenant=tenant,
        account=account,
    )
    cli = await make_cli_principal(db_session, os_user="cli-e2e", tenant=tenant, account=account)
    await link_principals(db_session, cli=cli, platform=pp)
    await routines_store.create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id="user-e2e",
        agent_id="a1",
        agent_name="daimon",
        cron_expr="* * * * *",
        timezone_="UTC",
        trigger_message="m",
    )
    db_session.add(UserConfig(account_id=account.id, agent_name="a", environment_name="e"))
    # Seed new tables for both principals.
    await user_skills_store.upsert_user_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=pp.id,
        agent_name="dev",
        name="pp-skill",
        source_repo_url="https://github.com/o/r",
        source_repo_branch="main",
        source_path="",
        content_hash="h1",
        anthropic_id=None,
        anthropic_latest_version=None,
    )
    await user_skills_store.upsert_user_skill(
        db_session,
        tenant_id=tenant.id,
        principal_id=cli.id,
        agent_name="dev",
        name="cli-skill",
        source_repo_url="https://github.com/o/r",
        source_repo_branch="main",
        source_path="",
        content_hash="h2",
        anthropic_id=None,
        anthropic_latest_version=None,
    )
    await github_credentials_store.upsert_credential(
        db_session,
        principal_id=pp.id,
        github_login="e2e-gh",
        encrypted_token=b"tok",
        scopes=("repo",),
    )
    await make_oauth_state(
        db_session,
        platform="discord",
        platform_user_id="user-e2e",
        scopes=("repo",),
        tenant_id=tenant.id,
    )
    await make_oauth_state(
        db_session,
        platform="cli",
        platform_user_id="cli-e2e",
        scopes=("repo",),
        tenant_id=tenant.id,
    )
    # A per-agent MCP token (account-scoped) — the row that crashes
    # purge today — plus an agent_github_binding (principal-scoped).
    await mcp_tokens_store.create_mcp_token_row(
        db_session,
        jti=uuid.uuid4(),
        account_id=account.id,
        tenant_id=tenant.id,
        agent_id="a1",
        label=None,
        created_at=datetime.now(tz=UTC),
    )
    await agent_github_binding_store.set_agent_github_binding(
        db_session,
        agent_id=uuid.uuid4(),
        principal_id=pp.id,
    )
    await db_session.flush()
    await db_session.commit()

    preview = await collect_purge_preview(sm=db_session_factory, account_id=account.id)
    report = await purge_account(sm=db_session_factory, account_id=account.id)

    assert (
        preview.linked_principals.count == report.db.cli_principals + report.db.platform_principals
    ), "linked_principals.count must equal cli + platform principal deletion totals"
    assert preview.principal_links.count == report.db.principal_links, (
        "principal_links.count must equal report.principal_links"
    )
    assert preview.routines.count == report.db.routines, "routines.count must equal report.routines"
    assert preview.user_configs.count == report.db.user_configs, (
        "user_configs.count must equal report.user_configs"
    )
    assert preview.account.count == report.db.accounts, "account.count must equal report.accounts"
    assert preview.user_skills.count == report.db.user_skills, (
        "user_skills.count must equal report.db.user_skills"
    )
    assert preview.github_credentials.count == report.db.github_credentials, (
        "github_credentials.count must equal report.db.github_credentials"
    )
    assert preview.github_oauth_states.count == report.db.github_oauth_states, (
        "github_oauth_states.count must equal report.db.github_oauth_states"
    )
    assert preview.mcp_tokens.count == report.db.mcp_tokens, (
        "mcp_tokens.count must equal report.db.mcp_tokens"
    )
    assert preview.agent_github_binding.count == report.db.agent_github_binding, (
        "agent_github_binding.count must equal report.db.agent_github_binding"
    )

    # The crash fix + erasure invariant: purge_account returned without a
    # foreign-key error, and zero rows remain in the two new tables. Read on a
    # fresh session — the purge used (and closed) its own.
    async with db_session_factory() as verify_session:
        mcp_residual = await verify_session.scalar(
            select(func.count()).select_from(McpToken).where(McpToken.account_id == account.id)
        )
        binding_residual = await verify_session.scalar(
            select(func.count())
            .select_from(AgentGithubBinding)
            .where(AgentGithubBinding.principal_id == pp.id)
        )
    assert mcp_residual == 0, (
        "purge must hard-delete every mcp_tokens row for the account (crash fix, PURGE-01)"
    )
    assert binding_residual == 0, (
        "purge must hard-delete every agent_github_binding row for the principal (PURGE-02)"
    )
