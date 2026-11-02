"""Staging-only confirm click: press a Confirm card's button as the QA bot that asked.

Bots can't press Discord buttons, so an automated evaluator can't finish a
journey that ends on a Confirm card. This module lets an operator do it, on
staging only, through the same handler path a real click takes:

- It builds the INTERACTION_CREATE payload Discord would send for a click on
  the named button (the button's custom_id, the requester as member, the real
  card message) and hands it to the gateway's INTERACTION_CREATE parser, the
  bot's server allow-list included. The card's callbacks run unchanged, so
  every real check runs: requester == clicker, the admin/owner re-check, the
  digest and the expiry.
- Only the reply transport differs. Interaction replies go through
  discord.py's `async_context` webhook adapter; for this one click it is
  swapped for `_ClickAdapter`, which edits the card with the bot's own token
  and hands ephemeral replies (refusals) back to the operator instead of
  posting them.

Who may use it. A Unix socket inside the Discord container (0600, same uid)
listens only when the GCE metadata server names a project ending in
`-staging` (or one ending in `-dev` whose deployment name is `lane-preview`, the
QA dev lane), QA bots are allow-listed and DAIMON_TURN__PROVIDER_RUNTIME_FILE
is set. Each request then re-reads `confirm-click.json` next to that runtime
file, which the operator swaps on the host: it must say
``{"enabled": true, "token_sha256": "<64 lowercase hex>"}`` and must not be
writable from inside the container, or every request is refused. The request
must carry the operator token whose SHA-256 matches; the token itself lives
outside the container. The clicker is never free: it must be one of
`discord.qa_bot_user_ids` AND the card's requester, so only cards a QA bot
asked for can be clicked. Every request, allowed or refused, is logged and,
when its server is known, written to the security audit log.

Operator side: ``python -m daimon.adapters.discord.confirm_click`` (token on stdin).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import struct
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import aiohttp
import structlog
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.security_audit import append_event

import discord
from discord.http import MultipartParameters
from discord.webhook.async_ import AsyncWebhookAdapter, async_context

if TYPE_CHECKING:
    from daimon.adapters.discord.bot import DaimonBot
    from daimon.core.config import Settings

__all__ = [
    "SOCKET_PATH",
    "ClickRequest",
    "ConfirmClicker",
    "control_file_path",
    "listen_refusal",
    "load_control",
    "qa_host",
    "start_confirm_click_server",
]

log = structlog.get_logger(__name__)

SOCKET_PATH = "/tmp/daimon-confirm-click/confirm-click.sock"
AUDIT_TOOL = "discord_confirm_click"
RUNTIME_FILE_ENV = "DAIMON_TURN__PROVIDER_RUNTIME_FILE"
CONTROL_FILE_NAME = "confirm-click.json"
STAGING_PROJECT_SUFFIX = "-staging"
#: The one dev lane that counts as a QA host: its project must end in
#: DEV_PROJECT_SUFFIX (from the metadata server) and its deployment name must be
#: QA_LANE_ENVIRONMENT. The name alone is an environment variable, so it never
#: qualifies a host on its own.
DEV_PROJECT_SUFFIX = "-dev"
QA_LANE_ENVIRONMENT = "lane-preview"
METADATA_PROJECT_URL = "http://metadata.google.internal/computeMetadata/v1/project/project-id"
#: Labels of the Confirm card's answer buttons. "Always allow" is left out: it
#: saves a lasting rule, which a test must never install.
BUTTONS = frozenset({"Confirm", "Approve", "Deny"})
#: Longest a click's handler may run before the operator gets a reply.
HANDLER_TIMEOUT_S = 30.0
REQUEST_LIMIT = 16 * 1024
_OPERATOR_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DISPATCH_TASK_PREFIX = "discord-ui-view-dispatch-"
_EPHEMERAL = 64
_CHANNEL_MESSAGE, _DEFERRED_CHANNEL_MESSAGE, _DEFERRED_UPDATE, _UPDATE = 4, 5, 6, 7

Outcome = Literal["allowed", "denied", "error"]


async def gcp_project_id(timeout_s: float = 2.0) -> str | None:
    """This host's GCP project id from the metadata server; None off GCP.

    Environment variables can't change it, so it is the staging check.
    """
    try:
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_s)) as http,
            http.get(METADATA_PROJECT_URL, headers={"Metadata-Flavor": "Google"}) as response,
        ):
            if response.status != 200:
                return None
            return (await response.text()).strip() or None
    except (aiohttp.ClientError, TimeoutError, OSError):
        return None


def qa_host(project_id: str | None, environment_name: str) -> bool:
    """Whether QA clicks may run here: a staging project, or the QA dev lane."""
    if project_id is None:
        return False
    if project_id.endswith(STAGING_PROJECT_SUFFIX):
        return True
    return project_id.endswith(DEV_PROJECT_SUFFIX) and environment_name == QA_LANE_ENVIRONMENT


def control_file_path() -> Path | None:
    """`confirm-click.json` beside the provider runtime file, or None when that is unset."""
    runtime_file = os.environ.get(RUNTIME_FILE_ENV)
    return Path(runtime_file).parent / CONTROL_FILE_NAME if runtime_file else None


def listen_refusal(
    settings: Settings, project_id: str | None, control_file: Path | None
) -> str | None:
    """Why the socket must not listen here, or None when it may."""
    if settings.discord is None or not settings.discord.qa_bot_user_ids:
        return "no QA bot is allow-listed"
    if control_file is None:
        return "no provider runtime file, so no control file"
    if not qa_host(project_id, settings.environment.name):
        return "this is not a staging host or the QA lane"
    return None


def load_control(path: Path | None) -> str | None:
    """The operator token hash while the control file switches clicks on, else None.

    Off when the file is missing, unreadable, malformed, writable from inside
    this container (it must come from the host's read-only mount), not
    ``"enabled": true``, or its hash is not 64 lowercase hex characters.
    """
    if path is None:
        return None
    try:
        if os.access(path, os.W_OK) or os.access(path.parent, os.W_OK):
            return None
        data: object = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    body = cast(dict[str, object], data)
    digest = body.get("token_sha256")
    if body.get("enabled") is not True or not isinstance(digest, str):
        return None
    return digest if _SHA256_RE.fullmatch(digest) else None


@dataclass(frozen=True)
class ClickRequest:
    token: str
    operator: str
    channel_id: int
    message_id: int
    button: str
    as_user_id: str

    @classmethod
    def parse(cls, raw: bytes) -> ClickRequest:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("request must be a JSON object")
        body = cast(dict[str, object], data)
        token, operator = body.get("token"), body.get("operator")
        button, as_user_id = body.get("button"), body.get("as_user_id")
        if not isinstance(token, str) or not token:
            raise ValueError("token is required")
        if not isinstance(operator, str) or not _OPERATOR_RE.fullmatch(operator):
            raise ValueError("operator must be a short label")
        if not isinstance(button, str) or not isinstance(as_user_id, str):
            raise ValueError("button and as_user_id are required")
        if not as_user_id.isdigit():
            raise ValueError("as_user_id must be a Discord user id")
        return cls(
            token=token,
            operator=operator,
            channel_id=_snowflake(body.get("channel_id")),
            message_id=_snowflake(body.get("message_id")),
            button=button,
            as_user_id=as_user_id,
        )


def _snowflake(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isdigit():
        raise ValueError("channel_id and message_id must be Discord ids")
    return int(value)


class _Refused(Exception):
    def __init__(self, reason: str, outcome: Outcome = "denied") -> None:
        super().__init__(reason)
        self.reason = reason
        self.outcome: Outcome = outcome


class _ClickAdapter(AsyncWebhookAdapter):
    """Interaction replies for one test click, sent without an interaction token.

    "Update message" edits and "edit original" calls edit the card itself with
    the bot's token; ephemeral replies are kept for the operator. Calls for any
    other token go to Discord as usual.
    """

    def __init__(self, *, http: Any, channel_id: int, message_id: int, token: str) -> None:  # noqa: ANN401
        super().__init__()
        self.http = http
        self.channel_id = channel_id
        self.message_id = message_id
        self.token = token
        self.replies: list[dict[str, Any]] = []
        self.card_updated = False
        self._original_ephemeral = False

    def _payload(
        self, payload: dict[str, Any] | None, multipart: list[dict[str, Any]] | None
    ) -> dict[str, Any]:
        if payload is not None:
            return payload
        for part in multipart or []:
            if part.get("name") == "payload_json":
                return cast(dict[str, Any], json.loads(part["value"]))
        return {}

    def _keep(self, kind: str, data: dict[str, Any]) -> None:
        # System notices are cards now: their words live in the embed description.
        embeds = cast(list[dict[str, Any]], data.get("embeds") or [])
        words = [str(data.get("content") or ""), *(str(e.get("description") or "") for e in embeds)]
        self.replies.append({"kind": kind, "content": "\n".join(w for w in words if w)})

    async def _edit_card(self, data: dict[str, Any]) -> dict[str, Any]:
        message = await self.http.edit_message(
            self.channel_id,
            self.message_id,
            params=MultipartParameters(payload=data, multipart=None, files=None),
        )
        self.card_updated = True
        self.replies.append({"kind": "card_updated"})
        return cast(dict[str, Any], message)

    async def _post(self, data: dict[str, Any]) -> dict[str, Any]:
        message = await self.http.send_message(
            self.channel_id, params=MultipartParameters(payload=data, multipart=None, files=None)
        )
        self._keep("posted", data)
        return cast(dict[str, Any], message)

    def _ephemeral_message(self, data: dict[str, Any]) -> dict[str, Any]:
        """A stand-in message for an ephemeral reply that was never posted."""
        return {
            "id": str(discord.utils.time_snowflake(discord.utils.utcnow())),
            "channel_id": str(self.channel_id),
            "type": 0,
            "content": data.get("content") or "",
            "author": {
                "id": "0",
                "username": "confirm-click",
                "discriminator": "0",
                "avatar": None,
            },
            "attachments": [],
            "embeds": [],
            "mentions": [],
            "mention_roles": [],
            "pinned": False,
            "mention_everyone": False,
            "tts": False,
            "timestamp": discord.utils.utcnow().isoformat(),
            "edited_timestamp": None,
            "flags": _EPHEMERAL,
            "components": [],
        }

    async def create_interaction_response(  # pyright: ignore[reportIncompatibleMethodOverride]  # async override of a Response-returning method
        self,
        interaction_id: int,
        token: str,
        *,
        session: aiohttp.ClientSession,
        proxy: str | None = None,
        proxy_auth: aiohttp.BasicAuth | None = None,
        params: MultipartParameters,
    ) -> Any:  # noqa: ANN401
        if token != self.token:
            return await super().create_interaction_response(
                interaction_id,
                token,
                session=session,
                proxy=proxy,
                proxy_auth=proxy_auth,
                params=params,
            )
        body = self._payload(params.payload, params.multipart)
        kind = body.get("type")
        data = cast(dict[str, Any], body.get("data") or {})
        ephemeral = bool(int(data.get("flags") or 0) & _EPHEMERAL)
        message_id: str | None = None
        if params.files:
            self._keep("unsent_attachment_reply", data)
        elif kind == _UPDATE:
            await self._edit_card(data)
            message_id = str(self.message_id)
        elif kind == _CHANNEL_MESSAGE:
            if ephemeral:
                self._original_ephemeral = True
                self._keep("ephemeral", data)
            else:
                message_id = (await self._post(data))["id"]
        elif kind == _DEFERRED_CHANNEL_MESSAGE:
            self._original_ephemeral = ephemeral
        elif kind != _DEFERRED_UPDATE:
            self._keep(f"unsupported_response_{kind}", data)
        interaction: dict[str, Any] = {
            "id": str(interaction_id),
            "type": 3,
            "response_message_loading": False,
            "response_message_ephemeral": ephemeral,
        }
        if message_id is not None:
            interaction["response_message_id"] = message_id
        return {"interaction": interaction}

    async def edit_original_interaction_response(  # pyright: ignore[reportIncompatibleMethodOverride]  # async override of a Response-returning method
        self,
        application_id: int,
        token: str,
        *,
        session: aiohttp.ClientSession,
        proxy: str | None = None,
        proxy_auth: aiohttp.BasicAuth | None = None,
        payload: dict[str, Any] | None = None,
        multipart: list[dict[str, Any]] | None = None,
        files: Sequence[discord.File] | None = None,
    ) -> Any:  # noqa: ANN401
        if token != self.token:
            return await super().edit_original_interaction_response(
                application_id,
                token,
                session=session,
                proxy=proxy,
                proxy_auth=proxy_auth,
                payload=payload,
                multipart=multipart,
                files=files,
            )
        data = self._payload(payload, multipart)
        if self._original_ephemeral:
            self._keep("ephemeral_edit", data)
            return self._ephemeral_message(data)
        return await self._edit_card(data)

    async def get_original_interaction_response(  # pyright: ignore[reportIncompatibleMethodOverride]  # async override of a Response-returning method
        self,
        application_id: int,
        token: str,
        *,
        session: aiohttp.ClientSession,
        proxy: str | None = None,
        proxy_auth: aiohttp.BasicAuth | None = None,
    ) -> Any:  # noqa: ANN401
        if token != self.token:
            return await super().get_original_interaction_response(
                application_id, token, session=session, proxy=proxy, proxy_auth=proxy_auth
            )
        return await self.http.get_message(self.channel_id, self.message_id)

    async def delete_original_interaction_response(  # pyright: ignore[reportIncompatibleMethodOverride]  # async override of a Response-returning method
        self,
        application_id: int,
        token: str,
        *,
        session: aiohttp.ClientSession,
        proxy: str | None = None,
        proxy_auth: aiohttp.BasicAuth | None = None,
    ) -> None:
        if token != self.token:
            await super().delete_original_interaction_response(
                application_id, token, session=session, proxy=proxy, proxy_auth=proxy_auth
            )
            return
        # A card is never deleted by a test click.
        self.replies.append({"kind": "unsent_delete"})

    async def execute_webhook(  # pyright: ignore[reportIncompatibleMethodOverride]  # async override of a Response-returning method
        self,
        webhook_id: int,
        token: str,
        *,
        session: aiohttp.ClientSession,
        proxy: str | None = None,
        proxy_auth: aiohttp.BasicAuth | None = None,
        payload: dict[str, Any] | None = None,
        multipart: list[dict[str, Any]] | None = None,
        files: Sequence[discord.File] | None = None,
        thread_id: int | None = None,
        wait: bool = False,
        with_components: bool = False,
    ) -> Any:  # noqa: ANN401
        if token != self.token:
            return await super().execute_webhook(
                webhook_id,
                token,
                session=session,
                proxy=proxy,
                proxy_auth=proxy_auth,
                payload=payload,
                multipart=multipart,
                files=files,
                thread_id=thread_id,
                wait=wait,
                with_components=with_components,
            )
        data = self._payload(payload, multipart)
        if files:
            self._keep("unsent_attachment_followup", data)
            return self._ephemeral_message(data) if wait else None
        if int(data.get("flags") or 0) & _EPHEMERAL:
            self._keep("ephemeral_followup", data)
            return self._ephemeral_message(data) if wait else None
        message = await self._post(data)
        return message if wait else None


def _peer_uid(writer: asyncio.StreamWriter) -> int | None:
    sock = cast(socket.socket | None, writer.get_extra_info("socket"))
    if sock is None or not hasattr(socket, "SO_PEERCRED"):
        return None
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", raw)
    return uid


class ConfirmClicker:
    """Checks one confirm-click request and runs the click through the real handler."""

    def __init__(self, bot: DaimonBot, settings: Settings, control_file: Path) -> None:
        if settings.discord is None:
            raise ValueError("confirm click needs Discord settings")
        self._bot = bot
        self._control_file = control_file
        self._qa_bot_ids = frozenset(settings.discord.qa_bot_user_ids)
        self._lock = asyncio.Lock()

    async def handle(self, request: ClickRequest, *, peer_uid: int | None) -> dict[str, Any]:
        async with self._lock:
            # Unauthenticated requests reach no Discord API: the cache only.
            guild_id = self._cached_guild_id(request.channel_id)
            outcome: Outcome = "error"
            reason = "unexpected error"
            result: dict[str, Any] = {}
            try:
                token_sha256 = load_control(self._control_file)
                if token_sha256 is None:
                    raise _Refused("confirm click is switched off on this host")
                supplied = hashlib.sha256(request.token.encode()).hexdigest()
                if not hmac.compare_digest(supplied, token_sha256):
                    raise _Refused("operator token rejected")
                if guild_id is None:
                    guild_id = await self._fetched_guild_id(request.channel_id)
                result = await self._click(request)
                outcome = "allowed" if result["answered"] else "denied"
                reason = "card answered" if result["answered"] else "the card's handler refused"
            except _Refused as refused:
                outcome, reason = refused.outcome, refused.reason
            except Exception:
                log.exception("discord.confirm_click.failed")
            await self._audit(request, guild_id, outcome, reason, peer_uid)
            return {"outcome": outcome, "reason": reason, **result}

    def _cached_guild_id(self, channel_id: int) -> int | None:
        guild = getattr(self._bot.get_channel(channel_id), "guild", None)
        return guild.id if isinstance(guild, discord.Guild) else None

    async def _fetched_guild_id(self, channel_id: int) -> int | None:
        try:
            channel = await self._bot.fetch_channel(channel_id)
        except discord.HTTPException:
            return None
        guild = getattr(channel, "guild", None)
        return guild.id if isinstance(guild, discord.Guild) else None

    async def _click(self, request: ClickRequest) -> dict[str, Any]:
        bot_user = self._bot.user
        if bot_user is None or not self._bot.is_ready():
            raise _Refused("the bot is not connected yet", "error")
        if request.as_user_id not in self._qa_bot_ids or request.as_user_id == str(bot_user.id):
            raise _Refused("the clicker is not an allow-listed QA bot")
        if request.button not in BUTTONS:
            raise _Refused("not a Confirm card answer button")
        channel = self._bot.get_channel(request.channel_id)
        if channel is None:
            try:
                channel = await self._bot.fetch_channel(request.channel_id)
            except discord.HTTPException:
                raise _Refused("channel not found") from None
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            raise _Refused("only server channels and threads are supported")

        from daimon.adapters.discord.tool_confirmation import confirmation_requester

        state = self._bot._connection  # pyright: ignore[reportPrivateUsage]  # the gateway's own state
        store = state._view_store  # pyright: ignore[reportPrivateUsage]  # discord.py keeps live views here
        items = cast(dict[tuple[int, str], Any], store._views.get(request.message_id, {}))  # pyright: ignore[reportPrivateUsage]
        custom_id: str | None = None
        requester: str | None = None
        view: object = None
        for (component_type, item_custom_id), item in items.items():
            if component_type != discord.ComponentType.button.value:
                continue
            item_requester = confirmation_requester(item.view)
            if item_requester is not None and getattr(item, "label", None) == request.button:
                custom_id, requester, view = item_custom_id, item_requester, item.view
                break
        if custom_id is None or requester is None:
            raise _Refused("no live Confirm card with that button on that message")
        if requester != request.as_user_id:
            raise _Refused("the clicker is not the card's requester")

        message = cast(dict[str, Any], await state.http.get_message(channel.id, request.message_id))
        if str(cast(dict[str, Any], message.get("author") or {}).get("id")) != str(bot_user.id):
            raise _Refused("the card was not posted by this bot")
        guild = channel.guild
        try:
            member = cast(
                dict[str, Any], await state.http.get_member(guild.id, int(request.as_user_id))
            )
        except discord.HTTPException:
            raise _Refused("the clicker is not a member of this server") from None
        raw_channel = cast(dict[str, Any], await state.http.get_channel(channel.id))
        member_obj = discord.Member(data=member, guild=guild, state=state)  # pyright: ignore[reportArgumentType]  # Discord's member payload
        me = cast(discord.Member | None, guild.me)  # None until the guild is chunked
        app_permissions = channel.permissions_for(me).value if me is not None else 0
        token = f"confirm-click-{secrets.token_urlsafe(24)}"
        payload: dict[str, Any] = {
            "id": str(discord.utils.time_snowflake(discord.utils.utcnow())),
            "application_id": str(self._bot.application_id or bot_user.id),
            "type": discord.InteractionType.component.value,
            "token": token,
            "version": 1,
            "guild_id": str(guild.id),
            "channel_id": str(channel.id),
            "channel": raw_channel,
            "member": {**member, "permissions": str(channel.permissions_for(member_obj).value)},
            "message": message,
            "data": {"custom_id": custom_id, "component_type": discord.ComponentType.button.value},
            "app_permissions": str(app_permissions),
            "locale": "en-US",
            "guild_locale": str(guild.preferred_locale),
            "entitlements": [],
            "authorizing_integration_owners": {"0": str(guild.id)},
            "context": 0,
            "attachment_size_limit": 10 * 1024 * 1024,
        }
        adapter = _ClickAdapter(
            http=state.http, channel_id=channel.id, message_id=request.message_id, token=token
        )
        before = asyncio.all_tasks()
        reset = async_context.set(adapter)
        try:
            # The gateway's INTERACTION_CREATE parser, the bot's server filter
            # included; tasks it starts inherit this context, so their replies
            # use `adapter`.
            state.parsers["INTERACTION_CREATE"](payload)
        finally:
            async_context.reset(reset)
        handlers = [
            task
            for task in asyncio.all_tasks() - before
            if task.get_name().startswith(_DISPATCH_TASK_PREFIX)
        ]
        if not handlers:
            raise _Refused("the click was dropped: this server is not served here", "denied")
        _done, pending = await asyncio.wait(handlers, timeout=HANDLER_TIMEOUT_S)
        if pending:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            raise _Refused("the card's handler timed out", "error")
        # Answered means the card's own answer was taken, whether or not its
        # edit reached Discord (the handler swallows a failed edit).
        return {
            "answered": confirmation_requester(view) is None,
            "card_updated": adapter.card_updated,
            "replies": adapter.replies,
        }

    async def _audit(
        self,
        request: ClickRequest,
        guild_id: int | None,
        outcome: Outcome,
        reason: str,
        peer_uid: int | None,
    ) -> None:
        fields = {
            "operator": request.operator,
            "peer_uid": peer_uid,
            "guild_id": guild_id,
            "channel_id": request.channel_id,
            "message_id": request.message_id,
            "button": request.button,
            "as_user_id": request.as_user_id,
            "outcome": outcome,
            "reason": reason,
        }
        log.warning("discord.confirm_click", **fields)
        if guild_id is None:
            return
        try:
            async with self._bot.runtime.sessionmaker.begin() as session:
                await append_event(
                    session,
                    tenant_id=derive_tenant_uuid(platform="discord", workspace_id=str(guild_id)),
                    account_id=None,
                    agent_id=None,
                    platform="discord",
                    platform_user_id=request.as_user_id,
                    tool_name=AUDIT_TOOL,
                    operation=request.button,
                    outcome=outcome,
                    reason=f"{reason}; operator={request.operator}; peer_uid={peer_uid}",
                    token_kind="operator",
                    target_channel_id=str(request.channel_id),
                    target_message_id=str(request.message_id),
                )
        except Exception:
            log.exception("security_audit.write_failed", tool=AUDIT_TOOL)


async def start_confirm_click_server(
    bot: DaimonBot,
    settings: Settings,
    *,
    path: str = SOCKET_PATH,
    control_file: Path | None = None,
) -> asyncio.Server | None:
    """Listen for confirm clicks on a staging host, else None.

    Listening is not enabling: each request is refused until the host's
    control file switches clicks on (`load_control`).
    """
    control = control_file if control_file is not None else control_file_path()
    if settings.discord is None or not settings.discord.qa_bot_user_ids or control is None:
        return None
    refusal = listen_refusal(settings, await gcp_project_id(), control)
    if refusal is not None:
        log.info("discord.confirm_click.not_listening", reason=refusal)
        return None
    clicker = ConfirmClicker(bot, settings, control)
    own_uid = os.getuid()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            uid = _peer_uid(writer)
            if uid is None or uid not in (own_uid, 0):
                response: dict[str, Any] = {"outcome": "denied", "reason": "wrong local user"}
                log.warning("discord.confirm_click", outcome="denied", reason="wrong local user")
            else:
                try:
                    raw = await asyncio.wait_for(reader.readline(), timeout=5)
                    request = ClickRequest.parse(raw)
                except (ValueError, TimeoutError, asyncio.LimitOverrunError) as err:
                    response = {"outcome": "denied", "reason": f"bad request: {err}"}
                    log.warning("discord.confirm_click", outcome="denied", reason="bad request")
                else:
                    response = await clicker.handle(request, peer_uid=uid)
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()

    directory = Path(path).parent
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Never listen in a directory someone else made or redirected.
        if directory.is_symlink() or directory.stat().st_uid != own_uid:
            log.warning(
                "discord.confirm_click.refused_to_start", reason="socket directory not ours"
            )
            return None
        directory.chmod(0o700)
        with contextlib.suppress(FileNotFoundError):
            Path(path).unlink()
        server = await asyncio.start_unix_server(serve, path=path, limit=REQUEST_LIMIT)
        Path(path).chmod(0o600)
    except OSError:
        log.exception("discord.confirm_click.refused_to_start", reason="socket setup failed")
        return None
    log.warning("discord.confirm_click.listening", path=path, control_file=str(control))
    return server


async def _send(socket_path: str, request: dict[str, Any]) -> dict[str, Any]:
    reader, writer = await asyncio.open_unix_connection(socket_path, limit=1024 * 1024)
    try:
        writer.write(json.dumps(request).encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=HANDLER_TIMEOUT_S + 30)
    finally:
        writer.close()
    return cast(dict[str, Any], json.loads(line))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m daimon.adapters.discord.confirm_click",
        description="Press a Confirm card button as the QA bot that asked (staging only). "
        "Reads the operator token from stdin.",
    )
    parser.add_argument("--channel-id", required=True)
    parser.add_argument("--message-id", required=True)
    parser.add_argument("--button", required=True, choices=sorted(BUTTONS))
    parser.add_argument("--as-user-id", required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--socket", default=SOCKET_PATH)
    args = parser.parse_args(argv)
    token = sys.stdin.readline().strip()
    if not token:
        print(json.dumps({"outcome": "denied", "reason": "no token on stdin"}))
        return 2
    try:
        response = asyncio.run(
            _send(
                args.socket,
                {
                    "token": token,
                    "operator": args.operator,
                    "channel_id": args.channel_id,
                    "message_id": args.message_id,
                    "button": args.button,
                    "as_user_id": args.as_user_id,
                },
            )
        )
    except (FileNotFoundError, ConnectionRefusedError):
        print(json.dumps({"outcome": "denied", "reason": "confirm click is not listening here"}))
        return 2
    print(json.dumps(response))
    return 0 if response.get("outcome") == "allowed" else 1


if __name__ == "__main__":
    sys.exit(main())
