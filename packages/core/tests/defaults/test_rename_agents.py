"""`daimon defaults apply` renames the seeded `daimon` agent to `default` in place."""

from __future__ import annotations

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
from daimon.core.defaults.report import Action, ApplyReport
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.agent_avatars import get_or_create_avatar
from daimon.testing.ma_models import ma_agent
from mux.testing.drivers.anthropic.ma import (
    FakeMAState,
    build_fake_anthropic,
    list_response,
    make_fake_ma_handler,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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


def _live(state: FakeMAState) -> dict[str, dict[str, object]]:
    return {
        str(agent["name"]): agent for agent in state.agents.values() if not agent.get("archived_at")
    }


def _actions(report: ApplyReport) -> list[tuple[str, Action]]:
    return [(outcome.name, outcome.action) for outcome in report.agents]


async def _apply(
    factory: async_sessionmaker[AsyncSession], state: FakeMAState, root: Path, *, dry_run: bool
) -> ApplyReport:
    agents = make_fake_ma_handler(state)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/environments":
            return list_response([])
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
    tmp_path: Path, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
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
