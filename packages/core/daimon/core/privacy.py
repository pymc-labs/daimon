"""Read-only snapshot of every daimon-side row that purge_account would remove.

Counterpart to `daimon.core.purge`: same FK-safe coverage, read-only. The
/privacy adapter renders this snapshot in the cascade-preview embed so the
user sees exactly what the subsequent `purge_account(...)` call will delete.
No Anthropic SDK calls — MA-side bodies, sessions, vault contents stay in
the Anthropic workspace and are not enumerated here.

Both count from `daimon.core.erasure.ERASABLE`, so a table purge erases is counted
here too. The schema-reflecting drift guard in `tests/test_privacy.py` checks every
account- or principal-keyed table has a disposition.

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
from collections.abc import Callable, Iterable
from dataclasses import fields
from typing import Final

from daimon.core.erasure import ERASABLE, Subject
from daimon.core.stores import accounts as accounts_store
from daimon.core.stores import github_links as github_links_store
from daimon.core.stores import identity as identity_store
from daimon.core.stores import mux_erasure as mux_erasure_store
from daimon.core.stores import watches as watches_store
from daimon.core.stores.domain import CliPrincipalRow, PlatformPrincipalRow
from daimon.core.turn.notices import provider_name
from mux.profiles import INLINE_REUSE, PERSISTENT_WORKSPACE
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
    watches: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    routines: PurgePreviewRow
    user_configs: PurgePreviewRow
    account: PurgePreviewRow  # singular: 1 if row exists else 0
    user_skills: PurgePreviewRow
    github_connect_requests: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    github_connect_click_intents: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    github_user_links: PurgePreviewRow
    connector_tokens: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    gemini_records: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    selfhosted_operations: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    mcp_tokens: PurgePreviewRow
    slack_user_tokens: PurgePreviewRow
    slack_turn_contexts: PurgePreviewRow
    direct_message_conversations: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    dm_carry_overs: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    held_timers: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    tag_opt_outs: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    dm_picks: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    dm_rules: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    dm_grant_users: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    credential_requests: PurgePreviewRow
    secret_shares: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    wizard_sessions: PurgePreviewRow
    message_feedback: PurgePreviewRow
    support_escalations: PurgePreviewRow
    support_requests_anonymized: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    support_person_limits: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    support_settlers_cleared: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    daimon_admins: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    agent_post_requesters: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    platform_user_names: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    teams_messages: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    microsoft_user_tokens: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    lead_intakes: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    web_people: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    web_admin_grants: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    monitored_events: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    workflows: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    ops_events: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    # Provider-neutral conversation state erased with the account (stores.mux_erasure),
    # one row per PurgeReport mux field.
    mux_slots: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    mux_binding_generations: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    mux_journal_sessions: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    mux_journal_events: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    mux_leases: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    mux_release_requests: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    mux_operations: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    host_call_cards: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    # What stays, so the confirmation can say so: conversations other people share with
    # the person (and the person's requests in them), and settlement records.
    retained_shared_conversations: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    retained_shared_operations: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    retained_accounting_records: PurgePreviewRow = PurgePreviewRow(count=0, example=None)
    retained_shared_provider_records: PurgePreviewRow = PurgePreviewRow(count=0, example=None)


def held_lines(preview: PurgePreview) -> list[str]:
    """The non-zero held-data categories, one counted phrase each."""
    categories = (
        (preview.linked_principals, "linked account"),
        (preview.routines, "routine"),
        (preview.watches, "page watch"),
        (preview.lead_intakes, "lead intake"),
        (preview.user_configs, "saved settings"),
        (preview.user_skills, "skill you synced"),
        (preview.github_connect_requests, "GitHub setup request"),
        (preview.github_connect_click_intents, "GitHub connect button"),
        (preview.github_user_links, "linked GitHub account"),
        (preview.mcp_tokens, "Daimon access token"),
        (preview.connector_tokens, "saved connector sign-in"),
        (preview.gemini_records, "Gemini conversation record"),
        (preview.selfhosted_operations, "self-hosted retry record"),
        (preview.slack_user_tokens, "saved access token"),
        (preview.slack_turn_contexts, "temporary request record"),
        (preview.direct_message_conversations, "private conversation"),
        (preview.dm_carry_overs, "imported earlier conversation"),
        (preview.held_timers, "imported timer"),
        (preview.tag_opt_outs, "never-tag setting"),
        (preview.dm_picks, "private conversation agent choice"),
        (preview.dm_rules, "private conversation routing rule"),
        (preview.dm_grant_users, "private conversation access entry"),
        (preview.daimon_admins, "Daimon admin grants"),
        (preview.agent_post_requesters, "post an agent made for you"),
        (preview.platform_user_names, "remembered name"),
        (preview.teams_messages, "message record"),
        (preview.microsoft_user_tokens, "saved Microsoft account"),
        (preview.web_people, "web app profile"),
        (preview.web_admin_grants, "web app admin role"),
        (preview.mux_slots, "saved conversation"),
        (preview.mux_journal_events, "saved conversation message"),
        (preview.host_call_cards, "approval request"),
        (preview.monitored_events, "record admins can read"),
        (preview.workflows, "workflow record"),
        (preview.ops_events, "failure record"),
    )
    parts = [
        "saved settings"
        if row is preview.user_configs
        else f"{row.count} {label if row.count == 1 else _plural(label)}"
        for row, label in categories
        if row.count > 0
    ]
    return parts


def cascade_lines(
    preview: PurgePreview,
    platform: str,
    *,
    bold: str = "**",
    code: str = "`",
    escape: Callable[[str], str] = str,
    app: str = "",
) -> list[str]:
    """The delete confirmation's "What will happen" lines, in each platform's markup.

    ``app`` names the chat app in the token and request-record lines, where it applies.
    """

    def n(row: PurgePreviewRow, singular: str) -> str:
        return f"{bold}{row.count}{bold} {singular if row.count == 1 else singular + 's'}"

    def ex(row: PurgePreviewRow) -> str:
        return f"{code}{escape(row.example or '—')}{code}"

    slack = f" {app}" if app else ""
    p = preview
    lines = (
        (
            p.linked_principals,
            f"🔑 Remove {n(p.linked_principals, 'linked account')}, "
            f"for example {ex(p.linked_principals)}",
        ),
        (p.routines, f"⏰ Cancel {n(p.routines, 'routine')}"),
        (p.principal_links, f"🔗 Remove {n(p.principal_links, 'link')} between your accounts"),
        (p.user_configs, "⚙ Remove your saved settings"),
        (
            p.user_skills,
            f"🧰 Forget {n(p.user_skills, 'skill')} you synced, for example {ex(p.user_skills)}",
        ),
        (p.github_user_links, f"🔗 Unlink {n(p.github_user_links, 'GitHub account')}"),
        (p.mcp_tokens, f"🎫 Delete {n(p.mcp_tokens, 'Daimon access token')}"),
        (p.slack_user_tokens, f"🔐 Delete {n(p.slack_user_tokens, f'saved{slack} access token')}"),
        (
            p.microsoft_user_tokens if platform == "teams" else None,
            "🔌 Disconnect your Microsoft account",
        ),
        (
            p.slack_turn_contexts,
            f"💬 Delete {n(p.slack_turn_contexts, f'temporary{slack} request record')}",
        ),
        (
            p.direct_message_conversations,
            "💬 Delete Daimon's saved data for "
            f"{n(p.direct_message_conversations, 'private conversation')}",
        ),
        (
            p.mux_slots if platform != "teams" else None,
            f"💬 Delete Daimon's copy of {n(p.mux_slots, 'saved conversation')}, with its messages",
        ),
        (p.account, "🪪 Remove your account"),
    )
    return [line for row, line in lines if row is not None and row.count > 0]


def summary_line(preview: PurgePreview) -> str:
    """The non-zero held-data categories, comma-joined, for the privacy panels."""
    parts = held_lines(preview)
    return ", ".join(parts) if parts else "nothing listed here"


def _plural(label: str) -> str:
    if label == "page watch":
        return "page watches"
    if label == "skill you synced":
        return "skills you synced"
    if label == "link from an agent to your GitHub keys":
        return "links from agents to your GitHub keys"
    if label == "post an agent made for you":
        return "posts agents made for you"
    if label == "record admins can read":
        return "records admins can read"
    return f"{label}s"


PRIVACY_TITLE: Final[str] = "🔒 Your data"
DELETE_PAUSED: Final[str] = (
    "Deleting your account is paused during the event.\n\n"
    "Ask the team running Daimon if you need it deleted now."
)


MONITORED_LINE: Final[str] = "Admins can read your conversations with the agents they monitor."

#: The other providers that store what an agent running on them is sent.
_OPENAI: Final[str] = PERSISTENT_WORKSPACE.provider
_GEMINI: Final[str] = INLINE_REUSE.provider
_OTHER_PROVIDERS: Final[tuple[str, ...]] = (_OPENAI, _GEMINI)


def _other_providers_text(providers: Iterable[str]) -> str | None:
    """The enabled other providers as "OpenAI and Gemini"; None when there are none."""
    chosen = set(providers)
    named = [provider_name(key) for key in _OTHER_PROVIDERS if key in chosen]
    return " and ".join(named) if named else None


async def account_other_providers(
    sm: async_sessionmaker[AsyncSession], *, account_id: uuid.UUID
) -> tuple[str, ...]:
    """The providers besides Anthropic the account's tenant admin turned on."""
    from daimon.core.stores.access_policy import load_access_policy

    async with sm() as session:
        account = await accounts_store.get_account(session, account_id)
        if account is None:
            return ()
        policy = await load_access_policy(session, tenant_id=account.tenant_id)
    return tuple(p for p in _OTHER_PROVIDERS if p in policy.enabled_backends)


def privacy_lines(
    display_name: str, providers: Iterable[str] = (), *, monitored: bool = False
) -> tuple[str, ...]:
    """The privacy panel's lines: what the deployment keeps, what the providers keep and,
    when admins monitor an agent in the workspace (`monitoring.monitors_any`), that they can
    read it.

    `providers` are the other providers an admin turned on; each is named, since an
    agent that runs on one sends it the conversation. The categories behind the lines
    are the privacy policy's, one button away.
    """
    others = _other_providers_text(providers)
    lines = (
        f"{display_name} stores your linked accounts, routines and settings. "
        "Saved GitHub keys are encrypted.",
        "Anthropic stores your agents and their conversations."
        + (f" {others} also stores them for agents that run there." if others else ""),
    )
    return (*lines, MONITORED_LINE) if monitored else lines


def delete_scope_lines(display_name: str, providers: Iterable[str] = ()) -> tuple[str, str]:
    """What Delete removes and what it leaves, true to `purge_account`.

    The purge deletes the deployment's own rows about the account and, upstream,
    tries to delete every Managed Agents session tagged with it (the
    conversations it started); that upstream delete can fail, so the confirm
    promises only the attempt. OpenAI sessions get the same attempt; Gemini keeps
    its stored interactions for its own retention. Shared agents and their memory stay;
    see the carve-outs in `daimon.core.purge`.
    """
    chosen = set(providers)
    elsewhere = ""
    if _OPENAI in chosen:
        elsewhere += " It tries the same at OpenAI."
    if _GEMINI in chosen:
        elsewhere += " Gemini conversations stay with Google for its own retention period."
    return (
        f"This deletes {display_name}'s records about you and tries to delete your "
        "conversations stored at Anthropic." + elsewhere,
        "Shared agents and their memory stay, and other people may keep using them.",
    )


def _format_platform_principal(p: PlatformPrincipalRow) -> str:
    # The id alone: the panel must not name a chat app other than the one it is shown on.
    return p.external_id


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

    No Anthropic SDK calls. No mutation. Counts each `ERASABLE` entry purge erases,
    plus the hand-written watches, mux state, account row and GitHub user.
    """
    async with sm() as session:
        cli_list = await identity_store.list_cli_principals_for_account(
            session, account_id=account_id
        )
        pp_list = await identity_store.list_platform_principals_for_account(
            session, account_id=account_id
        )
        subject = Subject(account_id, tuple(cli_list), tuple(pp_list))
        rows: dict[str, PurgePreviewRow] = {}
        for entry in ERASABLE:
            if entry.count is None:
                continue
            n = await entry.count(session, subject)
            example = await entry.example(session, subject) if entry.example and n else None
            rows[entry.key] = PurgePreviewRow(count=n, example=example)
        mux = await mux_erasure_store.inventory(
            session, account_id=account_id, principal_ids=[p.id for p in subject.principals]
        )
        for prefix, counts in (("mux_", mux.erased), ("retained_", mux.retained)):
            for f in fields(counts):
                rows[prefix + f.name] = PurgePreviewRow(count=getattr(counts, f.name), example=None)
        account = int(await accounts_store.account_exists(session, account_id=account_id))
        orphaned = await github_links_store.count_users_orphaned_by_account_delete(
            session, account_id=account_id
        )
        watches = await watches_store.count_for_account(session, account_id=account_id)
    return PurgePreview(
        **rows,
        linked_principals=PurgePreviewRow(
            count=len(cli_list) + len(pp_list),
            example=_format_linked_principals_example(cli_list, pp_list),
        ),
        account=PurgePreviewRow(count=account, example=None),
        github_user_links=PurgePreviewRow(count=orphaned, example=None),
        watches=PurgePreviewRow(count=watches, example=None),
    )
