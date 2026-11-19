"""Move a tenant's live references from one agent name to another.

Used by the defaults rename step when a seeded agent is renamed in place.
Rows that reference an agent by id already follow it. Audit and history rows
(`security_audit_events`, `agent_tool_rule_history`, `channel_changes`,
`long_job_policy_history`, `routine_sandbox_history`), settled or retired
`task_continuations` and the immutable neutral catalog keep the name they
recorded.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, Literal, cast, get_args

from daimon.core._models import Base, TenantAccessPolicyRecord
from daimon.core.ma_metadata_keys import MA_METADATA_KEY_MANAGED, MA_METADATA_KEY_NAME
from daimon.core.stores.agent_avatars import normalize_agent_name
from daimon.core.stores.domain import ContinuationReason
from sqlalchemy import (
    ColumnElement,
    Row,
    Select,
    Table,
    and_,
    exists,
    func,
    literal_column,
    select,
    update,
)
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
    ("dm_agent_picks", "agent_name"),
    ("dm_carry_overs", "agent_name"),
    ("dm_rules", "agent_name"),
    ("host_call_cards", "agent_name"),
)
# Keyed by agent name: a row already under the new name wins, the old one stays.
_KEYED_NAME_COLUMNS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("agent_avatars", "agent_name", ("tenant_id",)),
    ("dm_agent_grants", "agent_name", ("tenant_id",)),
    ("user_skills", "agent_name", ("tenant_id", "principal_id", "name")),
)
_POLICY_NAME_KEYS = ("agent_rules", "agent_channel_pins")

SelfHostedState = Literal["pending", "absent", "taken", "ambiguous"]


def _table(name: str) -> Table:
    return Base.metadata.tables[name]


def _live_rows(table: Table) -> list[ColumnElement[bool]]:
    """Wakes settled or with a retired reason are history: no dispatcher reads them.

    A retired reason also fails the NOT VALID reason check (0169, 0194) on any
    UPDATE, which would roll back the whole rename. `turn_outcomes.origin`, the
    only other NOT VALID check, is on a table this module never updates.
    """
    if table.name != "task_continuations":
        return []
    reasons = get_args(ContinuationReason)
    return [table.c.status.in_(("pending", "claimed")), table.c.reason.in_(reasons)]


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
        where = [table.c.tenant_id == tenant_id, _matches(column, old), *_live_rows(table)]
        await session.execute(update(table).where(*where).values({column_name: new}))
    for table_name, column_name, key_columns in _KEYED_NAME_COLUMNS:
        table = _table(table_name)
        column = table.c[column_name]
        other = table.alias("taken")
        taken = exists().where(
            and_(*(other.c[key] == table.c[key] for key in key_columns)),
            _matches(other.c[column_name], new),
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


async def rename_policy_keys(
    session: AsyncSession, *, tenant_id: uuid.UUID, old: str, new: str
) -> None:
    """Move the access policy's per-agent rules; an existing `new` entry wins.

    Called once the agent holds `new`, so an agent still named `old` keeps its
    rules and never takes another agent's.
    """
    policy = await session.scalar(
        select(TenantAccessPolicyRecord.policy)
        .where(TenantAccessPolicyRecord.tenant_id == tenant_id)
        .with_for_update()
    )
    if policy is None:
        return
    renamed = dict(policy)
    legacy, current = normalize_agent_name(old), normalize_agent_name(new)
    for key in _POLICY_NAME_KEYS:
        rules = cast("dict[str, object] | None", renamed.get(key))
        if not rules or any(normalize_agent_name(name) == current for name in rules):
            continue
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


def _selfhosted_rows(tenant_id: uuid.UUID) -> Select[Any]:
    table = _table("selfhosted_agents")
    return select(table.c.id, table.c.name, table.c.spec).where(
        table.c.tenant_id == tenant_id, table.c.archived_at.is_(None)
    )


def _metadata(spec: dict[str, Any]) -> dict[str, str]:
    return cast("dict[str, str]", spec.get("metadata") or {})


def _managed(spec: dict[str, Any]) -> bool:
    return _metadata(spec).get(MA_METADATA_KEY_MANAGED) == "true"


def _selfhosted_state(
    rows: Sequence[Row[Any]], *, old: str, new: str
) -> tuple[SelfHostedState, Row[Any] | None]:
    """Only a defaults-managed `old` is renamed; a member's own `old` is theirs."""
    legacy = [
        row
        for row in rows
        if normalize_agent_name(row.name) == normalize_agent_name(old) and _managed(row.spec)
    ]
    if not legacy:
        return "absent", None
    if len(legacy) > 1:
        return "ambiguous", None
    if any(normalize_agent_name(row.name) == normalize_agent_name(new) for row in rows):
        return "taken", None
    return "pending", legacy[0]


async def inspect_selfhosted_rename(
    session: AsyncSession, *, tenant_id: uuid.UUID, old: str, new: str
) -> SelfHostedState:
    """What `rename_selfhosted_agent` would do, read without locking."""
    rows = (await session.execute(_selfhosted_rows(tenant_id))).all()
    return _selfhosted_state(rows, old=old, new=new)[0]


async def rename_selfhosted_agent(
    session: AsyncSession, *, tenant_id: uuid.UUID, old: str, new: str
) -> SelfHostedState:
    """Rename the tenant's live managed self-hosted agent `old` in place ("pending" → done).

    A live `new` is never merged into: both are left alone ("taken"). Two live
    records that differ only in case are left alone too ("ambiguous"). The
    revision stays: no turn pins the name, so an in-flight turn is unaffected.
    """
    rows = (await session.execute(_selfhosted_rows(tenant_id).with_for_update())).all()
    state, row = _selfhosted_state(rows, old=old, new=new)
    if row is not None:
        table = _table("selfhosted_agents")
        await session.execute(
            update(table)
            .where(table.c.id == row.id)
            .values(name=new, spec=_renamed_spec(row.spec, new), updated_at=func.now())
        )
    return state


def _renamed_spec(spec: dict[str, Any], new: str) -> dict[str, Any]:
    """`spec` under `new`, with the `daimon_name` its SDK-shaped view reports."""
    metadata = {**_metadata(spec), MA_METADATA_KEY_NAME: new}
    return {**spec, "name": new, "metadata": metadata}


async def list_tenants_with_selfhosted_agent(
    session: AsyncSession, *, name: str
) -> list[uuid.UUID]:
    """Tenants holding a live managed self-hosted agent named `name`, in any case."""
    table = _table("selfhosted_agents")
    managed = table.c.spec["metadata"][MA_METADATA_KEY_MANAGED].astext == "true"
    return list(
        (
            await session.scalars(
                select(table.c.tenant_id)
                .where(table.c.archived_at.is_(None), managed, _matches(table.c.name, name))
                .distinct()
                .order_by(table.c.tenant_id)
            )
        ).all()
    )
