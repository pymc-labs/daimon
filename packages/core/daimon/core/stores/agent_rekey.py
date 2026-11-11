"""Move every row keyed by one agent onto another, when an agent changes provider.

A conversion makes a new record with a new id (and so a new derived uuid). The
columns in `REKEYED` follow it so owner, threads, routines, memory, features and
grants carry over; `KEPT` names the rest and why they stay. The contract test
checks that every agent-id column in the metadata is in exactly one of the two.
"""

from __future__ import annotations

import uuid
from typing import Final

from daimon.core._models import Base
from daimon.core.ma_identity import derive_agent_uuid
from sqlalchemy import Column, Table, and_, case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

REKEYED: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("agent_channel_memory_store", "agent_id"),
        ("agent_co_owners", "agent_id"),
        ("agent_creation_channels", "ma_agent_id"),
        ("agent_daimon_tool_allowlists", "agent_id"),
        ("agent_files", "agent_id"),
        ("agent_github_grant_drafts", "agent_id"),
        ("agent_github_grants", "agent_id"),
        ("agent_google_binding", "agent_id"),
        ("agent_helpers", "agent_id"),
        ("agent_helpers", "helper_agent_id"),
        ("agent_mcp_credentials", "agent_id"),
        ("agent_memory_store", "agent_id"),
        ("agent_notices", "agent_id"),
        ("agent_owner", "agent_id"),
        ("agent_personal_memory_store", "agent_id"),
        ("agent_posted_messages", "agent_id"),
        ("agent_skill_follows", "agent_id"),
        ("agent_tool_rules", "agent_id"),
        ("coding_key_claims", "agent_id"),
        ("coding_key_claims", "ma_agent_id"),
        ("compute_sessions", "agent_id"),
        ("connector_tokens", "agent_id"),
        ("context_provider_bindings", "agent_id"),
        ("credential_requests", "agent_id"),
        ("credential_requests", "target_ma_agent_id"),
        ("feature_values", "agent_id"),
        ("github_access_requests", "agent_id"),
        ("github_access_requests", "ma_agent_id"),
        ("github_app_session_vaults", "agent_id"),
        ("github_connect_click_intents", "agent_id"),
        ("github_connect_click_intents", "agent_ma_id"),
        ("github_connect_click_intents", "origin_ma_agent_id"),
        ("github_connect_invitations", "agent_id"),
        ("github_connect_invitations", "agent_ma_id"),
        ("github_connect_invitations", "origin_ma_agent_id"),
        ("github_connect_requests", "agent_id"),
        ("github_grant_proposals", "agent_id"),
        ("github_issued_tokens", "agent_id"),
        ("github_router_sessions", "agent_id"),
        ("helper_asks", "helper_agent_id"),
        ("host_call_cards", "executing_agent_id"),
        ("host_call_cards", "executing_ma_agent_id"),
        ("long_job_policy", "agent_id"),
        ("mcp_oauth_flows", "agent_id"),
        ("mcp_tokens", "agent_id"),
        ("pending_skill_adds", "ma_agent_id"),
        ("routines", "agent_id"),
        ("routines", "sandbox_agent_id"),
        ("spend_allowances", "agent_id"),
        ("task_continuations", "target_ma_agent_id"),
        ("tenant_github_repos", "scope_agent_id"),
        ("thread_agent_bindings", "configuration_target_ma_agent_id"),
        ("thread_agent_bindings", "responder_ma_agent_id"),
        ("thread_sessions", "ma_agent_id"),
        ("turn_origins", "configuration_target_ma_agent_id"),
        ("turn_origins", "responder_ma_agent_id"),
        # Per-agent spend caps read these; the agent keeps its month's spend.
        ("usage_events", "agent_id"),
        ("watches", "agent_id"),
        ("web_agent_grants", "agent_id"),
        ("web_invite_agents", "agent_id"),
        ("web_threads", "agent_id"),
    }
)

KEPT: Final[dict[tuple[str, str], str]] = {
    ("agent_backend_revision", "agent_id"): "the new record states its own provider",
    ("agent_versions", "agent_id"): "the old record's provider versions",
    ("selfhosted_agent_versions", "agent_id"): "the Daimon record's own history",
    ("agent_drafts", "agent_id"): "drafts were made against the old version",
    ("thread_model_consents", "agent_id"): "consent covered the old revision; asked again",
    ("agent_tool_rule_history", "agent_id"): "history",
    ("long_job_policy_history", "agent_id"): "history",
    ("long_job", "agent_id"): "history; running jobs block the move",
    ("routine_sandbox_history", "agent_id"): "history",
    ("turn_outcomes", "agent_id"): "history",
    ("security_audit_events", "agent_id"): "audit trail",
    ("neutral_agent_revisions", "agent"): "per-account native catalog, keyed by name",
    ("channel_config_revision", "agent_backend_ref"): "a kept revision re-resolves by name",
    ("web_workspace_settings", "default_agent"): "an agent name",
}

# Names and versions follow the agent by name, so they never need a move.
_NOT_IDS: Final = ("agent_name", "agent_version")


def agent_id_columns() -> set[tuple[str, str]]:
    """Every metadata column whose name says it holds an agent id."""
    return {
        (table.name, column.name)
        for table in Base.metadata.tables.values()
        for column in table.columns
        if "agent" in column.name and not any(part in column.name for part in _NOT_IDS)
    }


def _is_uuid(column: Column[object]) -> bool:
    try:
        return column.type.python_type is uuid.UUID
    except NotImplementedError:
        return False


async def rekey_agent(
    session: AsyncSession, *, tenant_id: uuid.UUID, old_id: str, new_id: str
) -> int:
    """Point every `REKEYED` row at `new_id`; returns rows moved. Re-running moves none.

    Text columns hold either the provider id or the derived uuid as text, so both
    spellings are mapped.
    """
    old_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=old_id)
    new_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=new_id)
    moved = 0
    for table_name, column_name in sorted(REKEYED):
        table: Table = Base.metadata.tables[table_name]
        column = table.c[column_name]
        if _is_uuid(column):
            match = column == old_uuid
            value: object = new_uuid
        else:
            match = column.in_((old_id, str(old_uuid)))
            value = case((column == old_id, new_id), else_=str(new_uuid))
        if "tenant_id" in table.c:
            match = and_(match, table.c.tenant_id == tenant_id)
        result = await session.execute(update(table).where(match).values({column_name: value}))
        moved += int(getattr(result, "rowcount", 0) or 0)
    return moved


async def open_agent_work(session: AsyncSession, *, tenant_id: uuid.UUID, agent_id: str) -> int:
    """Wakes, host-call cards, helper asks and long jobs still in flight for the agent."""
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=agent_id)
    spellings = (agent_id, str(agent_uuid))
    tables = Base.metadata.tables
    wakes, cards, asks, jobs = (
        tables["task_continuations"],
        tables["host_call_cards"],
        tables["helper_asks"],
        tables["long_job"],
    )
    checks = (
        select(func.count())
        .select_from(wakes)
        .where(
            wakes.c.tenant_id == tenant_id,
            wakes.c.target_ma_agent_id.in_(spellings),
            wakes.c.status.in_(("pending", "claimed")),
        ),
        select(func.count())
        .select_from(cards)
        .where(
            cards.c.tenant_id == tenant_id,
            or_(
                cards.c.executing_agent_id == agent_uuid,
                cards.c.executing_ma_agent_id.in_(spellings),
            ),
            cards.c.status.in_(("pending", "approved", "executing")),
        ),
        select(func.count())
        .select_from(asks)
        .where(
            asks.c.tenant_id == tenant_id,
            asks.c.helper_agent_id.in_(spellings),
            asks.c.status.in_(("queued", "running")),
        ),
        select(func.count())
        .select_from(jobs)
        .where(
            jobs.c.tenant_id == tenant_id,
            jobs.c.agent_id.in_(spellings),
            jobs.c.state.in_(("queued", "launching", "running")),
        ),
    )
    total = 0
    for check in checks:
        total += int(await session.scalar(check) or 0)
    return total
