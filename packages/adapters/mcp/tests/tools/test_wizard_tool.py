"""DB-backed unit tests for post_wizard's tool-level impl.

Drives `_post_wizard_impl` (the tool-level one in `tools/wizard.py`, which
delegates to the Discord-side `_post_wizard_impl` from `tools/discord/`) with
a real `committing_sessionmaker` (real Postgres, per guideline:testing), a
staged uploads, and a transport-level patched Discord `HTTPClient`
(`patch_discord_http`, loaded from the sibling conftest.py by file path —
same trick `test_discord_wizard.py`/`test_credential_requests.py` use).

The short_id the tool mints internally is never returned to the caller (the
tool's return value is the stop signal), so tests recover it the same way
`test_credential_requests.py` recovers a minted token: by parsing it back out
of the posted message payload's own `wz:{short_id}:{action}` custom_ids.
"""

from __future__ import annotations

import importlib.util
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import discord
import discord.http
import pytest
from anthropic import AsyncAnthropic
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.wizard import _STOP_SIGNAL, _post_wizard_impl
from daimon.core.config import AnthropicSettings, DatabaseSettings, DiscordSettings, Settings
from daimon.core.media.filenames import display_filename_for
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from daimon.core.stores.file_uploads import create_upload, store_upload_content
from daimon.core.stores.wizard_session import (
    count_wizard_sessions_for_platform_user,
    get_wizard_session,
)
from daimon.core.wizard.expiry import DEFAULT_TTL
from daimon.core.wizard.spec import Option, Step, StepKind
from daimon.testing.factories import make_account, make_tenant
from fastmcp.exceptions import ToolError
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Load patch_discord_http directly from the sibling conftest.py by file path —
# same workaround test_discord_wizard.py / test_credential_requests.py use to
# dodge the "from conftest import ..." collision with the parent tests/conftest.py.
_conftest_path = Path(__file__).parent / "conftest.py"
_spec = importlib.util.spec_from_file_location("_tools_conftest", _conftest_path)
assert _spec is not None and _spec.loader is not None
_tools_conftest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_tools_conftest)
patch_discord_http = _tools_conftest.patch_discord_http


_VIEW_CHANNEL = 1 << 10
_SEND_MESSAGES = 1 << 11

_SHORT_ID_RE = re.compile(r"wz:([A-Za-z0-9]{8}):")


def _runtime(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    with_discord: bool = True,
) -> McpRuntime:
    settings = Settings(
        database=DatabaseSettings(url="postgresql+asyncpg://x/y"),  # pyright: ignore[reportArgumentType]
        anthropic=AnthropicSettings(api_key=SecretStr("k")),
        discord=DiscordSettings(bot_token=SecretStr("test-bot-token")) if with_discord else None,
    )
    return McpRuntime(
        session_factory=sessionmaker,
        client=AsyncAnthropic(api_key="k"),
        settings=settings,
        deployment_default=DeploymentDefault(),
    )


def _auth_identity(
    *,
    platform: str | None = "discord",
    external_id: str | None = "111",
    platform_user_id: str | None = "42",
    tenant_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
) -> AuthIdentity:
    return AuthIdentity(
        account_id=account_id if account_id is not None else uuid.uuid4(),
        tenant_id=tenant_id if tenant_id is not None else uuid.uuid4(),
        role=Role.USER,
        platform=platform,
        external_id=external_id,
        platform_user_id=platform_user_id,
    )


def _guild_payload(*, guild_id: str = "111") -> dict[str, Any]:
    return {
        "id": guild_id,
        "name": "test-guild",
        "owner_id": "1",
        "afk_timeout": 0,
        "verification_level": 0,
        "default_message_notifications": 0,
        "explicit_content_filter": 0,
        "roles": [],
        "emojis": [],
        "features": [],
        "mfa_level": 0,
        "system_channel_flags": 0,
        "premium_tier": 0,
        "preferred_locale": "en-US",
        "nsfw_level": 0,
        "premium_progress_bar_enabled": False,
        "stickers": [],
        "region": "us-east",
    }


def _everyone_role(guild_id: str, perms: int) -> dict[str, Any]:
    return {
        "id": guild_id,
        "name": "@everyone",
        "permissions": str(perms),
        "position": 0,
        "color": 0,
        "hoist": False,
        "managed": False,
        "mentionable": False,
        "flags": 0,
    }


def _member_payload(user_id: str = "42") -> dict[str, Any]:
    return {
        "user": {
            "id": user_id,
            "username": "caller",
            "discriminator": "0001",
            "global_name": "caller",
            "avatar": None,
            "bot": False,
            "flags": 0,
        },
        "roles": [],
        "joined_at": "2024-01-01T00:00:00+00:00",
        "deaf": False,
        "mute": False,
        "flags": 0,
    }


def _text_channel_payload(*, channel_id: str = "222", guild_id: str = "111") -> dict[str, Any]:
    return {
        "id": channel_id,
        "type": 0,
        "guild_id": guild_id,
        "name": "general",
        "position": 0,
        "permission_overwrites": [],
        "nsfw": False,
        "rate_limit_per_user": 0,
        "parent_id": None,
    }


def _author_payload(author_id: str = "1") -> dict[str, Any]:
    return {
        "id": author_id,
        "username": "bot",
        "discriminator": "0001",
        "global_name": "bot",
        "avatar": None,
        "bot": True,
        "flags": 0,
    }


def _message_payload(
    *,
    message_id: str,
    channel_id: str = "222",
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "id": message_id,
        "channel_id": channel_id,
        "author": _author_payload(),
        "content": None,
        "timestamp": "2026-05-09T00:00:00+00:00",
        "edited_timestamp": None,
        "tts": False,
        "mention_everyone": False,
        "mentions": [],
        "mention_roles": [],
        "attachments": attachments or [],
        "embeds": [],
        "type": 0,
        "pinned": False,
        "flags": 32768,
    }


def _happy_path_handler(
    posted: dict[str, Any],
    *,
    message_id: str = "9301",
    attachments: list[dict[str, Any]] | None = None,
) -> Any:
    async def handler(route: discord.http.Route, kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL | _SEND_MESSAGES)]
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            return _text_channel_payload()
        if route.method == "POST" and route.path == "/channels/{channel_id}/messages":
            posted.update(kwargs)
            return _message_payload(message_id=message_id, attachments=attachments)
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    return handler


def _payload_from_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    if "json" in kwargs:
        return kwargs["json"]  # type: ignore[no-any-return]
    for part in kwargs["form"]:
        if part["name"] == "payload_json":
            return json.loads(part["value"])  # type: ignore[no-any-return]
    raise AssertionError(f"no payload_json part in multipart form: {kwargs!r}")


def _short_id_from_posted(posted: dict[str, Any]) -> str:
    payload = _payload_from_kwargs(posted)
    match = _SHORT_ID_RE.search(json.dumps(payload["components"]))
    assert match is not None, f"no wz: custom_id found in posted components: {payload!r}"
    return match.group(1)


def _one_choice_step(**kwargs: Any) -> Step:
    return Step(
        key=kwargs.pop("key", "k"),
        question="q",
        kind=StepKind.CHOICE,
        options=[Option(label="a", value="a")],
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. The stop signal
# ---------------------------------------------------------------------------


async def test_post_wizard_returns_the_stop_signal(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    posted: dict[str, Any] = {}
    patch_discord_http(monkeypatch, _happy_path_handler(posted))
    result = await _post_wizard_impl(
        _runtime(committing_sessionmaker),
        _auth_identity(tenant_id=tenant.id, account_id=account.id),
        prompt="Configure the pipeline",
        steps=[_one_choice_step()],
        channel_id="222",
    )
    assert result == _STOP_SIGNAL, "the tool must return exactly the module's stop-signal constant"
    assert "end your turn" in result.lower(), "the stop signal must tell the agent to end its turn"


# ---------------------------------------------------------------------------
# 2. The stored row
# ---------------------------------------------------------------------------


async def test_post_wizard_stores_the_form_with_its_message_id(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    auth = _auth_identity(tenant_id=tenant.id, account_id=account.id)
    posted: dict[str, Any] = {}
    patch_discord_http(monkeypatch, _happy_path_handler(posted, message_id="9302"))

    await _post_wizard_impl(
        _runtime(committing_sessionmaker),
        auth,
        prompt="Configure the pipeline",
        steps=[_one_choice_step()],
        channel_id="222",
    )

    short_id = _short_id_from_posted(posted)
    row = await get_wizard_session(db_session, short_id=short_id)
    assert row is not None, "the row minted by the post must be readable back through the store"
    assert row.status == "open"
    assert row.current_step == 0
    assert row.answers == {}
    assert row.message_id == "9302"
    assert row.tenant_id == tenant.id
    assert row.account_id == auth.account_id
    assert row.requester_platform_user_id == auth.platform_user_id
    assert row.channel_id == "222"


async def _stage_image(
    session: AsyncSession, tenant_id: uuid.UUID, *, data: bytes, title: str
) -> str:
    """Stage an image the way create_file_upload_url + a sandbox PUT would."""
    now = datetime.now(UTC)
    row, token = await create_upload(
        session,
        tenant_id=tenant_id,
        title=title,
        display_filename=display_filename_for(title, "image/png"),
        content_type="image/png",
        now=now,
    )
    await store_upload_content(session, upload_token=token, data=data, now=now)
    return row.id


# ---------------------------------------------------------------------------
# 3. Per-step content-delivery addresses persist
# ---------------------------------------------------------------------------


async def test_post_wizard_persists_a_content_address_for_every_step_image(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    handle1_id = await _stage_image(db_session, tenant.id, data=b"PNGDATA1", title="step1")
    handle2_id = await _stage_image(db_session, tenant.id, data=b"PNGDATA2", title="step2")
    await db_session.commit()
    posted: dict[str, Any] = {}
    patch_discord_http(
        monkeypatch,
        _happy_path_handler(
            posted,
            message_id="9303",
            attachments=[
                {
                    "id": "8001",
                    "filename": "step1.png",  # matches the staged title ("step1") below
                    "url": "https://cdn.discordapp.com/attachments/1/2/step1.png?ex=abc123",
                    "proxy_url": "https://media.discordapp.net/attachments/1/2/step1.png?ex=abc123",
                    "size": 8,
                },
                {
                    "id": "8002",
                    "filename": "step2.png",  # matches the staged title ("step2") below
                    "url": "https://cdn.discordapp.com/attachments/1/2/step2.png?ex=def456",
                    "proxy_url": "https://media.discordapp.net/attachments/1/2/step2.png?ex=def456",
                    "size": 8,
                },
            ],
        ),
    )

    await _post_wizard_impl(
        _runtime(committing_sessionmaker),
        _auth_identity(tenant_id=tenant.id, account_id=account.id),
        prompt="Configure the pipeline",
        steps=[
            _one_choice_step(key="stage", image_handle=handle1_id),
            _one_choice_step(key="region", image_handle=handle2_id),
        ],
        channel_id="222",
    )

    short_id = _short_id_from_posted(posted)
    row = await get_wizard_session(db_session, short_id=short_id)
    assert row is not None
    steps_by_key = {step["key"]: step for step in row.spec["steps"]}
    assert steps_by_key["stage"]["image_url"] == (
        "https://cdn.discordapp.com/attachments/1/2/step1.png?ex=abc123"
    ), "the stored spec must resolve step one's image url so another process can re-render it"
    assert steps_by_key["region"]["image_url"] == (
        "https://cdn.discordapp.com/attachments/1/2/step2.png?ex=def456"
    ), "the stored spec must resolve every step's image url, not just the displayed one"


# ---------------------------------------------------------------------------
# 4. Expiry follows the image signature
# ---------------------------------------------------------------------------


async def test_post_wizard_expiry_follows_the_image_signature(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    handle_id = await _stage_image(db_session, tenant.id, data=b"PNGDATA", title="x")
    await db_session.commit()
    an_hour_out = int((datetime.now(UTC) + timedelta(hours=1)).timestamp())
    posted: dict[str, Any] = {}
    patch_discord_http(
        monkeypatch,
        _happy_path_handler(
            posted,
            message_id="9304",
            attachments=[
                {
                    "id": "8001",
                    "filename": "x.png",  # matches the staged title ("x") above
                    "url": f"https://cdn.discordapp.com/attachments/1/2/x.png?ex={an_hour_out:x}",
                    "proxy_url": f"https://media.discordapp.net/attachments/1/2/x.png?ex={an_hour_out:x}",
                    "size": 7,
                }
            ],
        ),
    )
    before = datetime.now(UTC)

    await _post_wizard_impl(
        _runtime(committing_sessionmaker),
        _auth_identity(tenant_id=tenant.id, account_id=account.id),
        prompt="Configure the pipeline",
        steps=[_one_choice_step(image_handle=handle_id)],
        channel_id="222",
    )

    short_id = _short_id_from_posted(posted)
    row = await get_wizard_session(db_session, short_id=short_id)
    assert row is not None
    assert row.expires_at < before + timedelta(hours=1), (
        "a form whose image signature expires in an hour must expire under that hour"
    )


async def test_post_wizard_expiry_defaults_with_no_images(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    posted: dict[str, Any] = {}
    patch_discord_http(monkeypatch, _happy_path_handler(posted, message_id="9305"))
    before = datetime.now(UTC)

    await _post_wizard_impl(
        _runtime(committing_sessionmaker),
        _auth_identity(tenant_id=tenant.id, account_id=account.id),
        prompt="Configure the pipeline",
        steps=[_one_choice_step()],
        channel_id="222",
    )

    short_id = _short_id_from_posted(posted)
    row = await get_wizard_session(db_session, short_id=short_id)
    assert row is not None
    assert row.expires_at >= before + DEFAULT_TTL - timedelta(seconds=5), (
        "with no images, the stored expiry must be the default TTL window"
    )


# ---------------------------------------------------------------------------
# 5. A caller-supplied image_url never survives
# ---------------------------------------------------------------------------


async def test_post_wizard_strips_a_caller_supplied_image_url(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    posted: dict[str, Any] = {}
    patch_discord_http(monkeypatch, _happy_path_handler(posted, message_id="9306"))

    await _post_wizard_impl(
        _runtime(committing_sessionmaker),
        _auth_identity(tenant_id=tenant.id, account_id=account.id),
        prompt="Configure the pipeline",
        steps=[_one_choice_step(image_url="https://evil.example.com/x.png")],
        channel_id="222",
    )

    short_id = _short_id_from_posted(posted)
    row = await get_wizard_session(db_session, short_id=short_id)
    assert row is not None
    assert row.spec["steps"][0]["image_url"] is None, (
        "a caller-supplied image_url with no image_handle must never be persisted"
    )


# ---------------------------------------------------------------------------
# 6. Over-ceiling forms raise an actionable error
# ---------------------------------------------------------------------------


async def test_post_wizard_rejects_an_over_ceiling_form(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async def handler(_route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        raise AssertionError("an over-ceiling form must never reach the Discord HTTP layer")

    step = Step(
        key="too_many",
        question="q",
        kind=StepKind.CHOICE,
        options=[Option(label=f"opt{n}", value=f"v{n}") for n in range(26)],
    )
    with pytest.raises(ToolError, match=r"too_many.*(?s:.*)split the form") as excinfo:
        await _post_wizard_impl(
            _runtime(committing_sessionmaker),
            _auth_identity(),
            prompt="Configure the pipeline",
            steps=[step],
            channel_id="222",
        )
    assert "too_many" in str(excinfo.value), "the error must name the offending step's key"


# ---------------------------------------------------------------------------
# 7. A failed post leaves no row
# ---------------------------------------------------------------------------


async def test_post_wizard_writes_no_row_when_the_post_fails(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    auth = _auth_identity(tenant_id=tenant.id, account_id=account.id)

    async def handler(route: discord.http.Route, _kwargs: dict[str, Any]) -> Any:
        if route.path == "/guilds/{guild_id}":
            return _guild_payload()
        if route.path == "/guilds/{guild_id}/roles":
            return [_everyone_role("111", _VIEW_CHANNEL)]  # no send_messages
        if route.path == "/guilds/{guild_id}/members/{member_id}":
            return _member_payload()
        if route.path == "/channels/{channel_id}":
            return _text_channel_payload()
        if route.method == "POST" and route.path == "/channels/{channel_id}/messages":
            raise AssertionError("must not POST when send permission is denied")
        raise AssertionError(f"unexpected route {route.method} {route.path}")

    patch_discord_http(monkeypatch, handler)

    with pytest.raises(ToolError, match="send_messages"):
        await _post_wizard_impl(
            _runtime(committing_sessionmaker),
            auth,
            prompt="Configure the pipeline",
            steps=[_one_choice_step()],
            channel_id="222",
        )

    assert (
        await count_wizard_sessions_for_platform_user(
            db_session, platform_user_id=auth.platform_user_id or "", tenant_id=tenant.id
        )
        == 0
    ), "a failed post must leave no stored form nobody can tap"


# ---------------------------------------------------------------------------
# 8. Slack is refused
# ---------------------------------------------------------------------------


async def test_post_wizard_refuses_on_slack(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    auth = _auth_identity(platform="slack", external_id=None, platform_user_id="U123")
    with pytest.raises(ToolError, match="not supported on Slack"):
        await _post_wizard_impl(
            _runtime(committing_sessionmaker),
            auth,
            prompt="Configure the pipeline",
            steps=[_one_choice_step()],
            channel_id="222",
        )
