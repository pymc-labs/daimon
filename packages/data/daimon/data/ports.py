"""What the store registers with core at startup (the `daimon.ports` entry point).

Erasure statements, the `data` audit source and whether a workspace has a source; M5 fills
`GATED_TOOLS`. Everything here is
read only where the package is installed and does nothing for a deployment that never set
the store up: erasure skips a schema that does not exist, the audit source reads no rows.
"""

from __future__ import annotations

import uuid
from typing import Final

import psycopg
from daimon.core.config import Settings, load_data_settings
from daimon.core.ports.audit import register_audit_source
from daimon.core.ports.data_ready import register_has_sources
from daimon.core.ports.erasure import ErasureStatement, register_erasure
from daimon.core.ports.tool_gate import register_gated_tools
from daimon.data import pool
from daimon.data.audit import AUDIT_SOURCE
from daimon.data.schema import Schemas

GATED_TOOLS: Final[dict[str, frozenset[str]]] = {}

_PERSON: Final = "'account:' || cast(cast(:account_id as uuid) as text)"
# Lets these deletes past the bound-row guard (0006); set per row, so only where rows match.
_ERASING: Final = "set_config('daimon_data.erasing', 'on', true) = 'on' and"


def erasure(schemas: Schemas) -> tuple[ErasureStatement, ...]:
    """A person's own sources (and so their records), scope grants, links, connections and
    trail details, then their place in every access list and a tombstone no later write passes;
    a tenant's sources, scopes, people and trail. Records of shared sources stay: they are the
    source's, and a source identity once linked to the person stays, unlinked."""
    d = schemas.data

    def account(key: str, sql: str) -> ErasureStatement:
        return ErasureStatement(f"data.{key}", sql, "account", d)

    def tenant(key: str, sql: str) -> ErasureStatement:
        return ErasureStatement(f"data.tenant.{key}", sql, "tenant", d)

    return (
        account(
            "sources", f"delete from {d}.sources where {_ERASING} owner_account_id = :account_id"
        ),
        account(
            "created_by", f"update {d}.sources set created_by = null where created_by = :account_id"
        ),
        account("grants", f"delete from {d}.grants where {_ERASING} holder = {_PERSON}"),
        account("acl_refs", f"delete from {d}.acl_refs where ref = {_PERSON}"),
        account("group_members", f"delete from {d}.group_members where subject = {_PERSON}"),
        account("acl_grants", f"delete from {d}.acl_grants where holder = {_PERSON}"),
        account("holder_sets", f"delete from {d}.holder_sets where holder = {_PERSON}"),
        account(
            "granted_by", f"update {d}.grants set granted_by = null where granted_by = :account_id"
        ),
        account(
            "audit",
            f"update {d}.audit set account_id = null, target = null, detail = '{{}}'"
            " where account_id = :account_id",
        ),
        account(
            "audit_target",
            f"update {d}.audit set target = null, detail = '{{}}' where target = {_PERSON}",
        ),
        account(
            "identities",
            f"update {d}.identities set account_id = null, proof = null, linked_by = null,"
            " linked_at = null where account_id = :account_id",
        ),
        account(
            "linked_by", f"update {d}.identities set linked_by = null where linked_by = :account_id"
        ),
        *(
            account(f"{table}.{col}", f"delete from {d}.{table} where {col} = :account_id")
            for table, col in (
                ("link_decisions", "account_id"),
                ("data_admins", "account_id"),
                ("identity_notices", "account_id"),
                ("person_connections", "account_id"),
                ("connect_pending", "account_id"),
                ("personal_off", "account_id"),
            )
        ),
        account(
            "decided_by",
            f"update {d}.link_decisions set decided_by = null where decided_by = :account_id",
        ),
        account(
            "admin_granted_by",
            f"update {d}.data_admins set granted_by = null where granted_by = :account_id",
        ),
        account(
            "audit_detail",
            f"update {d}.audit set detail = detail - 'account'"
            " where detail ->> 'account' = cast(cast(:account_id as uuid) as text)",
        ),
        account("forget", f"select {d}.forget_person(cast(:account_id as uuid))"),
        tenant("sources", f"delete from {d}.sources where {_ERASING} tenant_id = :tenant_id"),
        tenant("scopes", f"delete from {d}.scopes where {_ERASING} tenant_id = :tenant_id"),
        *(
            tenant(table, f"delete from {d}.{table} where tenant_id = :tenant_id")
            for table in (
                "identities",
                "link_decisions",
                "data_admins",
                "identity_notices",
                "redaction_sets",
                "personal_off",
            )
        ),
        tenant("audit", f"delete from {d}.audit where tenant_id = :tenant_id"),
    )


async def _has_sources(settings: Settings, tenant_id: uuid.UUID) -> bool:
    """A live source in a ready store; a store error reads as none."""
    if not await pool.ready(settings):
        return False
    d = Schemas.of(settings.data).data
    try:
        async with pool.connection(settings) as conn:
            cur = await conn.execute(
                f"select exists (select 1 from {d}.sources where tenant_id = %s"
                f" and publication <> 'retired') as found".encode(),
                (tenant_id,),
            )
            row = await cur.fetchone()
    except psycopg.Error:
        return False
    return row is not None and bool(row["found"])


def register() -> None:
    register_has_sources(_has_sources)
    register_erasure(erasure(Schemas.of(load_data_settings())))
    register_audit_source("data", AUDIT_SOURCE)
    for key, tools in GATED_TOOLS.items():
        register_gated_tools(key, tools)
