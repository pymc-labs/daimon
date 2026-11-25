"""What the store registers with core at startup (the `daimon.ports` entry point).

Erasure statements and the `data` audit source; M5 fills `GATED_TOOLS`. Everything here is
read only where the package is installed and does nothing for a deployment that never set
the store up: erasure skips a schema that does not exist, the audit source reads no rows.
"""

from __future__ import annotations

from typing import Final

from daimon.core.config import load_data_settings
from daimon.core.ports.audit import register_audit_source
from daimon.core.ports.erasure import ErasureStatement, register_erasure
from daimon.core.ports.tool_gate import register_gated_tools
from daimon.data.audit import AUDIT_SOURCE
from daimon.data.schema import Schemas

GATED_TOOLS: Final[dict[str, frozenset[str]]] = {}

_PERSON: Final = "'account:' || cast(cast(:account_id as uuid) as text)"


def erasure(schemas: Schemas) -> tuple[ErasureStatement, ...]:
    """A person's own sources (and so their records), scope grants and trail details; a
    tenant's sources, scopes and trail. Records of shared sources stay: they are the source's."""
    d = schemas.data

    def account(key: str, sql: str) -> ErasureStatement:
        return ErasureStatement(f"data.{key}", sql, "account", d)

    def tenant(key: str, sql: str) -> ErasureStatement:
        return ErasureStatement(f"data.tenant.{key}", sql, "tenant", d)

    return (
        account("sources", f"delete from {d}.sources where owner_account_id = :account_id"),
        account(
            "created_by", f"update {d}.sources set created_by = null where created_by = :account_id"
        ),
        account("grants", f"delete from {d}.grants where holder = {_PERSON}"),
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
        tenant("sources", f"delete from {d}.sources where tenant_id = :tenant_id"),
        tenant("scopes", f"delete from {d}.scopes where tenant_id = :tenant_id"),
        tenant("audit", f"delete from {d}.audit where tenant_id = :tenant_id"),
    )


def register() -> None:
    register_erasure(erasure(Schemas.of(load_data_settings())))
    register_audit_source("data", AUDIT_SOURCE)
    for key, tools in GATED_TOOLS.items():
        register_gated_tools(key, tools)
