"""Read-only snapshot of every daimon-side row that purge_account would remove.

Counterpart to `daimon.core.purge`: same FK-safe coverage, read-only. The
/privacy adapter renders this snapshot in the cascade-preview embed so the
user sees exactly what the subsequent `purge_account(...)` call will delete.
No Anthropic SDK calls — MA-side bodies, sessions, vault contents stay in
the Anthropic workspace and are not enumerated here.

Adding a new account- or principal-scoped table that `purge_account` learns to
delete MUST update this module too, or the preview undercounts. The schema-
reflecting drift-guard test in `tests/test_privacy.py` enforces this at CI time.

Scope of "every row": both this preview and the purge it mirrors are scoped to
the account's OWN principals. One residue class follows from that and is
documented in full under `daimon.core.purge`'s carve-outs — message_feedback
rows cast in a tenant where the person has no principal (a vote by a bystander
carries account_id = NULL and no principal is minted for it) are neither
previewed nor deleted by a run invoked from another tenant. The remediation is
guild-local and self-service: running this same privacy purge inside that guild
mints a principal there, which brings its (tenant, platform-user) key into
scope and erases those rows.
"""

from __future__ import annotations

import uuid

from daimon.core.stores import accounts as accounts_store
from daimon.core.stores import agent_github_binding as agent_github_binding_store
from daimon.core.stores import credential_requests as credential_requests_store
from daimon.core.stores import github_credentials as github_credentials_store
from daimon.core.stores import github_oauth_states as github_oauth_states_store
from daimon.core.stores import identity as identity_store
from daimon.core.stores import mcp_tokens as mcp_tokens_store
from daimon.core.stores import message_feedback as message_feedback_store
from daimon.core.stores import routines as routines_store
from daimon.core.stores import slack_turn_contexts as slack_turn_contexts_store
from daimon.core.stores import slack_user_tokens as slack_user_tokens_store
from daimon.core.stores import support_escalation as support_escalation_store
from daimon.core.stores import tenants as tenants_store
from daimon.core.stores import user_skills as user_skills_store
from daimon.core.stores import wizard_session as wizard_session_store
from daimon.core.stores.domain import CliPrincipalRow, PlatformPrincipalRow
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class PurgePreviewRow(BaseModel):
    """Per-category preview row: count plus a human-display example string."""

    model_config = ConfigDict(frozen=True)
    count: int
    # Human-display label; None when count == 0 or the category has no
    # human-meaningful label (principal_links, user_configs, account,
    # github_oauth_states set example=None at any count).
    example: str | None


class PurgePreview(BaseModel):
    """1:1 with PurgeReport categories — same FK-safe coverage as purge_account.

    `cli_principals` + `platform_principals` from PurgeReport are folded into
    `linked_principals` here for display. The drift-guard test maps every
    PurgeReport field to a PurgePreview target field; adding a new
    PurgeReport int without extending the mapping (and this model) breaks CI.
    """

    model_config = ConfigDict(frozen=True)
    linked_principals: PurgePreviewRow  # combined CLI + platform
    principal_links: PurgePreviewRow
    routines: PurgePreviewRow
    user_configs: PurgePreviewRow
    account: PurgePreviewRow  # singular: 1 if row exists else 0
    user_skills: PurgePreviewRow
    github_credentials: PurgePreviewRow
    github_oauth_states: PurgePreviewRow
    mcp_tokens: PurgePreviewRow
    agent_github_binding: PurgePreviewRow
    slack_user_tokens: PurgePreviewRow
    slack_turn_contexts: PurgePreviewRow
    credential_requests: PurgePreviewRow
    wizard_sessions: PurgePreviewRow
    message_feedback: PurgePreviewRow
    support_escalations: PurgePreviewRow


def _format_platform_principal(p: PlatformPrincipalRow) -> str:
    # Capitalize the platform name for display, e.g. "Discord:1234567890".
    return f"{p.platform.capitalize()}:{p.external_id}"


def _format_cli_principal(p: CliPrincipalRow) -> str:
    return f"CLI:os_user={p.os_user}"


def _format_linked_principals_example(
    cli_list: list[CliPrincipalRow], pp_list: list[PlatformPrincipalRow]
) -> str | None:
    """Enumerate ALL linked principals comma-joined; None when both lists empty."""
    parts: list[str] = []
    for pp in pp_list:
        parts.append(_format_platform_principal(pp))
    for cli in cli_list:
        parts.append(_format_cli_principal(cli))
    return ", ".join(parts) if parts else None


async def collect_purge_preview(
    *,
    sm: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
) -> PurgePreview:
    """Read-only snapshot of what `purge_account(account_id=...)` would remove.

    No Anthropic SDK calls. No mutation. Mirrors `purge_account`'s FK-safe
    coverage by walking the same store helpers in read mode.
    """
    async with sm() as session:
        # 1. Linked principals (CLI + platform under the account).
        cli_list = await identity_store.list_cli_principals_for_account(
            session, account_id=account_id
        )
        pp_list = await identity_store.list_platform_principals_for_account(
            session, account_id=account_id
        )
        linked_principals = PurgePreviewRow(
            count=len(cli_list) + len(pp_list),
            example=_format_linked_principals_example(cli_list, pp_list),
        )

        # 2. principal_links — distinct links touching ANY principal under the
        # account. `purge_account` deletes each link exactly once (on the first
        # principal walk that touches it), so summing per-principal counts here
        # would double-count linked pairs where both endpoints are in-account.
        links_total = await identity_store.count_principal_links_for_account(
            session,
            cli_principal_ids=[cli.id for cli in cli_list],
            platform_principal_ids=[pp.id for pp in pp_list],
        )
        principal_links = PurgePreviewRow(
            count=links_total,
            example=None,
        )

        # 3. Routines (platform principals only — CLI principals don't own routines).
        routines_total = 0
        routines_example: str | None = None
        for pp in pp_list:
            n = await routines_store.count_routines_for_principal(
                session, tenant_id=pp.tenant_id, external_id=pp.external_id
            )
            routines_total += n
            if routines_example is None and n > 0:
                first = await routines_store.get_first_routine_for_principal(
                    session, tenant_id=pp.tenant_id, external_id=pp.external_id
                )
                if first is not None:
                    # RoutineRow has no human-meaningful label field today; use id.
                    routines_example = str(first.id)
        routines = PurgePreviewRow(count=routines_total, example=routines_example)

        # 4. user_configs.
        user_cfg_count = await accounts_store.count_user_config_for_account(
            session, account_id=account_id
        )
        user_configs = PurgePreviewRow(count=user_cfg_count, example=None)

        # 5. account row itself.
        exists = await accounts_store.account_exists(session, account_id=account_id)
        account = PurgePreviewRow(count=1 if exists else 0, example=None)

        # 6. user_skills — both CLI and platform principals own rows.
        user_skills_total = 0
        user_skills_example: str | None = None
        for p in (*cli_list, *pp_list):
            n = await user_skills_store.count_user_skills_for_principal(session, principal_id=p.id)
            user_skills_total += n
            if user_skills_example is None and n > 0:
                first_skill = await user_skills_store.get_first_user_skill_for_principal(
                    session, principal_id=p.id
                )
                if first_skill is not None:
                    user_skills_example = first_skill.name
        user_skills = PurgePreviewRow(count=user_skills_total, example=user_skills_example)

        # 7. github_credentials — both principal kinds. PK lookup:
        # count is 0 or 1 per principal; login string is the display example.
        github_credentials_total = 0
        github_credentials_example: str | None = None
        for p in (*cli_list, *pp_list):
            login = await github_credentials_store.get_credential_login_by_principal(
                session, principal_id=p.id
            )
            if login is not None:
                github_credentials_total += 1
                if github_credentials_example is None:
                    github_credentials_example = login
        github_credentials = PurgePreviewRow(
            count=github_credentials_total, example=github_credentials_example
        )

        # 8. github_oauth_states — keyed by (platform, platform_user_id). Build
        # the DISTINCT set of keys to avoid double-counting when two same-account
        # CLI principals share an os_user (mirrors the principal_links dedup
        # rationale). Both CLI and platform keys carry the principal's
        # tenant_id — mirroring the purge predicate exactly on both paths: an
        # external id (Slack/Discord user id, or CLI os_user) is not globally
        # unique across tenants, so both deletes are tenant-scoped.
        oauth_keys: set[tuple[str, str, uuid.UUID]] = set()
        for pp in pp_list:
            oauth_keys.add((pp.platform, pp.external_id, pp.tenant_id))
        for cli in cli_list:
            oauth_keys.add(("cli", cli.os_user, cli.tenant_id))
        github_oauth_states_total = 0
        for platform, platform_user_id, key_tenant_id in oauth_keys:
            n = await github_oauth_states_store.count_states_for_platform_user(
                session,
                platform=platform,
                platform_user_id=platform_user_id,
                tenant_id=key_tenant_id,
            )
            github_oauth_states_total += n
        github_oauth_states = PurgePreviewRow(
            count=github_oauth_states_total,
            example=None,  # no human-meaningful label; matches principal_links precedent
        )

        # 9. mcp_tokens — account-scoped (keyed by account_id, not principal), so
        # a single count mirrors the account/user_configs blocks above.
        mcp_tokens_count = await mcp_tokens_store.count_tokens_for_account(
            session, account_id=account_id
        )
        mcp_tokens = PurgePreviewRow(count=mcp_tokens_count, example=None)

        # 10. agent_github_binding — principal-scoped; sum a per-principal count
        # over both kinds, mirroring the user_skills loop.
        agent_github_binding_total = 0
        for p in (*cli_list, *pp_list):
            agent_github_binding_total += await agent_github_binding_store.count_for_principal(
                session, principal_id=p.id
            )
        agent_github_binding = PurgePreviewRow(count=agent_github_binding_total, example=None)

        # 11. slack_user_tokens — Slack platform principals only, 0 or 1 per
        # principal (PK is (team_id, slack_user_id)), mirroring the
        # github_credentials loop. team_id = Tenant.external_id resolved via
        # the principal's tenant_id.
        slack_user_tokens_total = 0
        tenant_cache: dict[uuid.UUID, str | None] = {}
        for pp in pp_list:
            if pp.platform != "slack":
                continue
            if pp.tenant_id not in tenant_cache:
                tenant_row = await tenants_store.get_tenant(session, pp.tenant_id)
                tenant_cache[pp.tenant_id] = (
                    tenant_row.external_id if tenant_row is not None else None
                )
            team_id = tenant_cache[pp.tenant_id]
            if team_id is None:
                continue
            token = await slack_user_tokens_store.get_slack_user_token(
                session, team_id=team_id, slack_user_id=pp.external_id
            )
            if token is not None:
                slack_user_tokens_total += 1
        slack_user_tokens = PurgePreviewRow(count=slack_user_tokens_total, example=None)

        # 12. slack_turn_contexts — account-scoped (tenant_id, account_id),
        # summed over every distinct tenant any principal belongs to, mirroring
        # purge_account's loop.
        distinct_tenant_ids = {p.tenant_id for p in (*cli_list, *pp_list)}
        slack_turn_contexts_total = 0
        for tenant_id in distinct_tenant_ids:
            slack_turn_contexts_total += (
                await slack_turn_contexts_store.count_turn_contexts_for_account(
                    session, tenant_id=tenant_id, account_id=account_id
                )
            )
        slack_turn_contexts = PurgePreviewRow(count=slack_turn_contexts_total, example=None)

        # 13. credential_requests — requester_platform_user_id-scoped. Keyed by
        # (platform_user_id, tenant_id) mirroring purge_principal's actual
        # per-principal tenant-scoped delete call exactly (unlike the
        # github_oauth_states block above, which counts platform principals
        # cross-tenant), so this preview agrees field-for-field with what
        # purge_account actually deletes. Dedup avoids double-counting when two
        # principals under the account share a (platform_user_id, tenant_id) key.
        credential_request_keys: set[tuple[str, uuid.UUID]] = set()
        for pp in pp_list:
            credential_request_keys.add((pp.external_id, pp.tenant_id))
        for cli in cli_list:
            credential_request_keys.add((cli.os_user, cli.tenant_id))
        credential_requests_total = 0
        for platform_user_id, key_tenant_id in credential_request_keys:
            credential_requests_total += (
                await credential_requests_store.count_credential_requests_for_platform_user(
                    session,
                    platform_user_id=platform_user_id,
                    tenant_id=key_tenant_id,
                )
            )
        credential_requests = PurgePreviewRow(count=credential_requests_total, example=None)

        # 14. wizard_sessions — requester_platform_user_id-scoped, same shape as
        # credential_requests above. Keyed by (platform_user_id, tenant_id),
        # mirroring _purge_principal_in_session's actual tenant-scoped delete
        # call exactly, so this preview agrees field-for-field with what
        # purge_account actually deletes. Dedup avoids double-counting when two
        # principals under the account share a (platform_user_id, tenant_id) key.
        wizard_session_keys: set[tuple[str, uuid.UUID]] = set()
        for pp in pp_list:
            wizard_session_keys.add((pp.external_id, pp.tenant_id))
        for cli in cli_list:
            wizard_session_keys.add((cli.os_user, cli.tenant_id))
        wizard_sessions_total = 0
        for platform_user_id, key_tenant_id in wizard_session_keys:
            wizard_sessions_total += (
                await wizard_session_store.count_wizard_sessions_for_platform_user(
                    session,
                    platform_user_id=platform_user_id,
                    tenant_id=key_tenant_id,
                )
            )
        wizard_sessions = PurgePreviewRow(count=wizard_sessions_total, example=None)

        # 14. message_feedback — account-scoped (keyed by account_id, not
        # principal), OR'd with the account's (tenant, platform-user) keys
        # exactly as purge_account's delete does, since a vote cast before the
        # person had an accounts row carries a null account_id. The argument
        # shapes here are identical to purge_account's call by contract — any
        # divergence makes this preview lie about what erasure will remove.
        message_feedback_platform_user_keys = [(pp.tenant_id, pp.external_id) for pp in pp_list]
        message_feedback_count = await message_feedback_store.count_message_feedback_for_account(
            session,
            account_id=account_id,
            platform_user_keys=message_feedback_platform_user_keys,
        )
        message_feedback = PurgePreviewRow(count=message_feedback_count, example=None)

        # 15. support_escalations — same account-scoped shape, and the SAME
        # platform-user keys, as message_feedback above. The argument shapes
        # here are identical to purge_account's delete by contract; any
        # divergence makes this preview lie about what erasure will remove.
        support_escalations_count = (
            await support_escalation_store.count_support_escalations_for_account(
                session,
                account_id=account_id,
                platform_user_keys=message_feedback_platform_user_keys,
            )
        )
        support_escalations = PurgePreviewRow(count=support_escalations_count, example=None)

    return PurgePreview(
        linked_principals=linked_principals,
        principal_links=principal_links,
        routines=routines,
        user_configs=user_configs,
        account=account,
        user_skills=user_skills,
        github_credentials=github_credentials,
        github_oauth_states=github_oauth_states,
        mcp_tokens=mcp_tokens,
        agent_github_binding=agent_github_binding,
        slack_user_tokens=slack_user_tokens,
        slack_turn_contexts=slack_turn_contexts,
        credential_requests=credential_requests,
        wizard_sessions=wizard_sessions,
        message_feedback=message_feedback,
        support_escalations=support_escalations,
    )
