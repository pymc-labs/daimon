"""The seams an installed package fills: erasure statements, gated tools, audit sources."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from daimon.core import audit_trail
from daimon.core.features import tool_gate
from daimon.core.ports import erasure
from daimon.core.ports.audit import AuditEntry, AuditSourceSpec, SourceAuditRow
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def test_registered_tools_are_gated_by_their_own_feature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tool_gate, "_REGISTERED", {})
    tool_gate.register_gated_tools("data_sources", ["search_the_store"])
    assert "data_sources" in tool_gate.gated_keys()
    assert "search_the_store" in tool_gate.gated_tools(["data_sources"])
    assert tool_gate.server_gate_key("search_the_store") == "data_sources"
    with pytest.raises(ValueError, match="already gates"):
        tool_gate.register_gated_tools(tool_gate.GATED_KEYS[0], [])


async def test_a_registered_audit_source_joins_the_trail(monkeypatch: pytest.MonkeyPatch) -> None:
    row = SourceAuditRow(uuid.uuid4(), datetime(2026, 1, 1, tzinfo=UTC), "sync", "docs")

    async def read(*_: object) -> list[AuditEntry]:
        return [AuditEntry("data", row.id, row.at, None, row.kind, row)]

    async def kinds(*_: object) -> list[str]:
        return ["sync"]

    monkeypatch.setattr(audit_trail, "_SOURCES", dict(audit_trail._SOURCES))
    audit_trail.register_audit_source("data", AuditSourceSpec(read, kinds))
    with pytest.raises(ValueError, match="built-in"):
        audit_trail.register_audit_source("web", AuditSourceSpec(read, kinds))
    entries = await audit_trail.list_entries(
        None,  # pyright: ignore[reportArgumentType]
        tenant_id=uuid.uuid4(),
        query=audit_trail.AuditFilter(source="data"),
        limit=10,
    )
    assert [e.row for e in entries] == [row]


@pytest.mark.fresh_schema
async def test_erasure_runs_only_where_its_schema_exists(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(erasure, "_REGISTERED", {})
    erasure.register_erasure(
        [
            erasure.ErasureStatement(
                "t", "delete from ports_probe.t where owner = :account_id", "account", "ports_probe"
            )
        ]
    )
    account = uuid.uuid4()
    assert await erasure.erase_account(db_session, account_id=account) == 0
    await db_session.execute(text("create schema ports_probe"))
    await db_session.execute(text("create table ports_probe.t (owner uuid)"))
    await db_session.execute(text("insert into ports_probe.t values (:a), (:a)"), {"a": account})
    assert await erasure.erase_account(db_session, account_id=account) == 2
    assert await erasure.erase_tenant(db_session, tenant_id=account) == 0
