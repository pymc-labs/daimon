"""Read-only snapshot of every daimon-side row that purge_account would remove.

Counterpart to `daimon.core.purge`: same FK-safe coverage, read-only. The
/privacy adapter renders this snapshot in the cascade-preview embed so the
user sees exactly what the subsequent `purge_account(...)` call will delete.
No Anthropic SDK calls — MA-side bodies, sessions, vault contents stay in
the Anthropic workspace and are not enumerated here.

Adding a new account- or principal-scoped table that `purge_account` learns to
delete MUST update this module too, or the preview undercounts. The schema-
reflecting drift-guard test in `tests/test_privacy.py` enforces this at CI time.
"""

from __future__ import annotations

import uuid

from daimon.core.stores import accounts as accounts_store
from daimon.core.stores import agent_github_binding as agent_github_binding_store
from daimon.core.stores import github_credentials as github_credentials_store
from daimon.core.stores import github_oauth_states as github_oauth_states_store
from daimon.core.stores import identity as identity_store
from daimon.core.stores import mcp_tokens as mcp_tokens_store
from daimon.core.stores import routines as routines_store
from daimon.core.stores import user_skills as user_skills_store
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
        # rationale). CLI keys carry the principal's tenant_id — mirroring the
        # purge predicate exactly: os_user is not globally unique, so the CLI
        # delete is tenant-scoped while platform deletes stay tenant-agnostic.
        oauth_keys: set[tuple[str, str, uuid.UUID | None]] = set()
        for pp in pp_list:
            oauth_keys.add((pp.platform, pp.external_id, None))
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
    )
