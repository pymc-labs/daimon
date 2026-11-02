"""Lane workspace ownership is checked before any destructive call."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from daimon.adapters.cli import lane_wipe


class FakeClient:
    """Stands in for the workspace behind one API key; lane_wipe talks to it via daimon.core.ma."""

    def __init__(
        self,
        *,
        agents: list[object] | None = None,
        environments: list[object] | None = None,
        vaults: list[object] | None = None,
        memory_stores: list[object] | None = None,
    ) -> None:
        self.rows = agents or []
        self.other_resources = [*(environments or []), *(vaults or []), *(memory_stores or [])]
        self.include_archived_calls: list[bool] = []


def marker(lane: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=f"daimon-lane-marker-{lane}",
        metadata={"daimon_workspace": "disposable"},
        archived_at=None,
    )


@pytest.fixture(autouse=True)
def workspace_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DAIMON_ENVIRONMENT__NAME", "lane-qa")

    async def list_agents(client: FakeClient, *, include_archived: bool = False) -> list[object]:
        client.include_archived_calls.append(include_archived)
        return list(client.rows)

    async def has_resources(client: FakeClient) -> bool:
        return bool(client.other_resources)

    async def create_agent(
        client: FakeClient, *, name: str, description: str, model: str, metadata: dict[str, str]
    ) -> None:
        assert description
        assert model == "claude-haiku-5-5"
        client.rows.append(SimpleNamespace(name=name, metadata=metadata, archived_at=None))

    monkeypatch.setattr(lane_wipe, "list_workspace_agents_for_lane", list_agents)
    monkeypatch.setattr(lane_wipe, "workspace_has_resources_for_lane", has_resources)
    monkeypatch.setattr(lane_wipe, "create_lane_marker_agent", create_agent)

    async def list_skills(_client: object) -> list[object]:
        return []

    monkeypatch.setattr(lane_wipe, "list_skills_strict", list_skills)


@pytest.mark.asyncio
async def test_non_lane_environment_rejected_before_api_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DAIMON_ENVIRONMENT__NAME", "staging")
    client = FakeClient()
    with pytest.raises(RuntimeError, match="DAIMON_ENVIRONMENT__NAME"):
        await lane_wipe.check_workspace(client, "lane-qa")  # type: ignore[arg-type]
    assert client.rows == []


@pytest.mark.asyncio
async def test_fresh_workspace_is_claimed() -> None:
    client = FakeClient()
    await lane_wipe.check_workspace(client, "lane-qa")  # type: ignore[arg-type]
    assert len(client.rows) == 1
    assert client.rows[0].name == "daimon-lane-marker-qa"
    assert client.rows[0].metadata == {"daimon_workspace": "disposable"}


@pytest.mark.asyncio
async def test_own_marker_is_accepted_without_creating_another() -> None:
    client = FakeClient(agents=[marker("qa"), SimpleNamespace(name="tenant-agent")])
    await lane_wipe.check_workspace(client, "lane-qa")  # type: ignore[arg-type]
    assert len(client.rows) == 2


@pytest.mark.asyncio
async def test_foreign_nonempty_workspace_is_refused() -> None:
    client = FakeClient(agents=[SimpleNamespace(name="staging-agent")])
    with pytest.raises(
        lane_wipe.WorkspaceNotOwned, match="^workspace not owned by lane-qa; refusing$"
    ):
        await lane_wipe.check_workspace(client, "lane-qa")  # type: ignore[arg-type]
    assert len(client.rows) == 1


@pytest.mark.asyncio
async def test_other_lane_marker_is_refused() -> None:
    client = FakeClient(agents=[marker("other")])
    with pytest.raises(lane_wipe.WorkspaceNotOwned):
        await lane_wipe.check_workspace(client, "lane-qa")  # type: ignore[arg-type]
    assert len(client.rows) == 1


@pytest.mark.asyncio
async def test_archived_own_marker_is_refused() -> None:
    archived = marker("qa")
    archived.archived_at = "2026-01-01T00:00:00Z"
    client = FakeClient(agents=[archived])
    with pytest.raises(lane_wipe.WorkspaceNotOwned):
        await lane_wipe.check_workspace(client, "lane-qa")  # type: ignore[arg-type]
    assert client.include_archived_calls == [True]


@pytest.mark.asyncio
async def test_custom_skill_blocks_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    async def list_skills(_client: object) -> list[object]:
        return [SimpleNamespace(source="custom")]

    monkeypatch.setattr(lane_wipe, "list_skills_strict", list_skills)
    client = FakeClient()
    with pytest.raises(lane_wipe.WorkspaceNotOwned):
        await lane_wipe.check_workspace(client, "lane-qa")  # type: ignore[arg-type]
    assert client.rows == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["environments", "vaults", "memory_stores"])
async def test_other_resource_blocks_claim(kind: str) -> None:
    client = FakeClient(**{kind: [object()]})
    with pytest.raises(lane_wipe.WorkspaceNotOwned):
        await lane_wipe.check_workspace(client, "lane-qa")  # type: ignore[arg-type]
    assert client.rows == []


@pytest.mark.asyncio
async def test_wipe_recreates_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(agents=[marker("qa"), SimpleNamespace(name="tenant-agent")])
    calls = 0

    async def delete(fake: FakeClient, *, i_understand_this_destroys_all_tenants: bool) -> None:
        nonlocal calls
        assert i_understand_this_destroys_all_tenants
        calls += 1
        fake.rows.clear()

    monkeypatch.setattr(lane_wipe, "delete_entire_workspace_for_testing", delete)
    await lane_wipe.wipe_workspace(client, "lane-qa")  # type: ignore[arg-type]
    assert calls == 1
    # The ownership check sees archived agents; the post-wipe look does not.
    assert client.include_archived_calls == [True, False]
    assert len(client.rows) == 1
    assert client.rows[0].name == "daimon-lane-marker-qa"
    assert client.rows[0].metadata == {"daimon_workspace": "disposable"}


def test_cli_refusal_has_only_generic_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DAIMON_ENVIRONMENT__NAME", "lane-qa")

    async def refuse(_command: str, _lane: str) -> None:
        raise lane_wipe.WorkspaceNotOwned("workspace not owned by lane-qa; refusing")

    monkeypatch.setattr(lane_wipe, "_run", refuse)
    assert lane_wipe.main(["wipe"]) == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "workspace not owned by lane-qa; refusing\n"
