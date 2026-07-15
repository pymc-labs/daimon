"""Tests for daimon.core.mcp_vault_janitor."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import httpx
import pytest
from anthropic import AsyncAnthropic
from anthropic.types.beta import BetaManagedAgentsVault
from daimon.core.mcp_vault_janitor import (
    archive_orphan_mcp_vaults,
    partition_orphan_vault_ids,
)
from daimon.testing.factories import make_account, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _vault(vault_id: str, display_name: str) -> BetaManagedAgentsVault:
    """Inline construction so SDK field drift breaks the test, per testing guidelines."""
    now = dt.datetime(2026, 5, 1, tzinfo=dt.UTC)
    return BetaManagedAgentsVault(
        id=vault_id,
        type="vault",
        display_name=display_name,
        metadata={},
        archived_at=None,
        created_at=now,
        updated_at=now,
    )


def _make_client(handler: httpx.MockTransport) -> AsyncAnthropic:
    return AsyncAnthropic(
        api_key="sk-test",
        http_client=httpx.AsyncClient(transport=handler),
    )


def test_partition_returns_vaults_whose_account_is_not_live() -> None:
    """Vault with parsed UUID not in live_account_ids must be classified orphan."""
    live = uuid.uuid4()
    orphan = uuid.uuid4()
    vaults = [
        _vault("vlt_live", f"daimon-mcp:{live}"),
        _vault("vlt_orphan", f"daimon-mcp:{orphan}"),
    ]
    orphans, unparseable = partition_orphan_vault_ids(vaults, live_account_ids={live})
    assert orphans == ["vlt_orphan"], "live account's vault must survive"
    assert unparseable == [], "both display names parse cleanly"


def test_partition_ignores_non_daimon_mcp_vaults() -> None:
    """Anthropic-built-in vaults or operator-named vaults are out of scope."""
    live = uuid.uuid4()
    vaults = [
        _vault("vlt_other", "something-else"),
        _vault("vlt_built_in", "anthropic-builtin-vault"),
        _vault("vlt_live", f"daimon-mcp:{live}"),
    ]
    orphans, unparseable = partition_orphan_vault_ids(vaults, live_account_ids={live})
    assert orphans == [], "non-daimon-mcp vaults must never be classified orphan"
    assert unparseable == [], "non-daimon-mcp vaults must not appear in unparseable either"


def test_partition_flags_unparseable_display_names_separately() -> None:
    """daimon-mcp:<non-uuid> is suspicious but not safely orphan — log, don't archive."""
    vaults = [_vault("vlt_garbage", "daimon-mcp:not-a-uuid")]
    orphans, unparseable = partition_orphan_vault_ids(vaults, live_account_ids=set())
    assert orphans == [], "malformed suffix must NOT auto-archive — operator inspects"
    assert unparseable == ["vlt_garbage"]


def test_partition_per_agent_vault_dead_account_is_orphan() -> None:
    """Per-agent vault daimon-mcp:{account}:{agent} with dead account must be orphan."""
    dead_account = uuid.uuid4()
    agent = uuid.uuid4()
    vaults = [_vault("vlt_per_agent_dead", f"daimon-mcp:{dead_account}:{agent}")]
    orphans, unparseable = partition_orphan_vault_ids(vaults, live_account_ids=set())
    assert orphans == ["vlt_per_agent_dead"], "per-agent vault with dead account must be orphan"
    assert unparseable == [], "per-agent display name must parse cleanly"


def test_partition_per_agent_vault_live_account_is_kept() -> None:
    """Per-agent vault daimon-mcp:{account}:{agent} with live account must be kept."""
    live_account = uuid.uuid4()
    agent = uuid.uuid4()
    vaults = [_vault("vlt_per_agent_live", f"daimon-mcp:{live_account}:{agent}")]
    orphans, unparseable = partition_orphan_vault_ids(vaults, live_account_ids={live_account})
    assert orphans == [], "per-agent vault with live account must not be orphaned"
    assert unparseable == [], "per-agent display name must parse cleanly"


def test_partition_per_agent_vault_nonuuid_first_segment_is_unparseable() -> None:
    """daimon-mcp:not-a-uuid:whatever has non-UUID first segment — must be unparseable."""
    vaults = [_vault("vlt_bad_segment", "daimon-mcp:not-a-uuid:whatever")]
    orphans, unparseable = partition_orphan_vault_ids(vaults, live_account_ids=set())
    assert orphans == [], "non-UUID first segment must NOT auto-archive"
    assert unparseable == ["vlt_bad_segment"], "non-UUID first segment must be unparseable"


@pytest.mark.asyncio
async def test_archive_orphan_dry_run_lists_but_does_not_mutate(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as s, s.begin():
        tenant = await make_tenant(s)
        live_account = await make_account(s, tenant=tenant)
        live_account_id = live_account.id
    orphan_account_id = uuid.uuid4()

    archived: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/v1/vaults":
            return httpx.Response(
                200,
                json={
                    "data": [
                        _vault("vlt_live", f"daimon-mcp:{live_account_id}").model_dump(mode="json"),
                        _vault("vlt_orphan", f"daimon-mcp:{orphan_account_id}").model_dump(
                            mode="json"
                        ),
                    ],
                    "has_more": False,
                },
            )
        if req.method == "POST" and req.url.path.endswith("/archive"):
            vault_id = req.url.path.split("/")[-2]
            archived.append(vault_id)
            return httpx.Response(200, json={"id": vault_id, "archived": True})
        raise AssertionError(f"unexpected call: {req.method} {req.url}")

    client = _make_client(httpx.MockTransport(handler))
    report = await archive_orphan_mcp_vaults(
        client, session_factory=db_session_factory, dry_run=True
    )

    assert report.orphan_vault_ids == ["vlt_orphan"], "must identify the orphan"
    assert report.archived_vault_ids == [], "dry_run must not archive anything"
    assert archived == [], "no archive HTTP call must fire in dry_run"


@pytest.mark.asyncio
async def test_archive_orphan_apply_archives_only_orphans(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with db_session_factory() as s, s.begin():
        tenant = await make_tenant(s)
        live_account = await make_account(s, tenant=tenant)
        live_account_id = live_account.id
    orphan_a = uuid.uuid4()
    orphan_b = uuid.uuid4()

    archived_ids: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path == "/v1/vaults":
            payload: list[dict[str, Any]] = [
                _vault("vlt_live", f"daimon-mcp:{live_account_id}").model_dump(mode="json"),
                _vault("vlt_orphan_a", f"daimon-mcp:{orphan_a}").model_dump(mode="json"),
                _vault("vlt_orphan_b", f"daimon-mcp:{orphan_b}").model_dump(mode="json"),
                _vault("vlt_unrelated", "unrelated").model_dump(mode="json"),
            ]
            return httpx.Response(200, json={"data": payload, "has_more": False})
        if (
            req.method == "POST"
            and req.url.path.startswith("/v1/vaults/")
            and req.url.path.endswith("/archive")
        ):
            vault_id = req.url.path.split("/")[-2]
            archived_ids.append(vault_id)
            return httpx.Response(200, json={"id": vault_id, "archived": True})
        raise AssertionError(f"unexpected call: {req.method} {req.url}")

    client = _make_client(httpx.MockTransport(handler))
    report = await archive_orphan_mcp_vaults(
        client, session_factory=db_session_factory, dry_run=False
    )

    assert sorted(report.orphan_vault_ids) == ["vlt_orphan_a", "vlt_orphan_b"]
    assert sorted(report.archived_vault_ids) == ["vlt_orphan_a", "vlt_orphan_b"]
    assert sorted(archived_ids) == ["vlt_orphan_a", "vlt_orphan_b"], (
        "must archive exactly the orphan vaults — not the live one, not the unrelated one"
    )
