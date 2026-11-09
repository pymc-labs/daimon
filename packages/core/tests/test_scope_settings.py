"""The Settings group: registry, reader, writer and panel, one rule per key for every surface."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from daimon.core.admin_actions import DelegatedAction
from daimon.core.authz import Subject
from daimon.core.channel_changes import ChannelChangeRefused, Expected
from daimon.core.config import AnthropicSettings, DatabaseSettings, DiscordSettings, Settings
from daimon.core.confirmation import prompt_for_tool_call
from daimon.core.errors import StaleChannelValue
from daimon.core.features import service
from daimon.core.scope_cascade import CascadeMode
from daimon.core.scope_settings import (
    OVERRIDES_OFF,
    SPECS,
    SettingsActor,
    SettingsPlace,
    apply_choice,
    change_setting,
    read_settings,
    setting_refusal,
    switch_default,
)
from daimon.core.stores.channel_changes import WORKSPACE_ID, list_changes
from daimon.core.tool_safety import DAIMON_SERVER_NAME, ToolCall, ToolSafetyPolicy
from daimon.testing.factories import make_tenant
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

Sessions = async_sessionmaker[AsyncSession]
THREAD = SettingsPlace.of("discord", "C1", "T1")


def _settings(*, overrides: bool = True) -> Settings:
    return Settings.model_validate(
        {
            "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
            "anthropic": AnthropicSettings(api_key=SecretStr("test")),
            "discord": DiscordSettings(bot_token=SecretStr("test")),
            "tool_safety": ToolSafetyPolicy(enabled=overrides, allow_gate_overrides=overrides),
        }
    )


def _actor(tenant_id: uuid.UUID, kind: str = "member") -> SettingsActor:
    """``member``, ``admin``, ``granted`` (every delegated action), ``agent_key``, ``web_only``."""

    async def admin_for(action: DelegatedAction | None) -> bool:
        return kind in ("admin", "agent_key", "web_only") or (
            kind == "granted" and action is not None
        )

    native = None if kind == "web_only" else "person"
    return SettingsActor(
        tenant_id, None, "chat", native, admin_for, via_agent_key=kind == "agent_key"
    )


@pytest.fixture
def native(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """The live native-admin check, answering yes unless a test says otherwise."""
    check = AsyncMock(return_value=Subject(is_admin=True, platform_user_id="person"))
    monkeypatch.setattr(service, "approval_admin", check)
    return check


async def _tenant(db_session: AsyncSession) -> uuid.UUID:
    tenant = await make_tenant(db_session)
    await db_session.commit()
    return tenant.id


async def _change(
    sessions: Sessions,
    tenant_id: uuid.UUID,
    key: str,
    level: str,
    value: str,
    actor: str = "admin",
    **kw: Any,
) -> Any:
    return await change_setting(
        sessions,
        _settings(),
        _actor(tenant_id, actor),
        key=key,
        level=level,
        value=value,
        place=THREAD,
        **kw,
    )


def test_registry_places_and_defaults() -> None:
    assert SPECS["approvals"].labels == {"on": "Ask", "off": "Allow writes", "inherit": "Inherit"}
    assert SPECS["participation"].platforms == frozenset({"discord", "teams"})
    assert SPECS["cost_footer"].levels == ("server", "channel", "thread")
    assert SettingsPlace.of("discord", "C1", "C1").levels() == ("server", "channel")
    assert SettingsPlace.of("discord", "C1", None, "a1").levels() == ("server", "agent", "channel")
    slack = SettingsPlace.of("slack", "C1", "C1:1.2")
    assert (slack.thread_id, slack.thread_key, slack.target("thread")) == (
        "1.2",
        "C1:1.2",
        "C1:1.2",
    )
    assert THREAD.target("thread") == "T1" and THREAD.target("server") is None
    assert switch_default(_settings(), "participation", "teams") is CascadeMode.DISABLED


@pytest.mark.parametrize(
    ("key", "level", "value", "place", "reason"),
    [
        ("colour", "server", "on", THREAD, "unknown_key"),
        ("approvals", "server", "disabled", THREAD, "bad_value"),
        ("approvals", "planet", "on", THREAD, "bad_level"),
        ("participation", "server", "on", SettingsPlace.of("slack", "C1", None), "wrong_platform"),
        ("approvals", "agent", "on", SettingsPlace.of("discord", "C1", None, "a1"), "wrong_level"),
        ("approvals", "thread", "on", SettingsPlace.of("discord", "C1", None), "no_such_level"),
        ("participation", "agent", "on", THREAD, "no_such_level"),
        ("progress_plans", "thread", "disabled", THREAD, "thread_disabled"),
    ],
)
def test_refusals_before_any_read(
    key: str, level: str, value: str, place: Any, reason: str
) -> None:
    refusal = setting_refusal(key, level, value, place=place)
    assert refusal is not None and refusal.reason == reason


async def test_read_says_which_level_decided(
    db_session: AsyncSession, db_session_factory: Sessions, native: AsyncMock
) -> None:
    tenant = await _tenant(db_session)
    await _change(db_session_factory, tenant, "approvals", "server", "on")
    await _change(db_session_factory, tenant, "approvals", "thread", "off")
    await _change(db_session_factory, tenant, "participation", "server", "disabled")
    await _change(db_session_factory, tenant, "cost_footer", "channel", "hidden")
    views = {
        v.key: v
        for v in await read_settings(
            db_session_factory, settings=_settings(), tenant_id=tenant, place=THREAD
        )
    }
    assert (views["approvals"].effective, views["approvals"].decided_by) == ("off", "thread")
    assert views["approvals"].own("server") == "on"
    participation = views["participation"]
    assert participation.effective == "disabled", "disabled is final below its level"
    assert participation.locked and participation.decided_by == "server"
    assert (views["cost_footer"].effective, views["cost_footer"].decided_by) == (
        "hidden",
        "channel",
    )
    (ignored,) = await read_settings(
        db_session_factory,
        settings=_settings(overrides=False),
        tenant_id=tenant,
        place=THREAD,
        keys=("approvals",),
    )
    assert not ignored.active and ignored.note.endswith(OVERRIDES_OFF)


# (key, level, value, actor, native admin live?, refusal match or None)
CHANGES = [
    ("approvals", "thread", "off", "member", True, None),
    ("approvals", "server", "off", "admin", False, "Only a server admin can change approvals"),
    ("approvals", "server", "off", "granted", False, "can change approvals"),
    ("approvals", "server", "off", "agent_key", True, "can change approvals"),
    ("approvals", "server", "off", "web_only", True, "can change approvals"),
    ("cost_footer", "server", "hidden", "granted", False, None),
    ("cost_footer", "server", "hidden", "member", True, "Ask a server admin"),
    ("participation", "thread", "on", "member", False, None),
    ("participation", "channel", "on", "member", False, "Only a server admin"),
]


@pytest.mark.parametrize(("key", "level", "value", "actor", "live", "refused"), CHANGES)
async def test_who_may_change_what(
    db_session: AsyncSession, db_session_factory: Sessions, native: AsyncMock,
    key: str, level: str, value: str, actor: str, live: bool, refused: str | None,
) -> None:  # fmt: skip
    tenant = await _tenant(db_session)
    native.return_value = Subject(is_admin=live, platform_user_id="person")
    if refused is not None:
        with pytest.raises(ChannelChangeRefused, match=refused):
            await _change(db_session_factory, tenant, key, level, value, actor)
    else:
        change = await _change(db_session_factory, tenant, key, level, value, actor)
        assert change.after == value
    if actor in ("agent_key", "web_only"):
        native.assert_not_awaited()


async def test_writes_carry_the_place_record_and_stale_check(
    db_session: AsyncSession, db_session_factory: Sessions, native: AsyncMock
) -> None:
    tenant = await _tenant(db_session)
    await _change(db_session_factory, tenant, "participation", "thread", "on", "member")
    footer = await _change(db_session_factory, tenant, "cost_footer", "server", "hidden")
    assert footer.before == "inherit"
    with pytest.raises(StaleChannelValue):
        await _change(
            db_session_factory, tenant, "cost_footer", "server", "shown", expected=Expected("shown")
        )
    async with db_session_factory() as session:
        channel = await list_changes(session, tenant_id=tenant, channel_id="C1")
        workspace = await list_changes(session, tenant_id=tenant, channel_id=WORKSPACE_ID)
    assert [(c.setting, c.after) for c in channel] == [
        ("thread_participation", {"thread": "T1", "mode": "on"})
    ]
    assert [(c.setting, c.after) for c in workspace] == [("cost_footer", "hidden")]


async def test_a_lock_above_rules_out_other_values_below(
    db_session: AsyncSession, db_session_factory: Sessions, native: AsyncMock
) -> None:
    tenant = await _tenant(db_session)
    await _change(db_session_factory, tenant, "cost_footer", "server", "hidden", locked=True)
    with pytest.raises(ChannelChangeRefused) as above:
        await _change(db_session_factory, tenant, "cost_footer", "thread", "shown")
    assert above.value.reason == "locked_above"
    with pytest.raises(ChannelChangeRefused, match="can't be locked at the thread level"):
        await _change(
            db_session_factory, tenant, "progress_plans", "thread", "on", "member", locked=True
        )
    with pytest.raises(ChannelChangeRefused) as approvals:
        await _change(db_session_factory, tenant, "approvals", "server", "on", locked=True)
    assert approvals.value.reason == "bad_lock"


@pytest.mark.parametrize(
    "raw", ["approvals:thread:maybe", "colour:server:on", "a:b", "approvals:server:off:x", None, 3]
)
async def test_a_forged_choice_writes_and_says_nothing(
    db_session: AsyncSession, db_session_factory: Sessions, native: AsyncMock, raw: object
) -> None:
    tenant = await _tenant(db_session)
    assert (
        await apply_choice(db_session_factory, _settings(), _actor(tenant, "admin"), raw, THREAD)
        is None
    )
    native.assert_not_awaited()


async def test_apply_choice_answers_with_the_change_or_the_refusal(
    db_session: AsyncSession, db_session_factory: Sessions
) -> None:
    member = _actor(await _tenant(db_session), "member")
    done = await apply_choice(
        db_session_factory, _settings(), member, "participation:thread:on", THREAD
    )
    assert done is not None and done[0] and "Thread participation is now On" in done[1]
    refused = await apply_choice(
        db_session_factory, _settings(), member, "participation:channel:on", THREAD
    )
    assert refused is not None and not refused[0] and "Ask a server admin" in refused[1]


@pytest.mark.parametrize(
    ("arguments", "line"),
    [
        ({"key": "approvals", "level": "thread", "value": "off"}, "Approvals: Allow writes for this thread"),
        ({"key": "cost_footer", "level": "server", "value": "hidden"}, "Cost footer: Hidden for all channels"),
        ({"key": "cost_footer", "level": "thread", "value": "hidden"}, "Cost footer: Hidden for this thread"),
        ({"key": "cost_footer", "level": "agent", "value": "hidden"}, None),
    ],
)  # fmt: skip
def test_the_agent_tool_card_says_the_change_in_the_panel_words(
    arguments: dict[str, object], line: str | None
) -> None:
    call = ToolCall(
        tool_use_id="s", server_name=DAIMON_SERVER_NAME, tool_name="change_setting",
        input={"origin_context_id": "origin", **arguments},
    )  # fmt: skip
    prompt = prompt_for_tool_call(call, requester_platform_user_id="u1", now=datetime.now(UTC))
    assert prompt.consequence == line
    assert prompt.detail_lines == (
        (line,) if line else ("Key: cost_footer", "Level: agent", "Value: hidden")
    )
