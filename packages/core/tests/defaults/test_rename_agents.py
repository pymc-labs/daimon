"""`daimon defaults apply` renames the seeded `daimon` agent to `default` in place."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from daimon.core.defaults import apply_defaults
from daimon.core.defaults.metadata import (
    MA_METADATA_KEY_MANAGED,
    MA_METADATA_KEY_NAME,
    MA_METADATA_KEY_READER_OF,
    MA_METADATA_KEY_READER_SOURCE,
)
from daimon.core.defaults.rename_agents import rename_seeded_agent
from daimon.core.defaults.report import Action, ApplyReport
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.agent_avatars import get_or_create_avatar
from daimon.core.stores.selfhosted_resources import SelfHostedAgents
from daimon.testing.factories import make_account, make_routine, make_tenant
from daimon.testing.ma_models import ma_agent
from mux.testing.drivers.anthropic.ma import (
    FakeMAState,
    build_fake_anthropic,
    list_response,
    make_fake_ma_handler,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..stores.test_selfhosted_resources import agent_spec, scope

_TENANT = derive_tenant_uuid(platform="cli", workspace_id="local")
_MANAGED = {MA_METADATA_KEY_MANAGED: "true"}


def _tree(root: Path) -> Path:
    (root / "agents").mkdir()
    (root / "agents" / "default.yaml").write_text(
        "name: default\nmodel: claude-sonnet-4-6\nsystem: You are Daimon.\n"
    )
    return root


def _seed(state: FakeMAState, *agents: object) -> None:
    for agent in agents:
        dumped = agent.model_dump(mode="json")  # type: ignore[attr-defined]  # BetaManagedAgentsAgent
        state.agents[dumped["id"]] = dumped


def _legacy_daimon() -> object:
    return ma_agent(id="ag_daimon", name="daimon", tenant_id=_TENANT, metadata=_MANAGED)


async def _selfhosted(
    session: AsyncSession, factory: async_sessionmaker[AsyncSession], *names: str
) -> None:
    """The cli tenant with a routine on `daimon` and live self-hosted records `names`."""
    tenant = await make_tenant(session, platform="cli", workspace_id="local")
    account = await make_account(session, tenant=tenant)
    await make_routine(session, tenant=tenant, agent_name="daimon")
    await session.commit()
    agents = SelfHostedAgents(factory)
    for name in names:
        await agents.create(scope(str(_TENANT), str(account.id)), agent_spec(name), key=name)


async def _names(session: AsyncSession, sql: str) -> list[str]:
    return list((await session.execute(text(sql), {"t": _TENANT})).scalars())


def _live(state: FakeMAState) -> dict[str, dict[str, object]]:
    return {
        str(agent["name"]): agent for agent in state.agents.values() if not agent.get("archived_at")
    }


def _actions(report: ApplyReport) -> list[tuple[str, Action]]:
    return [(outcome.name, outcome.action) for outcome in report.agents]


async def _apply(
    factory: async_sessionmaker[AsyncSession],
    state: FakeMAState,
    root: Path,
    *,
    dry_run: bool,
    refuse_update: str | None = None,
) -> ApplyReport:
    agents = make_fake_ma_handler(state)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/environments":
            return list_response([])
        if request.method == "POST" and request.url.path == f"/v1/agents/{refuse_update}":
            return httpx.Response(400, json={"type": "error", "error": {"message": "refused"}})
        return agents(request)

    client = build_fake_anthropic(handler)
    return await apply_defaults(factory, client, root, dry_run=dry_run, run_preflight=False)


async def test_apply_when_fresh_tenant_then_creates_default_without_rename(
    tmp_path: Path, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    state = FakeMAState()
    report = await _apply(db_session_factory, state, _tree(tmp_path), dry_run=False)
    assert not report.is_failure(), f"a fresh apply succeeds: {report}"
    assert ("default", Action.RENAMED) not in _actions(report), (
        "nothing to rename on a fresh tenant"
    )
    assert list(_live(state)) == ["default"], "a fresh tenant gets one agent named default"


async def test_apply_when_seeded_daimon_exists_then_renames_it_in_place(
    tmp_path: Path,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    root = _tree(tmp_path)
    await _apply(db_session_factory, FakeMAState(), root, dry_run=True)  # provisions the tenant row
    await get_or_create_avatar(db_session, tenant_id=_TENANT, agent_name="daimon")
    await db_session.commit()
    reader = ma_agent(
        id="ag_reader",
        name="daimon-reader",
        tenant_id=_TENANT,
        metadata={
            MA_METADATA_KEY_READER_OF: "ag_daimon",
            MA_METADATA_KEY_READER_SOURCE: "daimon",
        },
    )
    copy = ma_agent(id="ag_copy", name="daimon-copy", tenant_id=_TENANT)
    state = FakeMAState()
    _seed(state, _legacy_daimon(), reader, copy)

    report = await _apply(db_session_factory, state, root, dry_run=False)

    assert not report.is_failure(), f"the upgrade apply succeeds: {report}"
    assert ("default", Action.RENAMED) in _actions(report), "the report names the rename"
    live = _live(state)
    assert sorted(live) == ["daimon-copy", "default", "default-reader"], (
        f"daimon and its reader are renamed, the copy is untouched and nothing is created: {live}"
    )
    renamed = live["default"]
    assert renamed["id"] == "ag_daimon", "the same agent keeps its id, sessions and threads"
    metadata = renamed["metadata"]
    assert isinstance(metadata, dict)
    assert metadata[MA_METADATA_KEY_NAME] == "default", "the daimon tag follows the rename"
    assert metadata[MA_METADATA_KEY_MANAGED] == "true", "the agent stays managed"
    reader_metadata = live["default-reader"]["metadata"]
    assert isinstance(reader_metadata, dict)
    assert reader_metadata[MA_METADATA_KEY_NAME] == "default-reader", "the reader tag is renamed"
    assert reader_metadata[MA_METADATA_KEY_READER_SOURCE] == "default", (
        "the reader points at its renamed source"
    )
    avatars = (
        await db_session.execute(
            text("SELECT agent_name FROM agent_avatars WHERE tenant_id = :t"), {"t": _TENANT}
        )
    ).scalars()
    assert list(avatars) == ["default"], "the avatar row follows the agent"

    again = await _apply(db_session_factory, state, root, dry_run=False)
    assert not again.is_failure(), f"a re-run succeeds: {again}"
    assert ("default", Action.RENAMED) not in _actions(again), "a re-run renames nothing"
    assert sorted(_live(state)) == sorted(live), "a re-run creates and archives nothing"


async def test_apply_when_workspace_has_own_default_then_fails_and_leaves_both(
    tmp_path: Path,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _selfhosted(db_session, db_session_factory)
    own = ma_agent(id="ag_own", name="default", tenant_id=_TENANT)
    state = FakeMAState()
    _seed(state, _legacy_daimon(), own)

    report = await _apply(db_session_factory, state, _tree(tmp_path), dry_run=False)

    assert report.is_failure(), "a name collision fails the tenant loudly"
    failed = [o for o in report.agents if o.action is Action.FAILED]
    assert failed and "already has its own agent named 'default'" in (failed[0].error or ""), (
        f"the failure says what to do: {failed}"
    )
    live = _live(state)
    assert live["daimon"]["id"] == "ag_daimon", "the seeded agent keeps its name"
    assert live["default"]["id"] == "ag_own", "the workspace's own agent is not taken over"
    assert len(state.agents) == 2, "no agent is created or archived"
    routines = await _names(db_session, "SELECT agent_name FROM routines WHERE tenant_id = :t")
    assert routines == ["daimon"], "database references are unchanged"


async def test_apply_when_dry_run_then_reports_rename_without_writing(
    tmp_path: Path, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    state = FakeMAState()
    _seed(state, _legacy_daimon())

    report = await _apply(db_session_factory, state, _tree(tmp_path), dry_run=True)

    assert ("default", Action.RENAMED) in _actions(report), "a dry run reports the pending rename"
    assert list(_live(state)) == ["daimon"], "a dry run renames and creates nothing"


@pytest.mark.parametrize("own_daimon", [True, False])
async def test_apply_when_reader_source_is_not_seeded_then_reader_stays(
    own_daimon: bool, tmp_path: Path, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    reader = ma_agent(
        id="ag_reader",
        name="daimon-reader",
        tenant_id=_TENANT,
        metadata={MA_METADATA_KEY_READER_SOURCE: "daimon"},
    )
    state = FakeMAState()
    seeded = ma_agent(
        id="ag_daimon", name="daimon", tenant_id=_TENANT, metadata=None if own_daimon else _MANAGED
    )
    _seed(state, seeded, reader)

    report = await _apply(db_session_factory, state, _tree(tmp_path), dry_run=False)

    assert not report.is_failure(), f"apply succeeds: {report}"
    live = _live(state)
    if own_daimon:
        assert {"daimon", "daimon-reader", "default"} == set(live), (
            "a workspace's own daimon keeps its name and its reader"
        )
    else:
        assert {"default", "default-reader"} == set(live), "the seeded agent's reader follows it"


@pytest.mark.parametrize("name", ["default", "Default"])
async def test_apply_when_only_own_default_exists_then_fails_without_adopting_it(
    name: str, tmp_path: Path, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    own = ma_agent(id="ag_own", name=name, tenant_id=_TENANT)
    state = FakeMAState()
    _seed(state, own)

    report = await _apply(db_session_factory, state, _tree(tmp_path), dry_run=False)

    assert report.is_failure(), "a workspace's own default blocks the seeded agent"
    live = _live(state)
    assert list(live) == [name], f"nothing is created or archived: {live}"
    metadata = live[name]["metadata"]
    assert isinstance(metadata, dict)
    assert MA_METADATA_KEY_MANAGED not in metadata, "the workspace's agent is never stamped managed"


async def test_apply_when_seeded_and_own_default_exist_then_keeps_both(
    tmp_path: Path, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    seeded = ma_agent(id="ag_seeded", name="default", tenant_id=_TENANT, metadata=_MANAGED)
    own = ma_agent(id="ag_own", name="default", tenant_id=_TENANT)
    state = FakeMAState()
    _seed(state, seeded, own)

    report = await _apply(db_session_factory, state, _tree(tmp_path), dry_run=False)

    assert not report.is_failure(), f"the seeded agent is reconciled: {report}"
    live = sorted(str(a["id"]) for a in state.agents.values() if not a.get("archived_at"))
    assert live == ["ag_own", "ag_seeded"], "the workspace's agent is neither adopted nor archived"


@pytest.mark.parametrize(
    "names", [("daimon", "default"), ("Daimon", "daimon")], ids=["taken", "case-duplicates"]
)
async def test_apply_when_selfhosted_rename_unsafe_then_fails_and_moves_nothing(
    names: tuple[str, str],
    tmp_path: Path,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _selfhosted(db_session, db_session_factory, *names)
    state = FakeMAState()
    _seed(state, _legacy_daimon())

    report = await _apply(db_session_factory, state, _tree(tmp_path), dry_run=False)

    assert report.is_failure(), f"self-hosted records {names} block the rename"
    assert list(_live(state)) == ["daimon"], "the seeded agent keeps its name on the provider"
    routines = await _names(db_session, "SELECT agent_name FROM routines WHERE tenant_id = :t")
    assert routines == ["daimon"], "database references are unchanged"
    records = await _names(
        db_session,
        "SELECT name FROM selfhosted_agents WHERE tenant_id = :t AND archived_at IS NULL ORDER BY 1",
    )
    assert records == sorted(names), "both self-hosted records keep their names"


async def test_rename_when_nothing_pending_then_returns_without_taking_the_lock(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _selfhosted(db_session, db_session_factory, "default")
    state = FakeMAState()
    _seed(state, ma_agent(id="ag_seeded", name="default", tenant_id=_TENANT, metadata=_MANAGED))
    key = f"test-lock:{_TENANT}"

    async with db_session_factory() as holder, holder.begin():
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key}
        )
        outcome = await asyncio.wait_for(
            rename_seeded_agent(
                build_fake_anthropic(make_fake_ma_handler(state)),
                db_session_factory,
                tenant_id=_TENANT,
                old="daimon",
                new="default",
                lock_key=key,
                dry_run=False,
            ),
            timeout=10,
        )

    assert outcome is None, "a migrated tenant has nothing to rename"


async def test_apply_when_new_reader_exists_then_fails_and_moves_nothing(
    tmp_path: Path,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _selfhosted(db_session, db_session_factory)
    reader = {**_MANAGED, MA_METADATA_KEY_READER_SOURCE: "daimon"}
    state = FakeMAState()
    _seed(
        state,
        _legacy_daimon(),
        ma_agent(id="ag_reader", name="daimon-reader", tenant_id=_TENANT, metadata=reader),
        ma_agent(id="ag_taken", name="default-reader", tenant_id=_TENANT),
    )

    report = await _apply(db_session_factory, state, _tree(tmp_path), dry_run=False)

    failed = [o for o in report.agents if o.action is Action.FAILED]
    assert failed and "'default-reader'" in (failed[0].error or ""), f"names the reader: {failed}"
    assert set(_live(state)) == {"daimon", "daimon-reader", "default-reader"}, (
        "no agent is renamed, created or archived"
    )
    routines = await _names(db_session, "SELECT agent_name FROM routines WHERE tenant_id = :t")
    assert routines == ["daimon"], "database references are unchanged"


async def test_apply_when_provider_rename_fails_after_commit_then_rerun_finishes(
    tmp_path: Path,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _selfhosted(db_session, db_session_factory)
    root = _tree(tmp_path)
    state = FakeMAState()
    _seed(state, _legacy_daimon())

    failed = await _apply(db_session_factory, state, root, dry_run=False, refuse_update="ag_daimon")

    assert failed.is_failure(), "a provider refusal fails the apply"
    assert list(_live(state)) == ["daimon"], "the agent keeps its old name and nothing is created"
    routines = await _names(db_session, "SELECT agent_name FROM routines WHERE tenant_id = :t")
    assert routines == ["default"], "the database references committed first"

    again = await _apply(db_session_factory, state, root, dry_run=False)

    assert not again.is_failure(), f"the re-run succeeds: {again}"
    assert ("default", Action.RENAMED) in _actions(again), "the re-run finishes the rename"
    live = _live(state)
    assert list(live) == ["default"] and live["default"]["id"] == "ag_daimon", (
        f"the same agent now holds the new name: {live}"
    )
