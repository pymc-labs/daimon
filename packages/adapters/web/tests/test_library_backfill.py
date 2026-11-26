"""The library backfill lists the account once and runs once per workspace, again if a skill was skipped."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from daimon.adapters.web import server_skills


async def test_the_backfill_lists_once_and_retries_only_a_skipped_workspace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenants = [SimpleNamespace(id=uuid.uuid4(), platform="discord") for _ in range(2)]
    listed = 0
    recorded: list[uuid.UUID] = []

    async def list_tenants(_sessionmaker: Any) -> list[Any]:
        return tenants

    async def resolve(*_a: Any, **_k: Any) -> Any:
        return SimpleNamespace(on=lambda _key: True)

    async def list_account(_edit: Any) -> list[Any]:
        nonlocal listed
        listed += 1
        return []

    async def record(_edit: Any, *, writer: Any, rows: Any) -> int:
        recorded.append(writer.tenant_id)
        return 1 if writer.tenant_id == tenants[0].id else 0

    async def nothing(*_a: Any, **_k: Any) -> None:
        return None

    async def no_mirrors(_session: Any) -> list[Any]:
        return []

    class _Sessionmaker:
        def __call__(self) -> Any:
            return self

        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_a: Any) -> None:
            return None

    monkeypatch.setattr(server_skills, "_backfilled", set())
    monkeypatch.setattr(server_skills, "list_tenants_by_platform", list_tenants)
    monkeypatch.setattr(server_skills, "resolve_features", resolve)
    monkeypatch.setattr(server_skills, "list_account_skills", list_account)
    monkeypatch.setattr(server_skills, "record_library_skills", record)
    monkeypatch.setattr(server_skills, "_declared_repos", nothing)
    monkeypatch.setattr(server_skills, "change_deps", lambda _rt: SimpleNamespace(edit=object()))
    monkeypatch.setattr(server_skills.store, "list_mirrors", no_mirrors)
    runtime: Any = SimpleNamespace(sessionmaker=_Sessionmaker(), settings=None)

    for _ in range(3):
        await server_skills.sync_source_skills(runtime)

    assert listed == 3, "one listing per pass, shared by the workspaces still pending"
    # The workspace with an unreadable skill is tried again; the other is done.
    assert recorded == [tenants[0].id, tenants[1].id, tenants[0].id, tenants[0].id]
