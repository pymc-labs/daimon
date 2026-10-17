"""`daimon channels answerable` against the real database and a faked Discord."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import httpx
import pytest
import typer
from daimon.adapters.cli.commands.channels_answerable import channels_answerable
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.thread_agent_bindings import create_binding
from rich.console import Console
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..harness import FakeCliSettings, build_cli_runtime

pytestmark = pytest.mark.no_cli_local_seed
GUILD = "1000"
NONE = ChannelRule(writers="none")

# 10 is a category ruled writers none; 20 an open category.
CHANNELS: list[dict[str, object]] = [
    {"id": "10", "type": 4, "name": "owned", "parent_id": None},
    {"id": "20", "type": 4, "name": "shared", "parent_id": None},
    {"id": "101", "type": 0, "name": "general", "parent_id": None},
    {"id": "102", "type": 0, "name": "ruled", "parent_id": "20"},
    {"id": "103", "type": 0, "name": "in-owned", "parent_id": "10"},
    {"id": "104", "type": 15, "name": "forum", "parent_id": "20"},
    {"id": "105", "type": 0, "name": "bound-silent", "parent_id": None},
]


def _console() -> Console:
    return Console(file=StringIO(), force_terminal=False, highlight=False, width=200)


def _out(console: Console) -> str:
    return cast(StringIO, console.file).getvalue()


def _settings() -> object:
    return SimpleNamespace(
        cli=FakeCliSettings.cli,
        discord=SimpleNamespace(bot_token=SimpleNamespace(get_secret_value=lambda: "t")),
    )


def _guild(channels: list[dict[str, object]], *, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", "the check only reads Discord"
        assert request.headers["Authorization"] == "Bot t"
        assert request.url.path == f"/api/v10/guilds/{GUILD}/channels"
        return httpx.Response(status, json=channels if status == 200 else {})

    return httpx.MockTransport(handler)


async def _seed(
    factory: async_sessionmaker[AsyncSession], *, bound_parents: tuple[str, ...]
) -> None:
    await provision_tenant(factory, platform="discord", workspace_id=GUILD)
    tenant_id = derive_tenant_uuid(platform="discord", workspace_id=GUILD)
    async with factory.begin() as s:
        await set_access_policy(
            s,
            tenant_id=tenant_id,
            policy=TenantAccessPolicy(
                channel_rules={"102": NONE, "105": NONE},
                category_rules={"10": NONE},
            ),
        )
        for n, parent in enumerate(bound_parents):
            await create_binding(
                s,
                tenant_id=tenant_id,
                platform="discord",
                parent_channel_id=parent,
                thread_id=f"90{n}",
                responder_ma_agent_id=f"agent_{n}",
                responder_name="helper",
                kind="handoff",
            )
        await create_binding(
            s,
            tenant_id=tenant_id,
            platform="discord",
            parent_channel_id="dm-chan",
            thread_id="dm:x",
            responder_ma_agent_id="agent_dm",
            responder_name="helper",
        )


async def _run(
    factory: async_sessionmaker[AsyncSession],
    *,
    expect: Path | None = None,
    as_json: bool = True,
    channels: list[dict[str, object]] = CHANNELS,
) -> tuple[Console, int]:
    console = _console()
    code = 0
    try:
        await channels_answerable(
            rt=build_cli_runtime(factory, settings=_settings()),
            console=console,
            platform="discord",
            guild_id=GUILD,
            expect=expect,
            as_json=as_json,
            discord_transport=_guild(channels),
        )
    except typer.Exit as exc:
        code = exc.exit_code
    return console, code


async def test_rules_categories_open_channels_and_bindings_make_the_set(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(db_session_factory, bound_parents=("105",))

    console, code = await _run(db_session_factory)

    assert code == 0
    found = json.loads(_out(console))
    why = {c["channel_id"]: (c["answers"], c["why"]) for c in found["channels"]}
    assert why == {
        "101": (True, "no writers none rule"),
        "102": (False, "channel rule writers none"),
        "103": (False, "category 10 writers none"),
        "104": (True, "no writers none rule"),
        "105": (True, "thread bound here (channel rule writers none)"),
    }, "categories themselves are not channels"
    assert found["answerable"] == ["101", "104", "105"], (
        "an unconfigured channel answers, and a bound thread's parent counts whatever its rule"
    )
    assert [(b["thread_id"], b["parent_channel_id"]) for b in found["thread_bindings"]] == [
        ("900", "105")
    ], "DM scopes are not server channels"
    assert found["errors"] == []


async def test_expect_matches_with_a_thread_binding_under_a_listed_parent(
    db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    await _seed(db_session_factory, bound_parents=("101",))
    listed = tmp_path / "owned.txt"
    listed.write_text("# channels the other bot owns\n101\n\n104  # forum\n")

    console, code = await _run(db_session_factory, expect=listed, as_json=False)

    assert code == 0, _out(console)
    text = _out(console)
    assert "Daimon may answer in 2 channel(s)" in text
    assert "900" in text and "matches the expected list" in text


async def test_expect_mismatch_prints_both_diffs_and_the_unlisted_binding(
    db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    await _seed(db_session_factory, bound_parents=("105",))
    listed = tmp_path / "owned.txt"
    listed.write_text("101\n103\n")

    console, code = await _run(db_session_factory, expect=listed, as_json=False)

    assert code == 1
    text = _out(console)
    assert "Daimon may answer, not in the file: 104, 105" in text
    assert "In the file, Daimon may not answer: 103" in text
    assert "Thread 900 (helper) is bound under unlisted channel 105" in text

    json_console, code = await _run(db_session_factory, expect=listed)
    assert code == 1
    comparison = json.loads(_out(json_console))["comparison"]
    assert comparison["match"] is False
    assert (comparison["missing_from_expect"], comparison["missing_from_daimon"]) == (
        ["104", "105"],
        ["103"],
    )
    assert [b["thread_id"] for b in comparison["unlisted_thread_bindings"]] == ["900"]


async def test_an_unknown_category_counts_as_answerable_and_fails(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Fail closed: a channel whose category can't be found is never taken as silent."""
    await _seed(db_session_factory, bound_parents=())
    orphan = {"id": "106", "type": 0, "name": "orphan", "parent_id": "10"}
    without_category = [c for c in CHANNELS if c["id"] != "10"]

    console, code = await _run(db_session_factory, channels=[*without_category, orphan])

    assert code == 1
    found = json.loads(_out(console))
    assert {"103", "106"} <= set(found["answerable"]), "an unresolved category does not silence"
    assert len(found["errors"]) == 2 and "category 10" in found["errors"][0]


async def test_an_unseeded_server_is_open_and_discord_failures_are_errors(
    db_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    console, code = await _run(db_session_factory)
    assert code == 0
    assert json.loads(_out(console))["answerable"] == ["101", "102", "103", "104", "105"], (
        "no tenant yet: no rules, so every channel answers"
    )

    rt = build_cli_runtime(db_session_factory, settings=_settings())
    where = {"rt": rt, "console": _console(), "platform": "discord", "guild_id": GUILD}
    with pytest.raises(DaimonError, match="not visible to daimon"):
        await channels_answerable(**where, discord_transport=_guild([], status=403))
    with pytest.raises(DaimonError, match="HTTP 500"):
        await channels_answerable(**where, discord_transport=_guild([], status=500))
    bad = tmp_path / "bad.txt"
    bad.write_text("101\ngeneral\n")
    with pytest.raises(typer.BadParameter, match="bad.txt:2"):
        await channels_answerable(**where, expect=bad, discord_transport=_guild(CHANNELS))
    with pytest.raises(typer.BadParameter, match="only discord"):
        await channels_answerable(**{**where, "platform": "slack"})
