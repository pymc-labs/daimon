"""DB-backed unit tests for the thread-participation MCP tools.

Every refusal is asserted twice: the ToolError, and that the cascade is
unchanged afterwards — a rule that refuses but still writes the row would be
worse than no rule at all.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools import thread_participation as tool_module
from daimon.adapters.mcp.tools.thread_participation import (
    _get_thread_participation_impl,  # pyright: ignore[reportPrivateUsage]
    _set_thread_participation_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    DiscordSettings,
    McpSettings,
    Settings,
    ThreadParticipationSettings,
)
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from daimon.core.stores.thread_participation import (
    ParticipationModes,
    get_participation_modes,
)
from daimon.core.thread_participation import ParticipationMode, ParticipationScope
from daimon.testing.factories import make_account, make_tenant
from fastmcp.exceptions import ToolError
from pydantic import HttpUrl, PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.asyncio

_D28_MESSAGE = "Changing my setup needs Manage Server — ask a server admin to use /agent-setup"
_CHANNEL = "chan-1"
_THREAD = "thread-1"


@pytest.fixture(autouse=True)
def verified_scopes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[ParticipationScope, str | None]]:
    """Stand in for the Discord lookup: every id is visible, and `_THREAD`'s parent is `_CHANNEL`.

    The real `_verify_scope` is exercised against a fake Discord HTTP layer in
    `tools/test_thread_participation_verify.py`; here the cascade logic is under test.
    Patched on the tool module, which is where the tool looks it up.
    """
    calls: list[tuple[ParticipationScope, str | None]] = []

    async def fake(
        runtime: McpRuntime, auth: AuthIdentity, scope: ParticipationScope, scope_id: str | None
    ) -> str | None:
        calls.append((scope, scope_id))
        return _CHANNEL if scope is ParticipationScope.THREAD else None

    monkeypatch.setattr(tool_module, "verify_participation_scope", fake)
    return calls


def _settings(mode: ParticipationMode, *, discord: bool = True) -> Settings:
    """A real Settings so `settings.thread_participation` is the real model."""
    return Settings(
        database=DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@h/d")),
        anthropic=AnthropicSettings(api_key=SecretStr("sk-test")),
        mcp=McpSettings(jwt_secret=SecretStr("a" * 32), public_url=HttpUrl("https://x/mcp")),
        discord=DiscordSettings(bot_token=SecretStr("test-bot-token")) if discord else None,
        thread_participation=ThreadParticipationSettings(mode=mode),
    )


def _runtime(
    sessionmaker: async_sessionmaker[AsyncSession],
    mode: ParticipationMode = ParticipationMode.OFF,
    *,
    discord: bool = True,
) -> McpRuntime:
    return McpRuntime(
        session_factory=sessionmaker,
        client=MagicMock(spec=AsyncAnthropic),  # type: ignore[arg-type]
        settings=_settings(mode, discord=discord),
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


async def _modes(session: AsyncSession, tenant_id: uuid.UUID) -> ParticipationModes:
    return await get_participation_modes(
        session,
        tenant_id=tenant_id,
        platform="discord",
        channel_id=_CHANNEL,
        thread_id=_THREAD,
    )


# ---------------------------------------------------------------------------
# each scope persists
# ---------------------------------------------------------------------------


async def test_member_turns_a_thread_on(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    """Thread scope is the member action the whole feature exists for."""
    tenant_id = await _seed(committing_sessionmaker)

    result = await _set_thread_participation_impl(
        _runtime(committing_sessionmaker),
        _auth(tenant_id=tenant_id, admin=False),
        "on",
        _THREAD,
        _CHANNEL,
    )

    assert result.scope == f"thread:{_THREAD}", "result must name the thread scope"
    assert result.mode == "on", "result must echo the stored mode"
    assert result.effective_mode == "on", "nothing wider objects, so the thread's on wins"
    assert result.effective_tier == "thread", "the thread tier is the narrowest one set"
    assert (await _modes(db_session, tenant_id)).thread is ParticipationMode.ON, (
        "the thread row must be committed"
    )


async def test_member_turns_a_thread_off_again(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker)
    auth = _auth(tenant_id=tenant_id, admin=False)
    await _set_thread_participation_impl(runtime, auth, "on", _THREAD, _CHANNEL)

    result = await _set_thread_participation_impl(runtime, auth, "off", _THREAD, _CHANNEL)

    assert result.mode == "off", "last write wins at the thread scope"
    assert result.effective_mode == "off", "the cascade must reflect the new value"
    assert (await _modes(db_session, tenant_id)).thread is ParticipationMode.OFF, (
        "the thread row must hold off"
    )


async def test_admin_turns_a_channel_on(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant_id = await _seed(committing_sessionmaker)

    result = await _set_thread_participation_impl(
        _runtime(committing_sessionmaker),
        _auth(tenant_id=tenant_id, admin=True),
        "on",
        None,
        _CHANNEL,
    )

    assert result.scope == f"channel:{_CHANNEL}", "channel_id alone means channel scope"
    assert result.effective_tier == "channel", "the channel tier is the narrowest one set"
    assert (await _modes(db_session, tenant_id)).channel is ParticipationMode.ON, (
        "the channel row must be committed"
    )


async def test_admin_turns_the_workspace_on(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant_id = await _seed(committing_sessionmaker)

    result = await _set_thread_participation_impl(
        _runtime(committing_sessionmaker),
        _auth(tenant_id=tenant_id, admin=True),
        "on",
        None,
        None,
    )

    assert result.scope == "workspace", "neither id means workspace scope"
    assert result.effective_tier == "workspace", "the workspace tier is the only one set"
    assert (await _modes(db_session, tenant_id)).workspace is ParticipationMode.ON, (
        "the workspace row must be committed"
    )


async def test_inherit_clears_the_scopes_own_setting(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    """A thread set off under an on channel must be able to fall back to the channel."""
    tenant_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker)
    admin = _auth(tenant_id=tenant_id, admin=True)
    await _set_thread_participation_impl(runtime, admin, "on", None, _CHANNEL)
    await _set_thread_participation_impl(
        runtime, _auth(tenant_id=tenant_id, admin=False), "off", _THREAD, _CHANNEL
    )

    result = await _set_thread_participation_impl(
        runtime, _auth(tenant_id=tenant_id, admin=False), "inherit", _THREAD, _CHANNEL
    )

    assert result.mode == "inherit", "result must report that the scope now inherits"
    assert result.effective_mode == "on", "with its own row gone the thread follows the channel"
    assert result.effective_tier == "channel", "the channel is now the narrowest tier set"
    assert (await _modes(db_session, tenant_id)).thread is None, "the thread row must be gone"


# ---------------------------------------------------------------------------
# refusals: each must leave the cascade untouched
# ---------------------------------------------------------------------------


async def test_non_admin_cannot_set_a_channel_and_writes_nothing(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant_id = await _seed(committing_sessionmaker)

    with pytest.raises(ToolError) as exc_info:
        await _set_thread_participation_impl(
            _runtime(committing_sessionmaker),
            _auth(tenant_id=tenant_id, admin=False),
            "on",
            None,
            _CHANNEL,
        )

    assert str(exc_info.value) == _D28_MESSAGE, "channel scope is an admin action"
    assert (await _modes(db_session, tenant_id)).channel is None, (
        "a refused call must write no channel row"
    )


async def test_non_admin_cannot_set_the_workspace_and_writes_nothing(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant_id = await _seed(committing_sessionmaker)

    with pytest.raises(ToolError) as exc_info:
        await _set_thread_participation_impl(
            _runtime(committing_sessionmaker),
            _auth(tenant_id=tenant_id, admin=False),
            "on",
            None,
            None,
        )

    assert str(exc_info.value) == _D28_MESSAGE, "workspace scope is an admin action"
    assert (await _modes(db_session, tenant_id)).workspace is None, (
        "a refused call must write no workspace row"
    )


async def test_disabled_is_refused_at_thread_scope(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    """`disabled` means "and nothing below may override" — a thread has no below."""
    tenant_id = await _seed(committing_sessionmaker)

    with pytest.raises(ToolError) as exc_info:
        await _set_thread_participation_impl(
            _runtime(committing_sessionmaker),
            _auth(tenant_id=tenant_id, admin=False),
            "disabled",
            _THREAD,
            _CHANNEL,
        )

    assert "off" in str(exc_info.value), "the refusal must point the model at mode='off'"
    assert (await _modes(db_session, tenant_id)).thread is None, "no thread row may be written"


async def test_deployment_disabled_refuses_every_scope(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant_id = await _seed(committing_sessionmaker)

    with pytest.raises(ToolError) as exc_info:
        await _set_thread_participation_impl(
            _runtime(committing_sessionmaker, ParticipationMode.DISABLED),
            _auth(tenant_id=tenant_id, admin=True),
            "on",
            _THREAD,
            _CHANNEL,
        )

    assert "disabled for this deployment" in str(exc_info.value), (
        "the refusal must say the deployment, not the workspace, said no"
    )
    assert (await _modes(db_session, tenant_id)).thread is None, "no row may be written"


async def test_a_deployment_without_discord_settings_refuses(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """No Discord block at all is not a deployment that can follow Discord threads."""
    tenant_id = await _seed(committing_sessionmaker)

    with pytest.raises(ToolError):
        await _set_thread_participation_impl(
            _runtime(committing_sessionmaker, discord=False),
            _auth(tenant_id=tenant_id, admin=True),
            "on",
            _THREAD,
            _CHANNEL,
        )


async def test_a_disabled_channel_refuses_turning_a_thread_on(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    """The lock has to hold from below, or `disabled` is just a slower `off`."""
    tenant_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker)
    await _set_thread_participation_impl(
        runtime, _auth(tenant_id=tenant_id, admin=True), "disabled", None, _CHANNEL
    )

    with pytest.raises(ToolError) as exc_info:
        await _set_thread_participation_impl(
            runtime, _auth(tenant_id=tenant_id, admin=False), "on", _THREAD, _CHANNEL
        )

    assert "channel level" in str(exc_info.value), "the refusal must name the tier that said no"
    assert (await _modes(db_session, tenant_id)).thread is None, "no thread row may be written"


async def test_a_non_discord_caller_is_refused(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    tenant_id = await _seed(committing_sessionmaker)

    with pytest.raises(ToolError) as exc_info:
        await _set_thread_participation_impl(
            _runtime(committing_sessionmaker),
            _auth(tenant_id=tenant_id, admin=True, platform="slack"),
            "on",
            _THREAD,
            _CHANNEL,
        )

    assert "Discord" in str(exc_info.value), "the refusal must say the feature is Discord-only"
    assert (await _modes(db_session, tenant_id)).thread is None, "no row may be written"


# ---------------------------------------------------------------------------
# get_thread_participation
# ---------------------------------------------------------------------------


async def test_get_reports_every_tier_and_the_winner(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker)
    await _set_thread_participation_impl(
        runtime, _auth(tenant_id=tenant_id, admin=True), "on", None, None
    )
    await _set_thread_participation_impl(
        runtime, _auth(tenant_id=tenant_id, admin=True), "off", None, _CHANNEL
    )
    await _set_thread_participation_impl(
        runtime, _auth(tenant_id=tenant_id, admin=False), "on", _THREAD, _CHANNEL
    )

    status = await _get_thread_participation_impl(
        runtime, _auth(tenant_id=tenant_id, admin=False), _THREAD, _CHANNEL
    )

    assert status.deployment_mode == "off", "the deployment default must be reported"
    assert status.workspace_mode == "on", "the workspace tier's own value must be reported"
    assert status.channel_mode == "off", "the overridden channel value must still be reported"
    assert status.thread_mode == "on", "the thread tier's own value must be reported"
    assert status.effective_mode == "on", "the narrowest set tier wins"
    assert status.effective_tier == "thread", "the thread tier is the winner"
    assert "thread" in status.explanation, "the sentence must name the winning tier"


async def test_get_reports_the_all_unset_default(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Off by default: an untouched workspace answers from the deployment tier."""
    tenant_id = await _seed(committing_sessionmaker)

    status = await _get_thread_participation_impl(
        _runtime(committing_sessionmaker),
        _auth(tenant_id=tenant_id, admin=False),
        _THREAD,
        _CHANNEL,
    )

    assert status.effective_mode == "off", "nothing set anywhere means off"
    assert status.effective_tier == "deployment", "the deployment tier is the only one with a value"
    assert (status.workspace_mode, status.channel_mode, status.thread_mode) == (None, None, None), (
        "unset tiers must be reported as unset, not as off"
    )


# ---------------------------------------------------------------------------
# caller-supplied ids
# ---------------------------------------------------------------------------


async def test_a_disabled_channel_refuses_a_thread_even_when_channel_id_is_omitted(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
) -> None:
    """The parent comes from Discord, not from the caller, so leaving channel_id out is not a bypass."""
    tenant_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker)
    await _set_thread_participation_impl(
        runtime, _auth(tenant_id=tenant_id, admin=True), "disabled", None, _CHANNEL
    )

    with pytest.raises(ToolError) as exc_info:
        await _set_thread_participation_impl(
            runtime, _auth(tenant_id=tenant_id, admin=False), "on", _THREAD, None
        )

    assert "channel level" in str(exc_info.value)
    assert (await _modes(db_session, tenant_id)).thread is None, "no thread row may be written"


async def test_every_thread_and_channel_id_is_verified_before_a_write(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    verified_scopes: list[tuple[ParticipationScope, str | None]],
) -> None:
    tenant_id = await _seed(committing_sessionmaker)
    runtime = _runtime(committing_sessionmaker)
    admin = _auth(tenant_id=tenant_id, admin=True)

    await _set_thread_participation_impl(runtime, admin, "on", _THREAD, None)
    await _set_thread_participation_impl(runtime, admin, "on", None, _CHANNEL)
    await _set_thread_participation_impl(runtime, admin, "on", None, None)
    await _get_thread_participation_impl(runtime, admin, _THREAD, None)

    assert verified_scopes == [
        (ParticipationScope.THREAD, _THREAD),
        (ParticipationScope.CHANNEL, _CHANNEL),
        (ParticipationScope.WORKSPACE, None),
        (ParticipationScope.THREAD, _THREAD),
    ]


@pytest.mark.parametrize(("thread_id", "channel_id"), [("", None), (None, ""), ("  ", _CHANNEL)])
async def test_empty_ids_are_refused_without_a_write(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    thread_id: str | None,
    channel_id: str | None,
) -> None:
    tenant_id = await _seed(committing_sessionmaker)

    with pytest.raises(ToolError, match="must not be empty"):
        await _set_thread_participation_impl(
            _runtime(committing_sessionmaker),
            _auth(tenant_id=tenant_id, admin=True),
            "on",
            thread_id,
            channel_id,
        )

    modes = await _modes(db_session, tenant_id)
    assert (modes.workspace, modes.channel, modes.thread) == (None, None, None)


async def test_get_refuses_a_non_discord_caller_too(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = await _seed(committing_sessionmaker)

    with pytest.raises(ToolError, match="Discord"):
        await _get_thread_participation_impl(
            _runtime(committing_sessionmaker, ParticipationMode.ON),
            _auth(tenant_id=tenant_id, admin=False, platform="slack"),
            None,
            None,
        )
