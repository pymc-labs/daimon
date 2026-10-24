"""Staging confirm click: refused unless allowed, and otherwise the real click path runs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import discord
import pytest
from daimon.adapters.discord import confirm_click
from daimon.adapters.discord.confirm_click import (
    ClickRequest,
    ConfirmClicker,
    control_file_path,
    listen_refusal,
    load_control,
    start_confirm_click_server,
)
from daimon.adapters.discord.tool_confirmation import (
    _ConfirmationView,  # pyright: ignore[reportPrivateUsage]  # the real card view under test
)
from daimon.core.config import Settings
from daimon.core.confirmation import ConfirmationAnswer, ConfirmationPrompt
from daimon.core.posted_controls.confirmation import build_confirmation_card

TOKEN = "operator-token-for-tests"
TOKEN_SHA256 = hashlib.sha256(TOKEN.encode()).hexdigest()
GUILD, CHANNEL, CARD, BOT, QA, OTHER_QA, HUMAN = 10, 20, 30, 40, 111, 222, 333


def _settings(qa: tuple[str, ...] = (str(QA), str(OTHER_QA))) -> Settings:
    return Settings.model_validate(
        {
            "database": {"url": "postgresql+asyncpg://localhost/offline"},
            "anthropic": {"api_key": "offline"},
            "discord": {"bot_token": "offline", "qa_bot_user_ids": qa},
        }
    )


def _write_control(directory: Path, body: object, *, read_only: bool = True) -> Path:
    """A control file as the host's read-only mount presents it."""
    directory.mkdir(exist_ok=True)
    directory.chmod(0o700)
    path = directory / "confirm-click.json"
    if path.exists():
        path.chmod(0o600)
    path.write_text(body if isinstance(body, str) else json.dumps(body))
    if read_only:
        path.chmod(0o400)
        directory.chmod(0o500)
    return path


@pytest.fixture
def control(tmp_path: Path) -> Iterator[Path]:
    path = _write_control(tmp_path / "mount", {"enabled": True, "token_sha256": TOKEN_SHA256})
    yield path
    # Let pytest clean the tree up.
    path.parent.chmod(0o700)


def _user(user_id: int, *, bot: bool = False) -> dict[str, Any]:
    return {
        "id": str(user_id),
        "username": f"u{user_id}",
        "discriminator": "0",
        "avatar": None,
        "bot": bot,
    }


def _message() -> dict[str, Any]:
    return {
        "id": str(CARD),
        "channel_id": str(CHANNEL),
        "type": 0,
        "content": "",
        "author": _user(BOT, bot=True),
        "attachments": [],
        "embeds": [],
        "mentions": [],
        "mention_roles": [],
        "pinned": False,
        "mention_everyone": False,
        "tts": False,
        "timestamp": datetime.now(UTC).isoformat(),
        "edited_timestamp": None,
        "flags": 1 << 15,
        "components": [],
    }


def _channel_payload() -> dict[str, Any]:
    return {
        "id": str(CHANNEL),
        "type": 0,
        "guild_id": str(GUILD),
        "name": "v2-eval",
        "position": 0,
        "permission_overwrites": [],
        "nsfw": False,
        "parent_id": None,
    }


def _member(user_id: int) -> dict[str, Any]:
    return {
        "user": _user(user_id, bot=True),
        "roles": [],
        "joined_at": datetime.now(UTC).isoformat(),
        "deaf": False,
        "mute": False,
        "flags": 0,
    }


def _not_found() -> discord.NotFound:
    return discord.NotFound(cast(Any, SimpleNamespace(status=404, reason="Not Found")), "Unknown")


class _FakeHTTP:
    """discord.py's HTTP client, minus Discord: records card edits and posts."""

    proxy = None
    proxy_auth = None

    def __init__(self) -> None:
        setattr(self, "_HTTPClient__session", None)  # noqa: B010  # Interaction reads the mangled name
        self.edits: list[dict[str, Any]] = []
        self.posts: list[dict[str, Any]] = []
        self.members = {QA, OTHER_QA}
        self.edit_error: Exception | None = None
        self.calls = 0

    async def get_message(self, channel_id: int, message_id: int) -> dict[str, Any]:
        self.calls += 1
        assert (channel_id, message_id) == (CHANNEL, CARD)
        return _message()

    async def get_member(self, guild_id: int, user_id: int) -> dict[str, Any]:
        self.calls += 1
        if user_id not in self.members:
            raise _not_found()
        return _member(user_id)

    async def get_channel(self, channel_id: int) -> dict[str, Any]:
        self.calls += 1
        return _channel_payload()

    async def edit_message(self, channel_id: int, message_id: int, *, params: Any) -> Any:
        if self.edit_error is not None:
            raise self.edit_error
        self.edits.append(params.payload)
        return _message()

    async def send_message(self, channel_id: int, *, params: Any) -> Any:
        self.posts.append(params.payload)
        return _message()


class _Audit:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[None]:
        yield None

    async def append(self, _session: object, **fields: Any) -> None:
        self.events.append(fields)


class _World:
    """A real discord.py ConnectionState with one guild, channel and live card."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.client = discord.Client(intents=discord.Intents.none())
        # A started client has its loop; gateway events schedule listeners on it.
        self.client.loop = asyncio.get_running_loop()
        state = self.client._connection  # pyright: ignore[reportPrivateUsage]
        self.http = _FakeHTTP()
        state.http = self.http  # pyright: ignore[reportAttributeAccessIssue]
        state.user = discord.ClientUser(state=state, data=_user(BOT, bot=True))  # pyright: ignore[reportArgumentType]
        state.application_id = BOT
        guild = discord.Guild(
            data={  # pyright: ignore[reportArgumentType]
                "id": str(GUILD),
                "name": "staging",
                "roles": [
                    {
                        "id": str(GUILD),
                        "name": "@everyone",
                        "permissions": str(discord.Permissions.text().value),
                        "position": 0,
                        "color": 0,
                        "hoist": False,
                        "managed": False,
                        "mentionable": False,
                        "flags": 0,
                    }
                ],
                "preferred_locale": "en-US",
            },
            state=state,
        )
        self.channel = discord.TextChannel(state=state, guild=guild, data=_channel_payload())  # pyright: ignore[reportArgumentType]
        guild._add_channel(self.channel)  # pyright: ignore[reportPrivateUsage]
        state._add_guild(guild)  # pyright: ignore[reportPrivateUsage]
        self.audit = _Audit()
        monkeypatch.setattr(confirm_click, "append_event", self.audit.append)
        self.fetch_channel = AsyncMock(side_effect=_not_found())
        self.bot = SimpleNamespace(
            user=state.user,
            application_id=BOT,
            _connection=state,
            is_ready=lambda: True,
            get_channel=self._get_channel,
            fetch_channel=self.fetch_channel,
            runtime=SimpleNamespace(sessionmaker=self.audit),
        )
        self.state = state

    def _get_channel(self, channel_id: int) -> discord.TextChannel | None:
        return self.channel if channel_id == CHANNEL else None

    def allow_only_guild(self, allowed: int) -> None:
        """The bot's own server filter, as `DaimonBot.__init__` installs it."""
        parse = self.state.parsers["INTERACTION_CREATE"]

        def parse_allowed(data: dict[str, Any]) -> None:
            if int(data["guild_id"]) == allowed:
                parse(data)

        self.state.parsers["INTERACTION_CREATE"] = parse_allowed

    def post_card(
        self,
        *,
        requester: int = QA,
        expires_in: timedelta = timedelta(minutes=10),
        admin: bool = False,
        check_admin: Any = None,
    ) -> asyncio.Future[ConfirmationAnswer]:
        prompt = ConfirmationPrompt(
            title="Switch daimon to GPT?",
            requester_platform_user_id=str(requester),
            expires_at=datetime.now(UTC) + expires_in,
            admin=admin,
        )
        answer: asyncio.Future[ConfirmationAnswer] = asyncio.get_running_loop().create_future()
        card = build_confirmation_card(prompt, state="pending", token="tok_abcdefgh")
        view = _ConfirmationView(card, prompt, answer, check_admin)
        self.state.store_view(view, CARD)
        return answer

    def clicker(self, control: Path, settings: Settings | None = None) -> ConfirmClicker:
        return ConfirmClicker(cast(Any, self.bot), settings or _settings(), control)


def _request(**overrides: Any) -> ClickRequest:
    fields: dict[str, Any] = {
        "token": TOKEN,
        "operator": "nc-eval",
        "channel_id": CHANNEL,
        "message_id": CARD,
        "button": "Approve",
        "as_user_id": str(QA),
        **overrides,
    }
    return ClickRequest(**fields)


# ---- where it may listen ----


@pytest.mark.parametrize(
    ("settings", "project", "control_file", "reason"),
    [
        (_settings(qa=()), "example-staging", Path("/run/x.json"), "no QA bot"),
        (_settings(), "example-staging", None, "no provider runtime file"),
        (_settings(), None, Path("/run/x.json"), "not a staging host"),
        (_settings(), "example-prod", Path("/run/x.json"), "not a staging host"),
        (_settings(), "example-staging-not", Path("/run/x.json"), "not a staging host"),
    ],
)
def test_listening_is_refused_unless_qa_runtime_file_and_staging(
    settings: Settings, project: str | None, control_file: Path | None, reason: str
) -> None:
    refusal = listen_refusal(settings, project, control_file)
    assert refusal is not None and reason in refusal


def test_listening_is_allowed_on_a_staging_project() -> None:
    assert listen_refusal(_settings(), "example-staging", Path("/run/x.json")) is None


def test_control_file_sits_beside_the_provider_runtime_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", raising=False)
    assert control_file_path() is None
    monkeypatch.setenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", "/run/daimon-provider/runtime.json")
    assert control_file_path() == Path("/run/daimon-provider/confirm-click.json")


async def test_no_server_without_qa_bots_or_runtime_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, control: Path
) -> None:
    lookup = AsyncMock(return_value="example-staging")
    monkeypatch.setattr(confirm_click, "gcp_project_id", lookup)
    monkeypatch.delenv("DAIMON_TURN__PROVIDER_RUNTIME_FILE", raising=False)
    path = str(tmp_path / "s" / "sock")
    assert await start_confirm_click_server(cast(Any, None), _settings(), path=path) is None
    assert (
        await start_confirm_click_server(
            cast(Any, None), _settings(qa=()), path=path, control_file=control
        )
        is None
    )
    lookup.assert_not_awaited()
    assert not Path(path).exists()


async def test_no_server_outside_staging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, control: Path
) -> None:
    monkeypatch.setattr(confirm_click, "gcp_project_id", AsyncMock(return_value="example-prod"))
    path = str(tmp_path / "s" / "sock")
    assert (
        await start_confirm_click_server(
            cast(Any, None), _settings(), path=path, control_file=control
        )
        is None
    )
    assert not Path(path).exists()


async def test_no_server_in_a_redirected_socket_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, control: Path
) -> None:
    monkeypatch.setattr(confirm_click, "gcp_project_id", AsyncMock(return_value="example-staging"))
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    path = str(tmp_path / "link" / "sock")
    assert (
        await start_confirm_click_server(
            cast(Any, None), _settings(), path=path, control_file=control
        )
        is None
    )
    assert not (tmp_path / "real" / "sock").exists()


# ---- the host's control file ----


@pytest.mark.parametrize(
    ("body", "read_only"),
    [
        ({"enabled": False, "token_sha256": TOKEN_SHA256}, True),
        ({"enabled": "true", "token_sha256": TOKEN_SHA256}, True),
        ({"enabled": True}, True),
        ({"enabled": True, "token_sha256": ""}, True),
        ({"enabled": True, "token_sha256": TOKEN_SHA256.upper()}, True),
        ({"enabled": True, "token_sha256": TOKEN_SHA256[:-1]}, True),
        ([], True),
        ("{not json", True),
        ({"enabled": True, "token_sha256": TOKEN_SHA256}, False),
    ],
)
def test_control_file_is_off_unless_exactly_enabled_and_read_only(
    tmp_path: Path, body: object, read_only: bool
) -> None:
    path = _write_control(tmp_path / "mount", body, read_only=read_only)
    try:
        assert load_control(path) is None
    finally:
        path.parent.chmod(0o700)


def test_control_file_is_off_when_its_directory_is_writable(tmp_path: Path) -> None:
    path = _write_control(tmp_path / "mount", {"enabled": True, "token_sha256": TOKEN_SHA256})
    path.parent.chmod(0o700)
    assert load_control(path) is None


def test_control_file_missing_is_off(tmp_path: Path) -> None:
    assert load_control(tmp_path / "none.json") is None
    assert load_control(None) is None


def test_control_file_enabled_gives_the_hash(control: Path) -> None:
    assert load_control(control) == TOKEN_SHA256


# ---- refusals before any click ----


async def test_switched_off_host_refuses_before_any_discord_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    world = _World(monkeypatch)
    answer = world.post_card()
    result = await world.clicker(tmp_path / "missing.json").handle(_request(), peer_uid=1000)
    assert result["outcome"] == "denied"
    assert "switched off" in result["reason"]
    assert not answer.done()
    assert world.http.calls == 0


async def test_wrong_token_makes_no_discord_api_call(
    monkeypatch: pytest.MonkeyPatch, control: Path
) -> None:
    world = _World(monkeypatch)
    world.post_card()
    result = await world.clicker(control).handle(
        _request(token="wrong", channel_id=CHANNEL + 1), peer_uid=1000
    )
    assert result["reason"] == "operator token rejected"
    world.fetch_channel.assert_not_awaited()
    assert world.http.calls == 0


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"token": "wrong"}, "operator token rejected"),
        ({"as_user_id": str(HUMAN)}, "not an allow-listed QA bot"),
        ({"as_user_id": str(BOT)}, "not an allow-listed QA bot"),
        ({"button": "Details"}, "not a Confirm card answer button"),
        ({"button": "Always allow"}, "not a Confirm card answer button"),
        ({"message_id": CARD + 1}, "no live Confirm card"),
        ({"channel_id": CHANNEL + 1}, "channel not found"),
    ],
)
async def test_refusals_click_nothing_and_are_audited(
    monkeypatch: pytest.MonkeyPatch, control: Path, overrides: dict[str, Any], reason: str
) -> None:
    world = _World(monkeypatch)
    answer = world.post_card()
    result = await world.clicker(control, _settings(qa=(str(QA), str(OTHER_QA), str(BOT)))).handle(
        _request(**overrides), peer_uid=1000
    )
    assert result["outcome"] == "denied"
    assert reason in result["reason"]
    assert not answer.done()
    assert world.http.edits == []
    if overrides.get("channel_id") is None:
        (event,) = world.audit.events
        assert event["tool_name"] == "discord_confirm_click"
        assert event["outcome"] == "denied"
        assert event["token_kind"] == "operator"
        assert event["target_message_id"] == str(overrides.get("message_id", CARD))
        assert "operator=nc-eval" in event["reason"]
        assert TOKEN not in json.dumps(event, default=str)
    else:
        # No server, so no tenant for an audit row: the structured log has it.
        assert world.audit.events == []


async def test_another_requesters_card_is_refused(
    monkeypatch: pytest.MonkeyPatch, control: Path
) -> None:
    world = _World(monkeypatch)
    answer = world.post_card(requester=QA)
    result = await world.clicker(control).handle(_request(as_user_id=str(OTHER_QA)), peer_uid=1000)
    assert result["outcome"] == "denied"
    assert "not the card's requester" in result["reason"]
    assert not answer.done()
    assert world.http.edits == []


async def test_a_humans_card_is_refused(monkeypatch: pytest.MonkeyPatch, control: Path) -> None:
    world = _World(monkeypatch)
    answer = world.post_card(requester=HUMAN)
    for clicker in (str(QA), str(HUMAN)):
        result = await world.clicker(control).handle(_request(as_user_id=clicker), peer_uid=1000)
        assert result["outcome"] == "denied"
    assert not answer.done()


async def test_a_server_the_bot_does_not_serve_drops_the_click(
    monkeypatch: pytest.MonkeyPatch, control: Path
) -> None:
    world = _World(monkeypatch)
    world.allow_only_guild(GUILD + 1)
    answer = world.post_card()
    result = await world.clicker(control).handle(_request(), peer_uid=1000)
    assert result["outcome"] == "denied"
    assert "not served here" in result["reason"]
    assert not answer.done()
    assert world.http.edits == []


# ---- the real click path ----


async def test_allowed_click_runs_the_real_handler_and_edits_the_card(
    monkeypatch: pytest.MonkeyPatch, control: Path
) -> None:
    world = _World(monkeypatch)
    world.allow_only_guild(GUILD)
    seen: list[discord.Interaction] = []
    world.client.event(_on_interaction(seen))
    answer = world.post_card()
    result = await world.clicker(control).handle(_request(), peer_uid=1000)
    assert result["outcome"] == "allowed", result
    assert result["answered"] is True and result["card_updated"] is True
    assert await asyncio.wait_for(answer, 1) == "approved"
    (edit,) = world.http.edits
    assert "Approved" in json.dumps(edit)
    assert result["replies"] == [{"kind": "card_updated"}]
    (event,) = world.audit.events
    assert event["outcome"] == "allowed"
    assert event["platform_user_id"] == str(QA)
    assert event["operation"] == "Approve"
    # The same gateway event a real click raises, with the requester as user.
    await asyncio.sleep(0)
    assert [str(i.user.id) for i in seen] == [str(QA)]
    assert seen[0].type is discord.InteractionType.component


async def test_a_failed_card_edit_still_audits_the_answer(
    monkeypatch: pytest.MonkeyPatch, control: Path
) -> None:
    world = _World(monkeypatch)
    world.http.edit_error = discord.HTTPException(
        cast(Any, SimpleNamespace(status=500, reason="boom")), "boom"
    )
    answer = world.post_card()
    result = await world.clicker(control).handle(_request(), peer_uid=1000)
    assert await asyncio.wait_for(answer, 1) == "approved"
    assert result["outcome"] == "allowed"
    assert result["card_updated"] is False
    assert world.audit.events[-1]["outcome"] == "allowed"


async def test_deny_answers_denied(monkeypatch: pytest.MonkeyPatch, control: Path) -> None:
    world = _World(monkeypatch)
    answer = world.post_card()
    result = await world.clicker(control).handle(_request(button="Deny"), peer_uid=1000)
    assert result["outcome"] == "allowed"
    assert await asyncio.wait_for(answer, 1) == "denied"


async def test_a_second_click_finds_no_live_card(
    monkeypatch: pytest.MonkeyPatch, control: Path
) -> None:
    world = _World(monkeypatch)
    world.post_card()
    clicker = world.clicker(control)
    assert (await clicker.handle(_request(), peer_uid=1000))["outcome"] == "allowed"
    second = await clicker.handle(_request(button="Deny"), peer_uid=1000)
    assert second["outcome"] == "denied"
    assert len(world.http.edits) == 1


async def test_admin_recheck_runs_with_the_requester(
    monkeypatch: pytest.MonkeyPatch, control: Path
) -> None:
    world = _World(monkeypatch)
    check_admin = AsyncMock(return_value="This change needs a current admin.")
    answer = world.post_card(admin=True, check_admin=check_admin)
    result = await world.clicker(control).handle(_request(button="Confirm"), peer_uid=1000)
    check_admin.assert_awaited_once()
    assert check_admin.await_args is not None and check_admin.await_args.args[1] == str(QA)
    assert result["outcome"] == "denied"
    assert result["reason"] == "the card's handler refused"
    assert result["replies"] == [
        {"kind": "ephemeral", "content": "This change needs a current admin."}
    ]
    assert not answer.done()
    assert world.http.edits == []


async def test_expired_card_is_refused_by_the_handler(
    monkeypatch: pytest.MonkeyPatch, control: Path
) -> None:
    world = _World(monkeypatch)
    answer = world.post_card(expires_in=timedelta(seconds=-1))
    result = await world.clicker(control).handle(_request(), peer_uid=1000)
    assert result["outcome"] == "denied"
    assert result["replies"][0]["kind"] == "ephemeral"
    assert not answer.done()
    assert world.http.edits == []


async def test_a_stuck_handler_is_cancelled(monkeypatch: pytest.MonkeyPatch, control: Path) -> None:
    world = _World(monkeypatch)
    monkeypatch.setattr(confirm_click, "HANDLER_TIMEOUT_S", 0.05)
    cancelled = asyncio.Event()

    async def stuck(_prompt: object, _clicker: str) -> str | None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return None

    answer = world.post_card(admin=True, check_admin=stuck)
    result = await world.clicker(control).handle(_request(button="Confirm"), peer_uid=1000)
    assert result["outcome"] == "error"
    assert "timed out" in result["reason"]
    assert cancelled.is_set()
    assert not answer.done()


def _on_interaction(seen: list[discord.Interaction]) -> Any:
    async def on_interaction(interaction: discord.Interaction) -> None:
        seen.append(interaction)

    return on_interaction


# ---- socket + CLI ----


async def test_socket_is_private_and_rejects_bad_requests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, control: Path
) -> None:
    world = _World(monkeypatch)
    monkeypatch.setattr(confirm_click, "gcp_project_id", AsyncMock(return_value="example-staging"))
    path = str(tmp_path / "s" / "sock")
    server = await start_confirm_click_server(
        cast(Any, world.bot), _settings(), path=path, control_file=control
    )
    assert server is not None
    try:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(Path(path).parent).st_mode) == 0o700
        bad = await confirm_click._send(path, {"operator": "nc-eval"})  # pyright: ignore[reportPrivateUsage]
        assert bad["outcome"] == "denied" and "bad request" in bad["reason"]
        world.post_card()
        good = await confirm_click._send(  # pyright: ignore[reportPrivateUsage]
            path,
            {
                "token": TOKEN,
                "operator": "nc-eval",
                "channel_id": str(CHANNEL),
                "message_id": str(CARD),
                "button": "Approve",
                "as_user_id": str(QA),
            },
        )
        assert good["outcome"] == "allowed"
        assert world.audit.events[-1]["reason"].endswith(f"peer_uid={os.getuid()}")
        # Unknown local caller: refused before reading the request.
        monkeypatch.setattr(confirm_click, "_peer_uid", _no_peer)
        unknown = await confirm_click._send(path, {"token": TOKEN})  # pyright: ignore[reportPrivateUsage]
        assert unknown == {"outcome": "denied", "reason": "wrong local user"}
    finally:
        server.close()
        await server.wait_closed()


def test_cli_without_a_server_says_it_is_not_listening(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", SimpleNamespace(readline=lambda: TOKEN + "\n"))
    code = confirm_click.main(
        [
            "--channel-id",
            "1",
            "--message-id",
            "2",
            "--button",
            "Confirm",
            "--as-user-id",
            "3",
            "--operator",
            "nc-eval",
            "--socket",
            str(tmp_path / "none.sock"),
        ]
    )
    assert code == 2
    assert "not listening" in capsys.readouterr().out


def _no_peer(_writer: object) -> int | None:
    return None
