"""Text a model posts through a tool never names another chat app."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools._other_apps import scrub_platform
from daimon.adapters.mcp.tools.wizard import (
    _scrubbed_step,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.admin_actions import DelegatedAction
from daimon.core.config import AnthropicSettings, DatabaseSettings, Settings
from daimon.core.features.registry import Tier
from daimon.core.features.service import FeatureActor, change_feature
from daimon.core.features.snapshot import FeaturePlace
from daimon.core.stores.domain import Role
from daimon.core.stores.thread_sessions import create_thread_session
from daimon.core.wizard.spec import Option, Step
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from fastmcp.exceptions import ToolError
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..harness import registered_account_token
from .test_channels_dispatch import (  # pyright: ignore[reportPrivateUsage]
    _SECRET,
    _call_tool,
    _make_app,
)


def _make_settings() -> Settings:
    return Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        _env_file=None,  # type: ignore[call-arg]
    )


_CHANNELS = "daimon.adapters.mcp.tools.channels"
_TIDY = "daimon.adapters.mcp.tools.tidy"
_WIZARD = "daimon.adapters.mcp.tools.wizard"

_PLAN = {"threads": 1, "places": 1, "people": 0}
_SAID = "Never upload customer data to Discord or Slack, or Microsoft Teams."
# What each platform's person may read of `_SAID`: its own name stays.
_READ = {
    "discord": "Never upload customer data to Discord or another app.",
    "slack": "Never upload customer data to another app or Slack, or another app.",
    "teams": "Never upload customer data to another app, or Microsoft Teams.",
}


async def _token(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    platform: str,
    *,
    keep_names: bool = False,
) -> str:
    tenant = await make_tenant(db_session, platform=platform, workspace_id=f"scrub-{platform}")
    if keep_names:
        await _keep_names(sessionmaker, tenant.id, platform)
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session, platform=platform, external_id="U_CALLER", tenant=tenant, account=account
    )
    await db_session.commit()
    return await registered_account_token(sessionmaker, account_id=account.id, secret=_SECRET)


async def _keep_names(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, platform: str
) -> None:
    async def admin_for(_: DelegatedAction | None) -> bool:
        return True

    await change_feature(
        sessionmaker,
        _make_settings(),
        FeatureActor(tenant_id, None, "web", None, admin_for),
        key="other_app_names",
        tier="workspace",
        value="on",
        place=FeaturePlace.of(tenant_id, platform, None),
    )


def _record(monkeypatch: pytest.MonkeyPatch, target: str) -> AsyncMock:
    """Replace a tool's platform impl; it records its call and refuses, so no row is built."""
    impl = AsyncMock(side_effect=ToolError("recorded"))
    monkeypatch.setattr(target, impl)
    return impl


@pytest.mark.parametrize(
    ("platform", "impl"),
    [
        ("discord", "_send_message_impl"),
        ("slack", "_slack_send_message_impl"),
        ("teams", "_teams_send_message_impl"),
    ],
)
async def test_send_message_scrubs_for_the_callers_platform(
    platform: str,
    impl: str,
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = await _token(db_session, sessionmaker, platform)
    sent = _record(monkeypatch, f"{_CHANNELS}.{impl}")
    await _call_tool(
        _make_app(sessionmaker),
        token=token,
        tool_name="send_message",
        arguments={"channel_id": "C1", "content": _SAID, "plan": _PLAN},
    )
    assert sent.await_args is not None
    assert sent.await_args.kwargs["content"].startswith(_READ[platform])


@pytest.mark.parametrize(
    ("platform", "impl"),
    [
        ("discord", "_create_thread_impl"),
        ("slack", "_slack_create_thread_impl"),
        ("teams", "_teams_create_thread_impl"),
    ],
)
async def test_create_thread_scrubs_the_first_message_and_the_title(
    platform: str,
    impl: str,
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = await _token(db_session, sessionmaker, platform)
    created = _record(monkeypatch, f"{_CHANNELS}.{impl}")
    await _call_tool(
        _make_app(sessionmaker),
        token=token,
        tool_name="create_thread",
        arguments={
            "channel_id": "C1",
            "name": "Moving from Slack",
            "content": _SAID,
            "plan": _PLAN,
        },
    )
    assert created.await_args is not None
    assert created.await_args.kwargs["content"].startswith(_READ[platform])
    if platform == "discord":
        assert created.await_args.kwargs["name"] == "Moving from another app"


@pytest.mark.parametrize(
    ("platform", "impl"),
    [
        ("discord", "_edit_message_impl"),
        ("slack", "_slack_edit_message_impl"),
        ("teams", "_teams_edit_message_impl"),
    ],
)
async def test_edit_message_scrubs_the_new_text(
    platform: str,
    impl: str,
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = await _token(db_session, sessionmaker, platform)
    edited = _record(monkeypatch, f"{_TIDY}.{impl}")
    await _call_tool(
        _make_app(sessionmaker),
        token=token,
        tool_name="edit_message",
        arguments={"channel_id": "C1", "message_id": "1", "content": _SAID},
    )
    assert edited.await_args is not None
    assert edited.await_args.kwargs["content"] == _READ[platform]


@pytest.mark.parametrize("platform", ["discord", "slack", "teams"])
async def test_send_direct_message_scrubs_the_text(
    platform: str,
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = await _token(db_session, sessionmaker, platform)
    sent: Any = _record(monkeypatch, f"{_CHANNELS}.send_direct_message_impl")
    await _call_tool(
        _make_app(sessionmaker),
        token=token,
        tool_name="send_direct_message",
        arguments={"recipient_id": "U2", "content": _SAID, "plan": _PLAN},
    )
    assert sent.await_args is not None
    assert sent.await_args.kwargs["content"].startswith(_READ[platform])


def test_a_wizard_form_scrubs_what_a_person_reads_and_keeps_answer_values() -> None:
    step = Step(
        question="Where do you use Slack?",
        kind="choice",
        options=[Option(label="Slack", value="slack", description="Not Discord")],
    )
    scrubbed = _scrubbed_step(step, "teams")
    assert scrubbed.question == "Where do you use another app?"
    [option] = scrubbed.options
    assert (option.label, option.value, option.description) == (
        "Another app",
        "slack",
        "Not another app",
    )
    assert scrubbed.key == step.key, "the key was derived before the scrub"


async def test_rename_thread_scrubs_the_title(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = await _token(db_session, sessionmaker, "discord")
    renamed = _record(monkeypatch, f"{_CHANNELS}._rename_thread_impl")
    await _call_tool(
        _make_app(sessionmaker),
        token=token,
        tool_name="rename_thread",
        arguments={"thread_id": "1", "name": "Slack migration"},
    )
    assert renamed.await_args is not None
    assert renamed.await_args.kwargs["name"] == "Another app migration"


async def test_set_display_identity_scrubs_the_nickname(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = await _token(db_session, sessionmaker, "discord")
    set_identity = _record(monkeypatch, f"{_CHANNELS}._set_display_identity_impl")
    await _call_tool(
        _make_app(sessionmaker),
        token=token,
        tool_name="set_display_identity",
        arguments={"display_name": "Slack Helper", "origin_context_id": "ctx"},
    )
    assert set_identity.await_args is not None
    assert set_identity.await_args.kwargs["display_name"] == "Another app Helper"


@pytest.mark.parametrize("platform", ["discord", "slack", "teams"])
async def test_the_github_install_link_card_scrubs_its_purpose(
    platform: str,
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = await _token(db_session, sessionmaker, platform)
    posted = _record(
        monkeypatch, "daimon.adapters.mcp.tools.github_app._post_app_install_link_impl"
    )
    await _call_tool(
        _make_app(sessionmaker),
        token=token,
        tool_name="post_github_app_install_link",
        arguments={"channel_id": "C1", "purpose": _SAID},
    )
    assert posted.await_args is not None
    assert posted.await_args.kwargs["purpose"] == _READ[platform]


_LINKED = "See https://acme.slack.com/archives/C1/p1 and ask on Slack."


@pytest.mark.parametrize(
    ("platform", "impl"),
    [("discord", "_send_message_impl"), ("teams", "_teams_send_message_impl")],
)
async def test_send_message_keeps_another_apps_name_and_link_when_the_workspace_does(
    platform: str,
    impl: str,
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = await _token(db_session, sessionmaker, platform, keep_names=True)
    sent = _record(monkeypatch, f"{_CHANNELS}.{impl}")
    await _call_tool(
        _make_app(sessionmaker),
        token=token,
        tool_name="send_message",
        arguments={"channel_id": "C1", "content": _LINKED, "plan": _PLAN},
    )
    assert sent.await_args is not None
    assert sent.await_args.kwargs["content"].startswith(_LINKED)


async def test_send_message_still_scrubs_the_link_with_the_setting_off(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = await _token(db_session, sessionmaker, "discord")
    sent = _record(monkeypatch, f"{_CHANNELS}._send_message_impl")
    await _call_tool(
        _make_app(sessionmaker),
        token=token,
        tool_name="send_message",
        arguments={"channel_id": "C1", "content": _LINKED, "plan": _PLAN},
    )
    assert sent.await_args is not None
    assert "slack" not in sent.await_args.kwargs["content"].lower()


async def test_edit_message_keeps_another_apps_name_when_the_workspace_does(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = await _token(db_session, sessionmaker, "discord", keep_names=True)
    edited = _record(monkeypatch, f"{_TIDY}._edit_message_impl")
    await _call_tool(
        _make_app(sessionmaker),
        token=token,
        tool_name="edit_message",
        arguments={"channel_id": "C1", "message_id": "1", "content": _SAID},
    )
    assert edited.await_args is not None
    assert edited.await_args.kwargs["content"] == _SAID


@pytest.mark.parametrize("keep", [True, False])
async def test_a_wizard_form_keeps_or_scrubs_the_prompt_by_the_setting(
    keep: bool,
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = await _token(db_session, sessionmaker, "teams", keep_names=keep)
    validated = MagicMock(side_effect=ToolError("recorded"))
    monkeypatch.setattr(f"{_WIZARD}._validated", validated)
    await _call_tool(
        _make_app(sessionmaker),
        token=token,
        tool_name="post_wizard",
        arguments={
            "prompt": "Do you use Slack?",
            "steps": [{"question": "Where is Slack?", "kind": "text"}],
            "channel_id": "C1",
            "plan": _PLAN,
        },
    )
    prompt, steps = validated.call_args.args
    assert (prompt, steps[0].question) == (
        ("Do you use Slack?", "Where is Slack?")
        if keep
        else ("Do you use another app?", "Where is another app?")
    )


class _Case:
    """A discord tenant with a verified turn in thread T1 of channel C1, answered by agent_1."""

    def __init__(
        self,
        runtime: Any,
        auth: AuthIdentity,
        sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        self.runtime = runtime
        self.auth = auth
        self._sessionmaker = sessionmaker

    async def set(
        self,
        tier: Tier,
        value: str,
        *,
        channel: str | None = None,
        thread: str | None = None,
        agent: str | None = None,
    ) -> None:
        async def admin_for(_: DelegatedAction | None) -> bool:
            return True

        tenant_id = self.auth.tenant_id
        await change_feature(
            self._sessionmaker,
            _make_settings(),
            FeatureActor(tenant_id, None, "web", None, admin_for),
            key="other_app_names",
            tier=tier,
            value=value,
            place=FeaturePlace(tenant_id, "discord", channel, thread, agent),
        )

    async def scrub(self, destination: str) -> str | None:
        return await scrub_platform(
            self.runtime, self.auth, destination, origin_context_id=str(uuid.uuid4())
        )


async def _case(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    *,
    verified: bool = True,
) -> _Case:
    tenant = await make_tenant(db_session, platform="discord", workspace_id="scrub-places")
    account = await make_account(db_session, tenant=tenant)
    await create_thread_session(
        db_session,
        tenant_id=tenant.id,
        platform="discord",
        thread_id="T1",
        account_id=account.id,
        ma_session_id="sess_1",
        ma_agent_id="agent_1",
        channel_id="C1",
    )
    await db_session.commit()
    origin = SimpleNamespace(
        parent_channel_id="C1", thread_id="T1", responder_ma_agent_id="agent_1"
    )

    async def get_origin(*_a: object, **_k: object) -> SimpleNamespace | None:
        return origin if verified else None

    monkeypatch.setattr("daimon.adapters.mcp.tools._other_apps.get_verified_origin", get_origin)
    runtime = SimpleNamespace(session_factory=sessionmaker, settings=_make_settings())
    auth = AuthIdentity(
        account_id=account.id, tenant_id=tenant.id, role=Role.USER, platform="discord"
    )
    return _Case(runtime, auth, sessionmaker)


async def test_names_are_scrubbed_when_the_senders_agent_is_off(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = await _case(db_session, sessionmaker, monkeypatch)
    await case.set("workspace", "on")
    assert await case.scrub("C2") is None
    await case.set("agent", "off", agent="agent_1")
    assert await case.scrub("C2") == "discord"


async def test_an_agent_on_alone_does_not_keep_names_where_the_destination_is_off(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = await _case(db_session, sessionmaker, monkeypatch)
    await case.set("agent", "on", agent="agent_1")
    assert await case.scrub("C2") == "discord"
    await case.set("workspace", "on")
    assert await case.scrub("C2") is None


async def test_a_thread_destination_follows_its_own_and_its_channels_setting(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = await _case(db_session, sessionmaker, monkeypatch, verified=False)
    await case.set("workspace", "on")
    assert await case.scrub("T2") is None
    await case.set("thread", "off", channel="C1", thread="T2")
    assert await case.scrub("T2") == "discord", "a thread set Off"
    assert await case.scrub("C1") is None, "its channel is untouched"
    assert await case.scrub("T1") is None
    await case.set("channel", "off", channel="C1")
    assert await case.scrub("T1") == "discord", "a thread inherits its Off channel"


async def test_a_chat_agent_that_names_no_verified_turn_keeps_the_scrub(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = await _case(db_session, sessionmaker, monkeypatch, verified=False)
    await case.set("workspace", "on")
    assert await case.scrub("C2") is None
    agent_auth = AuthIdentity(
        account_id=case.auth.account_id,
        tenant_id=case.auth.tenant_id,
        role=Role.USER,
        platform="discord",
        chat_agent_id=uuid.uuid4(),
    )
    assert await scrub_platform(case.runtime, agent_auth, "C2") == "discord"
