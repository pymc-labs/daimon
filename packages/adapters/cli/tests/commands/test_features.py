"""`daimon features list | get | set | diff` over the core feature service."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from io import StringIO
from typing import Any

import httpx
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.cli.commands import features as features_cmd
from daimon.adapters.cli.commands.features import (
    features_diff,
    features_list,
    features_set,
)
from daimon.adapters.cli.runtime import CliRuntime
from daimon.adapters.cli.tenant import TenantSelector
from daimon.core.config import Settings
from daimon.core.errors import StoreError
from daimon.core.features.registry import FEATURES
from daimon.core.ma_resolver import new_resolver_cache
from daimon.core.scope import DeploymentDefault
from daimon.testing.factories import make_tenant
from mux.testing.drivers.anthropic.ma import MARouter, list_response
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from typer.testing import CliRunner

from .test_agents import _agent_json  # pyright: ignore[reportPrivateUsage]


def _settings() -> Settings:
    return Settings(_env_file=None, database={"url": "postgresql+asyncpg://u:p@h/db"})  # pyright: ignore[reportCallIssue, reportArgumentType]


def _console() -> tuple[Console, StringIO]:
    out = StringIO()
    return Console(file=out, width=200, highlight=False), out


def _runtime(
    sessions: async_sessionmaker[AsyncSession], agents: list[dict[str, object]]
) -> CliRuntime:
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, m: list_response(agents))
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(router.dispatch), base_url="https://api.anthropic.com"
    )
    return CliRuntime(
        settings=_settings(),
        anthropic=AsyncAnthropic(api_key="test", http_client=http),
        sessionmaker=sessions,
        deployment_default=DeploymentDefault(),
        resolver_cache=new_resolver_cache(),
    )


async def _tenant(sessions: async_sessionmaker[AsyncSession]) -> TenantSelector:
    async with sessions.begin() as session:
        tenant = await make_tenant(session, platform="discord", workspace_id="g1")
    return TenantSelector(tenant_id=str(tenant.id), guild_id=None)


def _fake_builtins(
    monkeypatch: pytest.MonkeyPatch, store: dict[str, dict[str, bool | None]]
) -> None:
    """The agent's own tool config, kept in a dict per agent id instead of Managed Agents."""

    def reader(
        _deps: object, *, actor: object, ma_agent_id: str
    ) -> Callable[[], Awaitable[Mapping[str, bool | None]]]:
        async def read() -> Mapping[str, bool | None]:
            return store.setdefault(ma_agent_id, {})

        return read

    def writer(
        _deps: object, *, actor: object, ma_agent_id: str
    ) -> Callable[[str, bool], Awaitable[bool | None]]:
        async def write(tool: str, enabled: bool) -> bool | None:
            was = store.setdefault(ma_agent_id, {}).get(tool)
            store[ma_agent_id][tool] = enabled
            return was

        return write

    monkeypatch.setattr(features_cmd, "builtin_reader", reader)
    monkeypatch.setattr(features_cmd, "builtin_writer", writer)


async def _rows(rt: CliRuntime, selector: TenantSelector, **place: Any) -> list[dict[str, Any]]:
    console, out = _console()
    await features_list(
        rt=rt,
        console=console,
        selector=selector,
        agent=place.get("agent"),
        channel=place.get("channel"),
        thread=place.get("thread"),
        as_json=True,
    )
    return json.loads(out.getvalue())


def _row(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    (found,) = [r for r in rows if r["key"] == key]
    return found


async def test_list_shows_every_registered_feature(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rt = _runtime(db_session_factory, [])
    selector = await _tenant(db_session_factory)
    assert [r["key"] for r in await _rows(rt, selector)] == list(FEATURES)


async def test_set_writes_the_tier_the_place_names_and_default_clears_it(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rt = _runtime(db_session_factory, [])
    selector = await _tenant(db_session_factory)
    console, _ = _console()
    kw: dict[str, Any] = {"rt": rt, "console": console, "selector": selector}

    await features_set(
        **kw, key="progress_plans", value="on", tier=None, agent=None, channel="c1", thread=None
    )
    channel = _row(await _rows(rt, selector, channel="c1"), "progress_plans")
    assert [t["text"] for t in channel["tiers"] if t["tier"] == "channel"] == ["on"]
    assert _row(await _rows(rt, selector), "progress_plans")["source"] != "channel"

    await features_set(
        **kw,
        key="progress_plans",
        value="default",
        tier=None,
        agent=None,
        channel="c1",
        thread=None,
    )
    cleared = _row(await _rows(rt, selector, channel="c1"), "progress_plans")
    assert cleared["source"] != "channel"


async def test_a_thread_needs_its_channel(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rt = _runtime(db_session_factory, [])
    selector = await _tenant(db_session_factory)
    with pytest.raises(StoreError, match="--channel"):
        await _rows(rt, selector, thread="t1")


async def test_an_agent_tool_is_written_to_the_agents_own_tool_config(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    selector = await _tenant(db_session_factory)
    tenant_id = uuid.UUID(str(selector.tenant_id))
    agents = [_agent_json(agent_id="ag_helper", name="helper", tenant_id=tenant_id)]
    rt = _runtime(db_session_factory, agents)
    store: dict[str, dict[str, bool | None]] = {}
    _fake_builtins(monkeypatch, store)
    console, _ = _console()

    await features_set(
        rt=rt,
        console=console,
        selector=selector,
        key="tool.bash",
        value="off",
        tier=None,
        agent="helper",
        channel=None,
        thread=None,
    )

    assert store["ag_helper"]["bash"] is False
    row = _row(await _rows(rt, selector, agent="helper"), "tool.bash")
    assert row["value"] in ("off", False)


async def test_diff_lists_only_the_features_two_agents_differ_on(
    db_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    selector = await _tenant(db_session_factory)
    tenant_id = uuid.UUID(str(selector.tenant_id))
    agents = [
        _agent_json(agent_id="ag_a", name="alpha", tenant_id=tenant_id),
        _agent_json(agent_id="ag_b", name="beta", tenant_id=tenant_id),
    ]
    rt = _runtime(db_session_factory, agents)
    _fake_builtins(monkeypatch, {"ag_a": {"bash": False}, "ag_b": {}})
    console, out = _console()

    await features_diff(
        rt=rt, console=console, selector=selector, agent_a="alpha", agent_b="beta", as_json=True
    )

    keys = [d["key"] for d in json.loads(out.getvalue())]
    assert keys == ["tool.bash"]


async def test_the_cli_has_no_approvals_setter(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Approvals need a live chat admin; the CLI has none, so the core refuses."""
    rt = _runtime(db_session_factory, [])
    selector = await _tenant(db_session_factory)
    console, _ = _console()
    with pytest.raises(StoreError):
        await features_set(
            rt=rt,
            console=console,
            selector=selector,
            key="approvals",
            value="native",
            tier=None,
            agent=None,
            channel=None,
            thread=None,
        )


async def test_set_refuses_a_stale_expected_value(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    rt = _runtime(db_session_factory, [])
    selector = await _tenant(db_session_factory)
    console, _ = _console()
    kw: dict[str, Any] = {"rt": rt, "console": console, "selector": selector}
    place: dict[str, Any] = {"tier": None, "agent": None, "channel": "c1", "thread": None}
    await features_set(**kw, key="progress_plans", value="on", **place, expected="default")
    with pytest.raises(StoreError, match="changed"):
        await features_set(**kw, key="progress_plans", value="off", **place, expected="default")
    row = _row(await _rows(rt, selector, channel="c1"), "progress_plans")
    assert [t["text"] for t in row["tiers"] if t["tier"] == "channel"] == ["on"], "not written"


def test_the_set_command_passes_expected_to_the_write(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake_set(**kwargs: Any) -> None:
        seen.update(kwargs)

    @asynccontextmanager
    async def fake_runtime(_settings: object) -> AsyncIterator[None]:
        yield None

    monkeypatch.setattr(features_cmd, "features_set", fake_set)
    monkeypatch.setattr(features_cmd, "build_runtime", fake_runtime)
    monkeypatch.setattr(features_cmd, "load_settings", lambda: None)
    result = CliRunner().invoke(
        features_cmd.features_app,
        ["set", "progress_plans", "on", "--channel", "c1", "--expected", "off:locked"],
    )
    assert result.exit_code == 0, result.output
    assert seen["expected"] == "off:locked", "the CLI write must carry the stale-write check"


async def test_get_names_the_level_that_decides_over_a_lower_one(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A channel's On under a workspace Off reads as Off, set for the workspace."""
    rt = _runtime(db_session_factory, [])
    selector = await _tenant(db_session_factory)
    console, out = _console()
    kw: dict[str, Any] = {"rt": rt, "console": console, "selector": selector}
    await features_set(
        **kw, key="watches", value="on", tier=None, agent=None, channel="c1", thread=None
    )
    await features_set(
        **kw, key="watches", value="off", tier="workspace", agent=None, channel=None, thread=None
    )
    await features_list(
        **kw, agent=None, channel="c1", thread=None, keys=["watches"], as_json=False
    )
    text = " ".join(out.getvalue().split())
    assert "Off, set for the" in text and "channel=on" in text, text
