"""Move a tenant's live references from one agent name to another.

Used by the defaults rename step when a seeded agent is renamed in place.
Rows that reference an agent by id already follow it. Audit and history rows
(`security_audit_events`, `agent_tool_rule_history`, `channel_changes`,
`long_job_policy_history`, `routine_sandbox_history`) and the immutable
neutral catalog keep the name they recorded.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal, cast

from daimon.core._models import Base, TenantAccessPolicyRecord
from daimon.core.stores.agent_avatars import normalize_agent_name
from sqlalchemy import ColumnElement, Table, and_, exists, func, literal_column, select, update
from sqlalchemy.ext.asyncio import AsyncSession

# (table, name column) pairs whose rows hold a tenant's live agent references.
_LIVE_NAME_COLUMNS: tuple[tuple[str, str], ...] = (
    ("platform_principals", "active_agent_name"),
    ("tenant_config", "agent_name"),
    ("channel_config", "agent_name"),
    ("agent_tool_rules", "agent_name"),
    ("routines", "agent_name"),
    ("channel_skills", "owner_agent_name"),
    ("coding_key_claims", "agent_name"),
    ("credential_requests", "target_name"),
    ("credential_requests", "responder_name"),
    ("thread_agent_bindings", "responder_name"),
    ("thread_agent_bindings", "configuration_target_name"),
    ("turn_origins", "responder_name"),
    ("turn_origins", "configuration_target_name"),
    ("helper_asks", "helper_name"),
    ("task_continuations", "target_name"),
    ("github_connect_invitations", "agent_name"),
    ("github_connect_invitations", "origin_responder_name"),
    ("github_connect_click_intents", "agent_name"),
    ("github_connect_click_intents", "origin_responder_name"),
    ("github_connect_requests", "agent_name"),
    ("github_access_requests", "agent_name"),
    ("direct_message_policies", "dm_agent_name"),
    ("backend_switch_cards", "answering_agent_name"),
    ("agent_backend_revision", "agent_name"),
    ("watches", "agent_name"),
    ("long_job", "agent_name"),
    ("compute_sessions", "agent_name"),
    ("web_workspace_settings", "default_agent"),
    ("web_threads", "agent_name"),
)
# Keyed by agent name: a row already under the new name wins, the old one stays.
_KEYED_NAME_COLUMNS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("agent_avatars", "agent_name", ("tenant_id",)),
    ("user_skills", "agent_name", ("tenant_id", "principal_id", "name")),
)
_POLICY_NAME_KEYS = ("agent_rules", "agent_channel_pins")

SelfHostedRename = Literal["renamed", "absent", "taken"]


def _table(name: str) -> Table:
    return Base.metadata.tables[name]


def _matches(column: ColumnElement[Any], name: str) -> ColumnElement[bool]:
    """`normalize_agent_name` in SQL: NFKC, then lower (casefold for agent names)."""
    return func.lower(func.normalize(column, literal_column("NFKC"))) == normalize_agent_name(name)


async def rename_agent_references(
    session: AsyncSession, *, tenant_id: uuid.UUID, old: str, new: str
) -> None:
    """Point every live reference to `old` at `new`. Idempotent."""
    for table_name, column_name in _LIVE_NAME_COLUMNS:
        table = _table(table_name)
        column = table.c[column_name]
        await session.execute(
            update(table)
            .where(table.c.tenant_id == tenant_id, _matches(column, old))
            .values({column_name: new})
        )
    for table_name, column_name, key_columns in _KEYED_NAME_COLUMNS:
        table = _table(table_name)
        column = table.c[column_name]
        other = table.alias("taken")
        taken = exists().where(
            and_(*(other.c[key] == table.c[key] for key in key_columns)),
            other.c[column_name] == new,
        )
        await session.execute(
            update(table)
            .where(table.c.tenant_id == tenant_id, _matches(column, old), ~taken)
            .values({column_name: new})
        )
    user_config, accounts = _table("user_config"), _table("accounts")
    await session.execute(
        update(user_config)
        .where(
            user_config.c.account_id.in_(
                select(accounts.c.id).where(accounts.c.tenant_id == tenant_id)
            ),
            _matches(user_config.c.agent_name, old),
        )
        .values(agent_name=new)
    )
    await _rename_policy_keys(session, tenant_id=tenant_id, old=old, new=new)


async def _rename_policy_keys(
    session: AsyncSession, *, tenant_id: uuid.UUID, old: str, new: str
) -> None:
    """Move the access policy's per-agent rules; an existing `new` entry wins."""
    policy = await session.scalar(
        select(TenantAccessPolicyRecord.policy)
        .where(TenantAccessPolicyRecord.tenant_id == tenant_id)
        .with_for_update()
    )
    if policy is None:
        return
    renamed = dict(policy)
    for key in _POLICY_NAME_KEYS:
        rules = cast("dict[str, object] | None", renamed.get(key))
        if not rules or new in rules:
            continue
        legacy = normalize_agent_name(old)
        renamed[key] = {
            (new if normalize_agent_name(name) == legacy else name): rule
            for name, rule in rules.items()
        }
    if renamed != policy:
        await session.execute(
            update(TenantAccessPolicyRecord)
            .where(TenantAccessPolicyRecord.tenant_id == tenant_id)
            .values(policy=renamed)
        )


async def rename_selfhosted_agent(
    session: AsyncSession, *, tenant_id: uuid.UUID, old: str, new: str
) -> SelfHostedRename:
    """Rename the tenant's live self-hosted agent `old` in place.

    A live `new` is never merged into: both are left alone ("taken").
    """
    table = _table("selfhosted_agents")
    live = and_(table.c.tenant_id == tenant_id, table.c.archived_at.is_(None))
    rows = (
        await session.execute(
            select(table.c.id, table.c.name, table.c.spec).where(live).with_for_update()
        )
    ).all()
    legacy = [row for row in rows if normalize_agent_name(row.name) == normalize_agent_name(old)]
    if not legacy:
        return "absent"
    if any(normalize_agent_name(row.name) == normalize_agent_name(new) for row in rows):
        return "taken"
    for row in legacy:
        await session.execute(
            update(table)
            .where(table.c.id == row.id)
            .values(
                name=new,
                spec={**row.spec, "name": new},
                revision=table.c.revision + 1,
                updated_at=func.now(),
            )
        )
    return "renamed"
