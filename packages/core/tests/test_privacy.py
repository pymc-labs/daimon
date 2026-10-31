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
    AccountGitHubLink,
    Base,
    CliPrincipal,
    GitHubIssuedToken,
    GitHubUserLink,
    MessageFeedback,
    PlatformPrincipal,
    Routine,
    UserConfig,
)
from daimon.core.channel_environments import (
    build_limited_channels_confirm,
    build_limited_network_confirm,
)
from daimon.core.credential_requests import mint_request_token
from daimon.core.privacy import (
    DELETE_PAUSED,
    PurgePreview,
    PurgePreviewRow,
    collect_purge_preview,
    summary_line,
)
from daimon.core.purge import PurgeReport, purge_account
from daimon.core.stores import credential_requests as credential_requests_store
from daimon.core.stores import github_connect as github_connect_store
from daimon.core.stores import github_issued_tokens as github_issued_tokens_store
from daimon.core.stores import message_feedback as message_feedback_store
from daimon.core.stores import microsoft_user_tokens as microsoft_tokens_store
from daimon.core.stores import routines as routines_store
from daimon.core.stores import slack_turn_contexts as slack_turn_contexts_store
from daimon.core.stores import slack_user_tokens as slack_user_tokens_store
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


def test_plain_privacy_summary_plural_and_pause_spacing() -> None:
    empty = PurgePreviewRow(count=0, example=None)
    values = {name: empty for name in PurgePreview.model_fields}
    values.update(
        linked_principals=PurgePreviewRow(count=1, example="user:maria"),
        routines=PurgePreviewRow(count=2, example="123"),
        user_configs=PurgePreviewRow(count=1, example=None),
    )
    preview = PurgePreview(**values)
    assert summary_line(preview) == "1 linked account, 2 routines, saved settings"
    assert (
        summary_line(preview.model_copy(update={"watches": PurgePreviewRow(count=2, example=None)}))
        == "1 linked account, 2 routines, 2 page watches, saved settings"
    )
    assert summary_line(PurgePreview(**{name: empty for name in values})) == "nothing listed here"
    assert DELETE_PAUSED == (
        "Deleting your account is paused during the event.\n\n"
        "Ask the team running Daimon if you need it deleted now."
    )


def test_restricted_network_confirm_names_selected_channel_and_spaced_action() -> None:
    assert build_limited_network_confirm(
        environment_name="Acme", panel=True, channel="#analytics"
    ) == (
        "Nothing changed. This channel or one of its threads limits who can read it, and "
        "the Acme environment "
        "could let its content out.\n\n"
        "Ask me in chat to make this change for #analytics, then confirm when I ask."
    )
    assert build_limited_network_confirm(
        environment_name=None, panel=True, channel="#analytics"
    ).endswith(
        "Ask me in chat to use the default environment for #analytics, then confirm when I ask."
    )
    assert build_limited_channels_confirm(environment_name=None) == (
        "Nothing changed. Some channels or their threads limit who can read them, and "
        "the default environment "
        "could let their content out."
    )


async def test_account_purge_deletes_github_user_after_last_link(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    first = await make_account(db_session, tenant=tenant)
    second = await make_account(db_session, tenant=tenant)
    db_session.add(
        GitHubUserLink(
            github_user_id=101,
            login="example",
            encrypted_access_token=b"ciphertext",
            access_expires_at=datetime.now(UTC) + timedelta(hours=1),
            status="active",
            token_generation=1,
            link_generation=1,
        )
    )
    await db_session.flush()
    for account in (first, second):
        db_session.add(
            AccountGitHubLink(
                account_id=account.id,
                github_user_id=101,
                platform="discord",
                platform_user_id=str(account.id),
                verified_via="discord_oauth",
            )
        )
    token_ids: list[uuid.UUID] = []
    for account in (first, second):
        token = await github_issued_tokens_store.create_pending(
            db_session,
            tenant_id=tenant.id,
            agent_id=uuid.uuid4(),
            session_id="privacy-test",
            installation_id=77,
            repo_ids=[101],
            permissions={"contents": "read"},
            grant_versions={"grant:101": 1, "authorization:101": 1},
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            requester_account_id=account.id,
            github_user_id=101,
            link_generation=1,
        )
        token_ids.append(token.token_id)
    await db_session.commit()

    first_preview = await collect_purge_preview(sm=db_session_factory, account_id=first.id)
    assert first_preview.github_user_links.count == 0
    first_report = await purge_account(sm=db_session_factory, account_id=first.id)
    assert first_report.db.github_user_links == 0
    async with db_session_factory() as session:
        first_token = await session.get(GitHubIssuedToken, token_ids[0])
        second_token = await session.get(GitHubIssuedToken, token_ids[1])
        assert first_token is not None
        assert first_token.requester_account_id is None and first_token.github_user_id is None
        assert second_token is not None
        assert second_token.requester_account_id == second.id and second_token.github_user_id == 101

    second_preview = await collect_purge_preview(sm=db_session_factory, account_id=second.id)
    assert second_preview.github_user_links.count == 1
    second_report = await purge_account(sm=db_session_factory, account_id=second.id)
    assert second_report.db.github_user_links == 1
    async with db_session_factory() as session:
        assert await session.get(GitHubUserLink, 101) is None


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
    assert preview.github_connect_requests.count == 0


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

    assert preview.linked_principals.example == "1234, CLI:os_user=alice", (
        "example must enumerate platform principals first, then CLI principals, comma-joined"
    )


async def test_collect_purge_preview_linked_principals_example_names_no_chat_platform(
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

    assert preview.linked_principals.example == "42", (
        "the panel must not name a chat platform: it may be another one than the viewer's"
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


async def test_preview_and_purge_agree_on_microsoft_user_tokens(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from datetime import UTC, datetime

    tenant = await make_tenant(db_session, platform="teams", workspace_id="PV_TEAMS_MS")
    account = await make_account(db_session, tenant=tenant)
    oid = "00000000-0000-0000-0000-00000000c0de"
    await make_platform_principal(
        db_session, platform="teams", external_id=oid, tenant=tenant, account=account
    )
    await microsoft_tokens_store.upsert_microsoft_user_token(
        db_session,
        tenant_id=tenant.id,
        user_id=oid,
        encrypted_refresh_token=b"ct",
        scopes="User.Read",
        now=datetime.now(tz=UTC),
    )
    await db_session.commit()

    preview = await collect_purge_preview(sm=db_session_factory, account_id=account.id)
    assert preview.microsoft_user_tokens.count == 1, "must count the Teams person's connection"
    assert "1 saved Microsoft account" in summary_line(preview), "the privacy card must list it"
    report = await purge_account(sm=db_session_factory, account_id=account.id)
    assert report.db.microsoft_user_tokens == 1, "purge must delete the connection it previewed"
    async with db_session_factory() as session:
        left = await microsoft_tokens_store.count_microsoft_user_tokens(
            session, tenant_id=tenant.id, user_id=oid
        )
    assert left == 0, "no refresh token may survive the purge"


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
        idempotency_key=uuid.uuid4(),
        target_ma_agent_id="ag_test",
        target_name="tester",
        requested_work=None,
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


async def test_github_connect_request_is_previewed_and_purged(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await github_connect_store.record_connect_request(
        db_session,
        tenant_id=tenant.id,
        requester_account_id=account.id,
        agent_id=uuid.uuid4(),
        agent_name="ResearchBot",
    )
    await github_connect_store.create_discord_connect_intent(
        db_session,
        tenant_id=tenant.id,
        requester_account_id=account.id,
        requester_platform_user_id="123",
        agent_id=uuid.uuid4(),
        agent_name="ResearchBot",
        parent_channel_id="456",
        thread_id="789",
        origin_ma_agent_id="agent_1",
        origin_responder_name="ResearchBot",
        requested_work=None,
    )
    await db_session.commit()

    preview = await collect_purge_preview(sm=db_session_factory, account_id=account.id)
    assert preview.github_connect_requests.count == 1
    assert preview.github_connect_click_intents.count == 1
    report = await purge_account(sm=db_session_factory, account_id=account.id)
    assert report.db.github_connect_requests == 1
    assert report.db.github_connect_click_intents == 1


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
        "lead_intakes": "lead_intakes",
        "routines": "routines",
        "watches": "watches",
        "user_configs": "user_configs",
        "accounts": "account",
        "user_skills": "user_skills",
        "github_connect_requests": "github_connect_requests",
        "github_connect_click_intents": "github_connect_click_intents",
        "github_user_links": "github_user_links",
        "mcp_tokens": "mcp_tokens",
        "gemini_records": "gemini_records",
        "selfhosted_operations": "selfhosted_operations",
        "slack_user_tokens": "slack_user_tokens",
        "slack_turn_contexts": "slack_turn_contexts",
        "direct_message_conversations": "direct_message_conversations",
        "credential_requests": "credential_requests",
        "wizard_sessions": "wizard_sessions",
        "message_feedback": "message_feedback",
        "support_escalations": "support_escalations",
        "daimon_admins": "daimon_admins",
        "channel_admins": "channel_admins",
        "agent_post_requesters": "agent_post_requesters",
        "platform_user_names": "platform_user_names",
        "teams_messages": "teams_messages",
        "microsoft_user_tokens": "microsoft_user_tokens",
        "mux_slots": "mux_slots",
        "mux_binding_generations": "mux_binding_generations",
        "mux_journal_sessions": "mux_journal_sessions",
        "mux_journal_events": "mux_journal_events",
        "mux_leases": "mux_leases",
        "mux_release_requests": "mux_release_requests",
        "mux_operations": "mux_operations",
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
        "watches": "owner_account_id FK; explicit erasure also deletes evidence and pending wakes",
        "watch_notice_receipts": "owner_account_id FK cascade; watches.delete_for_account deletes receipts",
        "watch_credentials": "owner_account_id FK cascade; watches.delete_for_account deletes keys",
        "selfhosted_resource_operations": "account UUID stored as account_id text",
        "long_job": "accounts.id FK cascade; cancellation audit cascades from the job",
        "neutral_agent_revisions": "accounts.id FK cascade",
        "neutral_skill_versions": "accounts.id FK cascade",
        "cli_principals": "account_id FK -> accounts.id",
        "platform_principals": "account_id FK -> accounts.id",
        "user_config": "account_id PK/FK -> accounts.id",
        "user_skills": "principal_id",
        "github_connect_requests": "requester_account_id FK -> accounts.id",
        "github_connect_click_intents": "requester_account_id FK -> accounts.id",
        "mcp_tokens": "account_id FK -> accounts.id",
        "wizard_session": "account_id FK -> accounts.id",
        "message_feedback": "account_id FK -> accounts.id",
        "support_escalations": "account_id FK -> accounts.id",
        "direct_message_conversations": "account_id FK -> accounts.id",
        # The person's user id leaves every grant; updated_by is SET NULL.
        "daimon_admins": "subject_id and granted_by are erased by platform principal",
        "channel_admins": "user_ids + updated_by_account_id FK -> accounts.id",
        # Text account_id / principal_id with no FK: erased by `stores.mux_erasure`,
        # except operations against a shared slot, which are counted as retained.
        "operation": "text account_id and principal_id (mux erasure)",
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
            # Shared setup binding attribution uses SET NULL on creator erasure;
            # deleting the conversation would destroy other participants' work.
            "thread_agent_bindings",
            # delete_account explicitly removes this person's user-scope directives;
            # shared entries belong to the repo and retain content with attribution
            # severed by ON DELETE SET NULL. Never erase other participants' memories.
            "repo_memory",
            # Active-turn authority is ephemeral, carries no conversation content,
            # and is automatically erased by accounts.id ON DELETE CASCADE.
            # The store tests verify this; it needs no separately reported category.
            "turn_origins",
            # Owner records cascade with account erasure, including conversation
            # metadata. Tagged Fly apps then have no registration and are deleted
            # by the scheduler orphan sweep; Daimon calls lose their owner authority.
            "compute_sessions",
            # Background command rows (with their output tail) cascade with the
            # account and with their session; the machine files go with the app.
            "compute_tasks",
            # Who may edit an agent: no content, and ON DELETE CASCADE with
            # accounts.id, after which the agent is admin-only again.
            "agent_owner",
            # Same shape for co-owners: the person's own row is ON DELETE CASCADE with
            # accounts.id, and the admin who added them is ON DELETE SET NULL, so erasing
            # either account never leaves an identifier behind. Covered by
            # test_agent_co_owners.py.
            "agent_co_owners",
            # Same shape: a previewed skill add waits at most 15 minutes for its
            # person's reply, holds only a content hash, and cascades with accounts.id.
            "pending_skill_adds",
            # A tenant-wide channel budget; the only accounts.id FK is the admin
            # who set it, set_by_account_id with ON DELETE SET NULL.
            "channel_budgets",
            # A tenant-wide channel skill; the only accounts.id FK is the admin
            # who added it, added_by_account_id with ON DELETE SET NULL.
            "channel_skills",
            # An avatar belongs to the tenant's agent, not the uploader. The
            # nullable uploader reference is severed by ON DELETE SET NULL;
            # tenant deletion cascades to the avatar itself.
            "agent_avatars",
            # Tenant/agent-scoped, no account/principal column — "purge account X"
            # is undefined for them; deferred to a future tenant-purge path.
            "agent_files",
            "agent_google_binding",
            "thread_sessions",
            # Billing carve-outs retained for integrity.
            "usage_events",
            "tenant_user_caps",
            # Tenant-scoped billing record; the only accounts.id FK is the nullable
            # redeemed_by_account_id with ON DELETE SET NULL, so erasure severs who
            # redeemed a code while the tenant keeps its credit history.
            "promo_redemptions",
            # Same shape for a channel code's redemptions: the nullable
            # redeemed_by_account_id is ON DELETE SET NULL, and the severed row
            # still counts toward the channel's cap.
            "promo_channel_redemptions",
            # A refused redeem attempt holds no content, lives 15 minutes for the
            # throttle, and its account_id is ON DELETE CASCADE.
            "promo_redeem_failures",
            # Repo authorization and grants belong to the tenant. Their nullable
            # creator references are erased by ON DELETE SET NULL.
            "tenant_github_repos",
            # An invitation is single-use and disappears with its requesting
            # account; its encrypted flow rows cascade from the invitation.
            "github_connect_invitations",
            "agent_github_grants",
            # Drafts belong to the tenant; deleting an account clears the
            # nullable actor reference and leaves the pending repo choice.
            "agent_github_grant_drafts",
            # A request is personal to the asker and cascades with their account.
            "github_access_requests",
            # A recipient's private card row cascades with their account.
            "github_access_request_deliveries",
            # Browser link attempts last ten minutes and cascade with the account.
            "github_personal_link_intents",
            # Connect Microsoft links last ten minutes and cascade with the account.
            "microsoft_sign_ins",
            # The account link is removed by ON DELETE CASCADE. A token's
            # requester reference is erased by SET NULL and its GitHub user ID
            # is explicitly cleared; its recorded link
            # generation then fails the inventory sweeper's link check.
            "account_github_links",
            "github_issued_tokens",
            # The vault belongs to a tenant session. Erasure clears its requester
            # FK; the scheduler then retires the orphaned session and vault
            # instead of renewing its tokens as a headless requester.
            "github_app_session_vaults",
            # The erasure fence's tombstone of a principal: a bare id with no content,
            # kept on purpose so a stale writer naming an erased principal is refused.
            "erased_principal",
            # A pending chat grant lives 15 minutes and both account references
            # are ON DELETE CASCADE, so erasing the account removes it.
            "github_grant_proposals",
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


async def test_every_mux_table_and_text_account_column_has_a_disposition() -> None:
    """Each provider-neutral table says what account erasure does with it.

    The account FK / principal_id drift guard above cannot see these tables' ownership:
    their account is plain text with no foreign key. The reflected schema finds every
    table that hangs off a slot, binding or journal head, or carries a text account or
    principal column, and each must have an explicit disposition in `stores.mux_erasure`
    (or be one of the tables another test covers).
    """
    from daimon.core.stores.mux_erasure import JSON_OWNERSHIP_FIELDS, TABLE_DISPOSITIONS
    from sqlalchemy import Text

    parents = {"provider_binding_slot", "provider_binding", "journal_session"}
    hanging_off_a_slot = {
        name
        for name, table in Base.metadata.tables.items()
        if any(fk.column.table.name in parents for fk in table.foreign_keys)
    }
    text_account_columns = {
        name
        for name, table in Base.metadata.tables.items()
        if any(
            column.name in {"account_id", "principal_id", "legacy_account_id"}
            and isinstance(column.type, Text)
            for column in table.columns
        )
    }
    # Text principal columns that also carry an accounts.id FK, or an account-keyed
    # receipt table; each is deleted by the purge and listed in the test above.
    purged_elsewhere = {
        "neutral_agent_revisions",
        "neutral_skill_versions",
        "selfhosted_resource_operations",
    }
    undisposed = (
        (parents | hanging_off_a_slot | text_account_columns)
        - set(TABLE_DISPOSITIONS)
        - purged_elsewhere
    )
    assert not undisposed, f"mux table(s) {sorted(undisposed)} have no erasure disposition"
    assert set(TABLE_DISPOSITIONS) <= set(Base.metadata.tables), "disposition for a removed table"
    assert all(TABLE_DISPOSITIONS.values()), "every disposition must say what happens"
    for path, why in JSON_OWNERSHIP_FIELDS.items():
        table = path.split(".")[0].split(" ")[0]
        assert table in TABLE_DISPOSITIONS and why, f"{path} names a table with no disposition"
    assert {
        "provider_binding_slot",
        "provider_binding",
        "thread_lease",
        "thread_provider_release",
        "operation",
        "journal_session",
        "journal",
        "usage_observation",
        "accounting_outbox",
        "channel_config_revision",
    } <= set(TABLE_DISPOSITIONS), "a mux table lost its disposition"


async def test_account_purge_removes_selfhosted_receipts_and_keeps_shared_resources(
    db_session, db_session_factory
):
    from daimon.core.stores.selfhosted_resources import SelfHostedAgents
    from mux.contracts.ids import ModelRef, PageRequest, Scope
    from mux.contracts.resources import AgentFilter, AgentSpec
    from mux.profiles.selfhosted import LOOP

    tenant = await make_tenant(db_session)
    first = await make_account(db_session, tenant=tenant)
    other = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    agents = SelfHostedAgents(db_session_factory)
    for account, name in ((first, "first"), (other, "other")):
        caller = Scope(
            tenant_id=str(tenant.id),
            account_id=str(account.id),
            principal_id=name,
            authorization_id="test",
        )
        await agents.create(
            caller,
            AgentSpec(name=name, model=ModelRef(provider=LOOP.provider, id="gpt-6-luna")),
            key="create",
        )
    preview = await collect_purge_preview(sm=db_session_factory, account_id=first.id)
    assert preview.selfhosted_operations.count == 1
    report = await purge_account(sm=db_session_factory, account_id=first.id)
    assert report.db.selfhosted_operations == 1
    remaining = await collect_purge_preview(sm=db_session_factory, account_id=other.id)
    assert remaining.selfhosted_operations.count == 1
    caller = Scope(
        tenant_id=str(tenant.id),
        account_id=str(other.id),
        principal_id="other",
        authorization_id="test",
    )
    assert len((await agents.list(caller, filters=AgentFilter(), page=PageRequest())).data) == 2
    assert (
        await purge_account(sm=db_session_factory, account_id=first.id)
    ).db.selfhosted_operations == 0


async def test_collect_purge_preview_linked_principals_example_omits_platform_names(
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
    assert preview.linked_principals.example == "42", (
        "linked account examples must not name another chat platform"
    )
