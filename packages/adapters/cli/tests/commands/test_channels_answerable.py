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
from daimon.adapters.cli.commands import channels_answerable as module
from daimon.adapters.cli.commands.channels_answerable import (
    channels_answerable,
    channels_answerable_command,
)
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.thread_agent_bindings import create_binding, update_lifecycle
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


def _settings(*, token: bool = True) -> object:
    discord = SimpleNamespace(bot_token=SimpleNamespace(get_secret_value=lambda: "t"))
    return SimpleNamespace(cli=FakeCliSettings.cli, discord=discord if token else None)


def _guild(channels: list[dict[str, object]], *, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET", "the check only reads Discord"
        assert request.headers["Authorization"] == "Bot t"
        assert request.url.path == f"/api/v10/guilds/{GUILD}/channels"
        return httpx.Response(status, json=channels if status == 200 else {})

    return httpx.MockTransport(handler)


async def _seed(
    factory: async_sessionmaker[AsyncSession],
    *,
    bound_parents: tuple[str, ...],
    deleted_parents: tuple[str, ...] = (),
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
        for n, parent in enumerate(deleted_parents):
            await create_binding(
                s,
                tenant_id=tenant_id,
                platform="discord",
                parent_channel_id=parent,
                thread_id=f"80{n}",
                responder_ma_agent_id=f"agent_gone_{n}",
                responder_name="gone",
            )
            await update_lifecycle(
                s,
                tenant_id=tenant_id,
                platform="discord",
                parent_channel_id=parent,
                thread_id=f"80{n}",
                deleted=True,
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
    expected: set[str] | None = None,
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
            expected=expected,
            as_json=as_json,
            discord_transport=_guild(channels),
        )
    except typer.Exit as exc:
        code = exc.exit_code
    return console, code


async def _fail(
    factory: async_sessionmaker[AsyncSession],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    token: bool = True,
) -> None:
    await channels_answerable(
        rt=build_cli_runtime(factory, settings=_settings(token=token)),
        console=_console(),
        platform="discord",
        guild_id=GUILD,
        discord_transport=transport,
    )


async def test_rules_categories_open_channels_and_bindings_make_the_set(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(db_session_factory, bound_parents=("101", "105"), deleted_parents=("102",))

    console, code = await _run(db_session_factory)

    assert code == 0
    found = json.loads(_out(console))
    why = {c["channel_id"]: (c["answers"], c["why"]) for c in found["channels"]}
    assert why == {
        "101": (True, "no writers none rule"),
        "102": (False, "channel rule writers none"),
        "103": (False, "category 10 writers none"),
        "104": (True, "no writers none rule"),
        "105": (False, "channel rule writers none"),
    }, "categories themselves are not channels"
    assert found["answerable"] == ["101", "104"], (
        "an unconfigured channel answers; a thread bound under a writers none channel "
        "is refused by the turn gate, so it does not reopen the channel"
    )
    assert [
        (b["thread_id"], b["parent_channel_id"], b["answers"]) for b in found["thread_bindings"]
    ] == [("900", "101", True), ("901", "105", False)], (
        "deleted bindings and DM scopes are not listed"
    )
    assert found["errors"] == []


async def test_expect_matches_the_channels_the_other_bot_hands_over(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(db_session_factory, bound_parents=("101", "105"))

    console, code = await _run(db_session_factory, expected={"101", "104"}, as_json=False)

    assert code == 0, _out(console)
    text = _out(console)
    assert "Daimon may answer in 2 channel(s)" in text
    assert "900" in text and "matches the expected list" in text


async def test_expect_mismatch_prints_both_diffs_and_the_unlisted_binding(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(db_session_factory, bound_parents=("104", "105"))
    expected = {"101", "103"}

    console, code = await _run(db_session_factory, expected=expected, as_json=False)

    assert code == 1
    text = _out(console)
    assert "Daimon may answer, not in the file: 104" in text
    assert "In the file, Daimon may not answer: 103" in text
    assert "Thread 900 (helper) answers under unlisted channel 104" in text
    assert "Thread 901" not in text, "a refused thread under a silent channel is no difference"

    json_console, code = await _run(db_session_factory, expected=expected)
    assert code == 1
    comparison = json.loads(_out(json_console))["comparison"]
    assert comparison["match"] is False
    assert (comparison["missing_from_expect"], comparison["missing_from_daimon"]) == (
        ["104"],
        ["103"],
    )
    assert [b["thread_id"] for b in comparison["unlisted_thread_bindings"]] == ["900"]


async def test_an_unknown_category_counts_as_answerable_and_fails(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Fail closed: a channel whose category can't be found is never taken as silent."""
    await _seed(db_session_factory, bound_parents=())
    orphan: dict[str, object] = {"id": "106", "type": 0, "name": "orphan", "parent_id": "10"}
    without_category = [c for c in CHANNELS if c["id"] != "10"]

    console, code = await _run(db_session_factory, channels=[*without_category, orphan])

    assert code == 1
    found = json.loads(_out(console))
    assert {"103", "106"} <= set(found["answerable"]), "an unresolved category does not silence"
    assert len(found["errors"]) == 2 and "category 10" in found["errors"][0]


async def test_a_binding_under_a_channel_not_in_the_server_is_an_error_and_not_counted(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed(db_session_factory, bound_parents=("999",))

    console, code = await _run(db_session_factory, expected={"101", "104"})

    assert code == 1
    found = json.loads(_out(console))
    assert found["answerable"] == ["101", "104"], "a parent Discord doesn't list is not counted"
    assert [
        (b["thread_id"], b["parent_in_server"], b["answers"]) for b in found["thread_bindings"]
    ] == [("900", False, False)]
    assert found["errors"] == [
        "thread 900: parent channel 999 is not in the server's channel list; not counted"
    ]
    assert found["comparison"]["match"] is True, "the error alone is what fails the run"


async def test_an_unseeded_server_is_open_and_discord_failures_are_errors(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    console, code = await _run(db_session_factory)
    assert code == 0
    assert json.loads(_out(console))["answerable"] == ["101", "102", "103", "104", "105"], (
        "no tenant yet: no rules, so every channel answers"
    )

    with pytest.raises(DaimonError, match="not visible to daimon"):
        await _fail(db_session_factory, transport=_guild([], status=403))
    with pytest.raises(DaimonError, match="not visible to daimon"):
        await _fail(db_session_factory, transport=_guild([], status=404))
    with pytest.raises(DaimonError, match="HTTP 500"):
        await _fail(db_session_factory, transport=_guild([], status=500))

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(DaimonError, match="could not reach Discord"):
        await _fail(db_session_factory, transport=httpx.MockTransport(unreachable))
    with pytest.raises(DaimonError, match="DAIMON_DISCORD__BOT_TOKEN"):
        await _fail(db_session_factory, token=False)


def test_bad_arguments_fail_before_the_runtime_opens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def no_runtime(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("the runtime must not open on bad arguments")

    monkeypatch.setattr(module, "build_runtime", no_runtime)
    monkeypatch.setattr(module, "load_settings", no_runtime)
    bad = tmp_path / "bad.txt"
    bad.write_text("101\ngeneral\n")
    with pytest.raises(typer.BadParameter, match="bad.txt:2"):
        channels_answerable_command("discord", GUILD, expect=bad)
    with pytest.raises(typer.BadParameter, match="cannot read"):
        channels_answerable_command("discord", GUILD, expect=tmp_path / "missing.txt")
    with pytest.raises(typer.BadParameter, match="only discord"):
        channels_answerable_command("slack", GUILD)
    with pytest.raises(typer.BadParameter, match="not a Discord server id"):
        channels_answerable_command("discord", "guild")


def test_the_expect_file_takes_one_id_per_line_with_comments(tmp_path: Path) -> None:
    listed = tmp_path / "handed-over.txt"
    listed.write_text("# channels the other bot does not answer\n101\n\n104  # forum\n")

    assert module.read_expect_file(listed) == {"101", "104"}
