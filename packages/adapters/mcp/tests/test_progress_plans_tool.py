"""DB-backed unit tests for the progress-plan MCP tools.

The switch shares thread participation's cascade and permission rules, so these
cover what is its own: the table it writes, Slack's thread ids, and that
`update_plan` asks for nothing but the steps.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import progress_plans as tool_module
from daimon.adapters.mcp.tools._channel_target import ChannelTarget
from daimon.adapters.mcp.tools.progress_plans import (
    _get_progress_plans_impl,  # pyright: ignore[reportPrivateUsage]
    _set_progress_plans_impl,  # pyright: ignore[reportPrivateUsage]
    register_progress_plan_tools,
)
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    DiscordSettings,
    McpSettings,
    ProgressPlansSettings,
    Settings,
)
from daimon.core.scope import DeploymentDefault
from daimon.core.scope_cascade import CascadeMode, CascadeModes, CascadeScope
from daimon.core.stores.domain import Role
from daimon.core.stores.progress_plans import get_progress_plan_modes
from daimon.core.stores.thread_participation import get_participation_modes
from daimon.testing.factories import make_account, make_tenant
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_CHANNEL = "chan-1"
_THREAD = "thread-1"
_SLACK_CHANNEL = "C0123"
_SLACK_TS = "1700000000.000100"


@pytest.fixture(autouse=True)
def visible_scopes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every id is visible; a Discord thread's parent is `_CHANNEL`; Slack lookups are recorded."""

    async def discord(
        runtime: McpRuntime, auth: AuthIdentity, scope: CascadeScope, scope_id: str | None
    ) -> str | None:
        return _CHANNEL if scope is CascadeScope.THREAD else None

    slack_lookups: list[str] = []

    async def slack(runtime: McpRuntime, auth: AuthIdentity, raw: str) -> ChannelTarget:
        slack_lookups.append(raw)
        return ChannelTarget(raw)

    monkeypatch.setattr(tool_module, "verify_participation_scope", discord)
    monkeypatch.setattr(tool_module, "resolve_channel", slack)
    return slack_lookups


def _runtime(
    sessionmaker: async_sessionmaker[AsyncSession], mode: CascadeMode = CascadeMode.OFF
) -> McpRuntime:
    return McpRuntime(
        session_factory=sessionmaker,
        client=MagicMock(spec=AsyncAnthropic),  # type: ignore[arg-type]
        settings=Settings(
            database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
            anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
            mcp=McpSettings(jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://x/mcp")),
            discord=DiscordSettings(bot_token=SecretStr("test-bot-token")),
            progress_plans=ProgressPlansSettings(mode=mode),
        ),
        deployment_default=DeploymentDefault(),
    )


def _auth(*, tenant_id: uuid.UUID, admin: bool, platform: str | None = "discord") -> AuthIdentity:
    return AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=tenant_id,
        role=Role.ADMIN if admin else Role.USER,
        platform=platform,
        is_admin=admin,
    )


async def _seed(sessionmaker: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    async with sessionmaker.begin() as session:
        tenant = await make_tenant(session)
        await make_account(session, tenant=tenant)
        return tenant.id


async def _modes(
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    *,
    platform: str = "discord",
    channel_id: str = _CHANNEL,
    thread_id: str = _THREAD,
) -> CascadeModes:
    async with sessionmaker() as session:
        return await get_progress_plan_modes(
            session,
            tenant_id=tenant_id,
            platform=platform,
            channel_id=channel_id,
            thread_id=thread_id,
        )


async def test_member_turns_progress_plans_on_for_a_thread_and_nothing_else(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The thread row is written to the progress-plan table, not participation's."""
    tenant_id = await _seed(committing_sessionmaker)

    result = await _set_progress_plans_impl(
        _runtime(committing_sessionmaker),
        _auth(tenant_id=tenant_id, admin=False),
        "on",
        _THREAD,
        None,
    )

    assert (result.effective_mode, result.effective_tier) == ("on", "thread"), (
        "a thread turned on resolves on at the thread tier"
    )
    assert "progress card" in result.note, "the note must say what the person will see"
    modes = await _modes(committing_sessionmaker, tenant_id)
    assert modes.thread is CascadeMode.ON, "the thread row must be stored"
    async with committing_sessionmaker() as session:
        participation = await get_participation_modes(
            session, tenant_id=tenant_id, platform="discord", channel_id=_CHANNEL, thread_id=_THREAD
        )
    assert participation.thread is None, "progress plans must not touch thread participation"


async def test_a_member_cannot_set_a_channel_and_nothing_is_written(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Channel scope is an admin action, refused before any write."""
    tenant_id = await _seed(committing_sessionmaker)

    with pytest.raises(ToolError, match="requires a server admin"):
        await _set_progress_plans_impl(
            _runtime(committing_sessionmaker),
            _auth(tenant_id=tenant_id, admin=False),
            "on",
            None,
            _CHANNEL,
        )

    assert (await _modes(committing_sessionmaker, tenant_id)).channel is None, (
        "a refused call must leave no row behind"
    )


async def test_a_disabled_channel_refuses_a_thread_turned_on_and_writes_nothing(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """`disabled` is final below the scope that set it."""
    tenant_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker)
    await _set_progress_plans_impl(
        runtime, _auth(tenant_id=tenant_id, admin=True), "disabled", None, _CHANNEL
    )

    with pytest.raises(ToolError, match="disabled at the channel level"):
        await _set_progress_plans_impl(
            runtime, _auth(tenant_id=tenant_id, admin=False), "on", _THREAD, None
        )

    assert (await _modes(committing_sessionmaker, tenant_id)).thread is None, (
        "the refused thread write must leave no row"
    )


async def test_disabled_is_not_a_thread_setting(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """A thread is turned off, not disabled."""
    tenant_id = await _seed(committing_sessionmaker)

    with pytest.raises(ToolError, match="not a thread-level setting"):
        await _set_progress_plans_impl(
            _runtime(committing_sessionmaker),
            _auth(tenant_id=tenant_id, admin=True),
            "disabled",
            _THREAD,
            None,
        )


async def test_a_deployment_that_disables_progress_plans_refuses_every_write(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The operator's `disabled` cannot be overridden from chat."""
    tenant_id = await _seed(committing_sessionmaker)

    with pytest.raises(ToolError, match="turned off for all of Daimon"):
        await _set_progress_plans_impl(
            _runtime(committing_sessionmaker, CascadeMode.DISABLED),
            _auth(tenant_id=tenant_id, admin=True),
            "on",
            None,
            None,
        )


async def test_a_slack_thread_is_keyed_by_its_channel_and_timestamp(
    committing_sessionmaker: async_sessionmaker[AsyncSession], visible_scopes: list[str]
) -> None:
    """A Slack timestamp is unique only within its channel, so the row carries both."""
    tenant_id = await _seed(committing_sessionmaker)

    result = await _set_progress_plans_impl(
        _runtime(committing_sessionmaker),
        _auth(tenant_id=tenant_id, admin=False, platform="slack"),
        "on",
        _SLACK_TS,
        _SLACK_CHANNEL,
    )

    key = f"{_SLACK_CHANNEL}:{_SLACK_TS}"
    assert result.scope == f"thread:{key}", "the result must name the stored thread key"
    assert visible_scopes == [_SLACK_CHANNEL], "the caller's access to the channel is checked"
    modes = await _modes(
        committing_sessionmaker,
        tenant_id,
        platform="slack",
        channel_id=_SLACK_CHANNEL,
        thread_id=key,
    )
    assert modes.thread is CascadeMode.ON, "the row is keyed `<channel>:<thread ts>`"


async def test_a_slack_thread_timestamp_alone_is_refused(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Without its channel a timestamp names no thread."""
    tenant_id = await _seed(committing_sessionmaker)

    with pytest.raises(ToolError, match="channel_id"):
        await _set_progress_plans_impl(
            _runtime(committing_sessionmaker),
            _auth(tenant_id=tenant_id, admin=False, platform="slack"),
            "on",
            _SLACK_TS,
            None,
        )


async def test_get_reports_every_tier_and_the_winner(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """The narrowest tier with a value wins and is named."""
    tenant_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker)
    await _set_progress_plans_impl(
        runtime, _auth(tenant_id=tenant_id, admin=True), "on", None, None
    )

    status = await _get_progress_plans_impl(
        runtime, _auth(tenant_id=tenant_id, admin=False), _THREAD, None
    )

    assert status.deployment_mode == "off", "the deployment default is reported"
    assert status.workspace_mode == "on", "the workspace tier's own value is reported"
    assert (status.effective_mode, status.effective_tier) == ("on", "workspace"), (
        "an unset thread and channel inherit the workspace"
    )
    assert status.explanation.startswith("Progress plans are on"), "the sentence states the answer"


async def test_the_switch_is_refused_outside_chat() -> None:
    """The plan lives on a chat card; elsewhere there is nothing to turn on."""
    with pytest.raises(ToolError, match="nothing to turn on"):
        await _get_progress_plans_impl(
            MagicMock(), _auth(tenant_id=uuid.uuid4(), admin=True, platform=None), None, None
        )


async def test_update_plan_asks_only_for_steps_and_acknowledges() -> None:
    """The schema holds steps alone: no field may invite the model's reasoning into a tool input."""
    mcp = FastMCP(name="t")
    register_progress_plan_tools(mcp, MagicMock())
    tool = next(tool for tool in await mcp.list_tools() if tool.name == "update_plan")
    schema = json.dumps(tool.parameters).lower()
    text = f"{schema} {(tool.description or '').lower()}"

    for word in ("reason", "explanation", "explain", "note", "thinking", "thought", "why"):
        assert word not in text, f"update_plan's schema or description must not mention {word!r}"
    assert set(tool.parameters["properties"]) == {"steps"}, "steps is the only argument"
    assert tool.tags == {"discord", "slack", "teams"}, "every chat platform sees the tool"

    result = await mcp.call_tool(
        "update_plan", {"steps": [{"step": "Load the data", "status": "in_progress"}]}
    )
    assert "not a reply" in json.dumps(result.content[0].model_dump()), (
        "the ack tells the model to carry on"
    )
