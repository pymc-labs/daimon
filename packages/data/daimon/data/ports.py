"""What the store registers with core at startup (the `daimon.ports` entry point).

Erasure statements, the `data` audit source, whether a workspace has a source, putting a
private form's credential onto a source, the scopes an audience holds and the declared skill
repositories; M5 fills `GATED_TOOLS`. Everything here is read only where the package is
installed and does nothing for a deployment that never set the store up: erasure skips a
schema that does not exist, the audit source reads no rows.
"""

from __future__ import annotations

import uuid
from typing import Final

import psycopg
from daimon.core.config import Settings, load_data_settings
from daimon.core.data_audience import DataAudience
from daimon.core.ports.audit import register_audit_source
from daimon.core.ports.data_credentials import SourceCredentialRefused, register_set_credential
from daimon.core.ports.data_ready import Sessions, register_has_sources
from daimon.core.ports.data_scopes import register_held_scopes
from daimon.core.ports.erasure import ErasureStatement, register_erasure
from daimon.core.ports.skill_repos import register_skill_repo_loader
from daimon.core.ports.tool_gate import register_gated_tools
from daimon.data import manage, pool
from daimon.data.audit import AUDIT_SOURCE
from daimon.data.declared import DeclaredSkillRepos
from daimon.data.identity import IdentityError
from daimon.data.migrate import STORE_FILES
from daimon.data.reading import open_read
from daimon.data.schema import Schemas
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

_UNREACHABLE: Final = "The data store can't be reached. Nothing was saved."
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
                ("identity_notices", "account_id"),
                ("person_connections", "account_id"),
                ("connect_pending", "account_id"),
            )
        ),
        account(
            "decided_by",
            f"update {d}.link_decisions set decided_by = null where decided_by = :account_id",
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
                "identity_notices",
                "redaction_sets",
            )
        ),
        tenant("audit", f"delete from {d}.audit where tenant_id = :tenant_id"),
    )


async def _has_sources(sessions: Sessions, settings: Settings, tenant_id: uuid.UUID) -> bool:
    """A live source in a store with every file applied; a missing store reads as none."""
    d = Schemas.of(settings.data).data
    try:
        async with sessions() as session:
            found = await session.scalar(
                text(
                    f"select (select count(*) from {d}.schema_version where file = any(:files))"
                    f" = :count and exists (select 1 from {d}.sources where tenant_id = :tenant"
                    " and publication <> 'retired')"
                ),
                {"files": list(STORE_FILES), "count": len(STORE_FILES), "tenant": tenant_id},
            )
    except SQLAlchemyError:
        return False
    return bool(found)


async def _held_scopes(
    settings: Settings, tenant_id: uuid.UUID, audience: DataAudience
) -> frozenset[str]:
    """The scopes `audience` may read together, as the data tools resolve them."""
    async with pool.connection(settings) as conn:
        read = await open_read(conn, Schemas.of(settings.data), tenant_id, audience, salt="skills")
    return frozenset(read.ctx.scopes)


async def _set_credential(
    settings: Settings, tenant_id: uuid.UUID, source_id: uuid.UUID, by: uuid.UUID, value: str
) -> None:
    keys = tuple(k.get_secret_value() for k in settings.crypto.keys)
    try:
        async with pool.connection(settings) as conn:
            await manage.set_credential(
                conn, Schemas.of(settings.data), keys, tenant_id=tenant_id, by=by,
                source_id=source_id, value=value,
            )  # fmt: skip
    except IdentityError as e:
        raise SourceCredentialRefused(f"{e} Nothing was saved.") from None
    except psycopg.Error:
        raise SourceCredentialRefused(
            "The data store can't be reached. Nothing was saved."
        ) from None


def register() -> None:
    register_has_sources(_has_sources)
    register_held_scopes(_held_scopes)
    register_set_credential(_set_credential)
    register_erasure(erasure(Schemas.of(load_data_settings())))
    register_audit_source("data", AUDIT_SOURCE)
    register_skill_repo_loader(DeclaredSkillRepos)
    for key, tools in GATED_TOOLS.items():
        register_gated_tools(key, tools)
