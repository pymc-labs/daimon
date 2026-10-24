"""Bounded Microsoft Graph operations for Teams QA target and bot chat."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from html import escape, unescape
from typing import Any, cast

import httpx

from qa.live.teams.config import TeamsQaConfig
from qa.live.teams.tokens import TokenStore

BASE = "https://graph.microsoft.com/v1.0"
READ = ["ChannelMessage.Read.All"]
SEND = ["ChannelMessage.Send"]
TARGET = ["Team.ReadBasic.All", "Channel.ReadBasic.All", "User.Read"]
CHAT = ["Chat.ReadWrite", "User.Read"]


class GraphRequestError(RuntimeError):
    """A Graph response or read request that cannot be used."""


class AmbiguousPostError(GraphRequestError):
    """A POST may have reached Graph."""


class PostOutcomeUnknown(GraphRequestError):
    """The one POST was not confirmed by a bounded history search."""


class NoBotChat(GraphRequestError):
    """The persona has not established a one-on-one chat with the bot."""


class PersonaCollision(GraphRequestError):
    """The two role personas have the same Graph access-token identity."""


def graph_time(value: object) -> datetime | None:
    """Parse a Graph timestamp without assuming its UTC spelling."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def bot_sender_id(message: dict[str, Any]) -> str | None:
    """Read the app ID from a Graph chat message sender."""
    sender: Any = message.get("from")
    if not isinstance(sender, dict):
        return None
    application = cast(dict[str, object], sender).get("application")
    if not isinstance(application, dict):
        return None
    value = cast(dict[str, object], application).get("id")
    return value if isinstance(value, str) else None


def user_sender_id(message: dict[str, Any]) -> str | None:
    """Read the user object ID from a Graph chat message sender."""
    sender: Any = message.get("from")
    if not isinstance(sender, dict):
        return None
    user = cast(dict[str, object], sender).get("user")
    if not isinstance(user, dict):
        return None
    value = cast(dict[str, object], user).get("id")
    return value if isinstance(value, str) else None


class GraphTeamsDriver:
    def __init__(
        self,
        config: TeamsQaConfig,
        tokens: TokenStore,
        http: httpx.AsyncClient,
        *,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.config = config
        self.tokens = tokens
        self.http = http
        self.now = now
        self.sleep = sleep

    @property
    def channel(self) -> str:
        """Return the configured team and channel path."""
        return f"/teams/{self.config.team_id}/channels/{self.config.channel_id}"

    async def _request(
        self,
        persona: str,
        scopes: list[str],
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        token = self.tokens.acquire(persona, scopes)
        delay = 1.0
        for _attempt in range(1 if method == "POST" else 5):
            if deadline is not None and self.now() >= deadline:
                raise GraphRequestError("Graph request deadline reached")
            try:
                response = await self.http.request(
                    method,
                    BASE + path,
                    headers={"Authorization": f"Bearer {token.value}"},
                    json=payload,
                    timeout=min(15, max(0.01, deadline - self.now()))
                    if deadline is not None
                    else 15,
                )
            except httpx.RequestError:
                if method == "POST":
                    raise AmbiguousPostError("Graph POST outcome unknown") from None
                raise GraphRequestError("Graph transport failed") from None
            if method == "POST" and (response.status_code == 429 or response.status_code >= 500):
                raise AmbiguousPostError("Graph POST outcome unknown")
            if response.status_code == 429 or 500 <= response.status_code <= 599:
                if response.status_code == 429 and deadline is None:
                    deadline = self.now() + 30
                if deadline is None or self.now() >= deadline:
                    if method == "POST" and response.status_code >= 500:
                        raise AmbiguousPostError("Graph POST outcome unknown")
                    raise GraphRequestError("Graph request deadline reached")
                retry = response.headers.get("Retry-After", "")
                wait = float(retry) if retry.replace(".", "", 1).isdigit() else delay
                await self.sleep(min(wait, max(0, deadline - self.now())))
                delay = min(delay * 2, 8)
                continue
            if response.is_error:
                raise GraphRequestError(f"Graph returned HTTP {response.status_code}")
            return response.json()
        raise GraphRequestError("Graph request deadline reached")

    async def verify_target(self, persona: str) -> None:
        """Refuse a Graph target whose IDs or display names differ from config."""
        team = await self._request(persona, TARGET, "GET", f"/teams/{self.config.team_id}")
        channel = await self._request(persona, TARGET, "GET", self.channel)
        if (team.get("id"), team.get("displayName")) != (
            self.config.team_id,
            self.config.team_display_name,
        ):
            raise ValueError("team target mismatch")
        if (channel.get("id"), channel.get("displayName")) != (
            self.config.channel_id,
            self.config.channel_display_name,
        ):
            raise ValueError("channel target mismatch")
        if not self.config.bot_app_id:
            raise ValueError("bot app identity missing")

    def require_distinct_personas(self) -> dict[str, str]:
        """Resolve both roles from access tokens before sending any command."""
        by_role: dict[str, list[str]] = {"admin": [], "member": []}
        for name, identity in self.config.personas.items():
            by_role[identity.role].append(name)
        if any(len(names) != 1 for names in by_role.values()):
            raise PersonaCollision("admin and member personas required")
        oids = {
            name: self.tokens.access_oid(name, CHAT) for names in by_role.values() for name in names
        }
        if oids[by_role["admin"][0]].casefold() == oids[by_role["member"][0]].casefold():
            raise PersonaCollision("persona identities match")
        return oids

    async def bot_chat(self, persona: str) -> tuple[str, list[dict[str, Any]]]:
        """Find one existing 1:1 chat by a prior message from the bot app ID."""
        data = await self._request(persona, CHAT, "GET", "/me/chats?$filter=chatType eq 'oneOnOne'")
        matches: list[tuple[str, list[dict[str, Any]]]] = []
        for chat in data.get("value", []):
            if chat.get("chatType") != "oneOnOne":
                continue
            chat_id = chat.get("id")
            if not isinstance(chat_id, str) or not chat_id or "/" in chat_id:
                continue
            messages = await self.chat_messages(persona, chat_id)
            if any(bot_sender_id(item) == self.config.bot_app_id for item in messages):
                matches.append((chat_id, messages))
        if len(matches) > 1:
            raise ValueError("bot chat ambiguous")
        if not matches:
            raise NoBotChat("no bot chat")
        return matches[0]

    async def chat_messages(
        self, persona: str, chat_id: str, deadline: float | None = None
    ) -> list[dict[str, Any]]:
        """Read the latest messages from a previously identified chat."""
        if not chat_id or "/" in chat_id:
            raise ValueError("invalid chat id")
        data = await self._request(
            persona, CHAT, "GET", f"/chats/{chat_id}/messages?$top=50", deadline=deadline
        )
        return list(data.get("value", []))

    async def post_chat_command(
        self,
        persona: str,
        chat_id: str,
        command: str,
        snapshot: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Send one plain-text command, reconciling uncertainty without resending."""
        if not chat_id or "/" in chat_id:
            raise ValueError("invalid chat id")
        seen_ids = frozenset(str(item["id"]) for item in snapshot if item.get("id"))
        latest_snapshot_time = max(
            (
                created
                for item in snapshot
                if (created := graph_time(item.get("createdDateTime"))) is not None
            ),
            default=None,
        )
        started = datetime.now(UTC)
        path = f"/chats/{chat_id}/messages"
        payload = {"body": {"contentType": "text", "content": command}}
        try:
            return await self._request(persona, CHAT, "POST", path, payload=payload)
        except AmbiguousPostError:
            deadline = self.now() + 30
            await self.sleep(3)
            expected_oid = self.config.personas[persona].expected_oid
            if expected_oid is None:
                expected_oid = self.tokens.status(persona).get("oid")
            while self.now() < deadline:
                try:
                    messages = await self.chat_messages(persona, chat_id, deadline)
                except GraphRequestError:
                    raise PostOutcomeUnknown("Graph POST outcome unknown") from None
                for item in messages:
                    created = graph_time(item.get("createdDateTime"))
                    body = item.get("body", {}).get("content", "")
                    plain = unescape(re.sub(r"<[^>]*>", "", body)).strip()
                    if (
                        expected_oid
                        and user_sender_id(item) == expected_oid
                        and item.get("id") not in seen_ids
                        and created is not None
                        and created >= started - timedelta(seconds=10)
                        and (latest_snapshot_time is None or created >= latest_snapshot_time)
                        and plain == command
                    ):
                        return item
                await self.sleep(min(3, max(0, deadline - self.now())))
            raise PostOutcomeUnknown("Graph POST outcome unknown") from None

    async def find_by_correlation(
        self,
        persona: str,
        correlation_id: str,
        since: datetime | None = None,
        *,
        command_text: str | None = None,
        seen_ids: frozenset[str] = frozenset(),
        deadline: float | None = None,
    ) -> dict[str, Any] | None:
        """Search bounded recent messages for a marked or exact persona post."""
        path = self.channel + "/messages?$top=50"
        for _ in range(4):
            data = await self._request(persona, READ, "GET", path, deadline=deadline)
            for item in data.get("value", []):
                if str(item.get("id", "")) in seen_ids:
                    continue
                created_at = graph_time(item.get("createdDateTime"))
                if created_at is None:
                    continue
                if since and created_at < since - timedelta(seconds=10):
                    continue
                expected_oid = self.config.personas[persona].expected_oid
                if command_text is not None and expected_oid is None:
                    expected_oid = self.tokens.status(persona).get("oid")
                sender_oid = item.get("from", {}).get("user", {}).get("id")
                if expected_oid and sender_oid != expected_oid:
                    continue
                if command_text is not None and not expected_oid:
                    continue
                content = item.get("body", {}).get("content", "")
                if command_text is not None:
                    without_mention = re.sub(r"<at\b[^>]*>.*?</at>", "", content, flags=re.I | re.S)
                    plain = unescape(re.sub(r"<[^>]*>", "", without_mention)).strip()
                    if plain == command_text:
                        return item
                elif f"[qa:{correlation_id}]" in content:
                    return item
            next_link = data.get("@odata.nextLink")
            if not isinstance(next_link, str) or not next_link.startswith(BASE + self.channel):
                break
            path = next_link.removeprefix(BASE)
        return None

    async def channel_message_ids(self, persona: str) -> frozenset[str]:
        """Record the same bounded history pages used by reconciliation."""
        path = self.channel + "/messages?$top=50"
        seen: set[str] = set()
        for _ in range(4):
            data = await self._request(persona, READ, "GET", path)
            seen.update(str(item["id"]) for item in data.get("value", []) if item.get("id"))
            next_link = data.get("@odata.nextLink")
            if not isinstance(next_link, str) or not next_link.startswith(BASE + self.channel):
                break
            path = next_link.removeprefix(BASE)
        return frozenset(seen)

    async def _paged_messages(
        self, persona: str, path: str, prefix: str, deadline: float | None
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for _ in range(4):
            data = await self._request(persona, READ, "GET", path, deadline=deadline)
            messages.extend(data.get("value", []))
            next_link = data.get("@odata.nextLink")
            if not isinstance(next_link, str) or not next_link.startswith(BASE + prefix):
                break
            path = next_link.removeprefix(BASE)
        return messages

    async def post_mention(
        self,
        persona: str,
        text: str,
        correlation_id: str,
        *,
        command: bool = False,
        seen_ids: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        """Post exactly once, then reconcile an uncertain outcome for 30 seconds."""
        if seen_ids is None:
            seen_ids = await self.channel_message_ids(persona)
        bot = self.config.bot_display_name
        content = f'<at id="0">{escape(bot)}</at> {escape(text)}'
        if not command:
            content += f" [qa:{escape(correlation_id)}]"
        payload = {
            "body": {"contentType": "html", "content": content},
            "mentions": [
                {
                    "id": 0,
                    "mentionText": bot,
                    "mentioned": {
                        "application": {
                            "@odata.type": "#microsoft.graph.teamworkApplicationIdentity",
                            "id": self.config.bot_app_id,
                            "displayName": bot,
                            "applicationIdentityType": "bot",
                        }
                    },
                }
            ],
        }
        attempt_started = datetime.now().astimezone()
        try:
            return await self._request(
                persona, SEND, "POST", self.channel + "/messages", payload=payload
            )
        except AmbiguousPostError:
            deadline = self.now() + 30
            await self.sleep(3)
            while self.now() < deadline:
                try:
                    found = await self.find_by_correlation(
                        persona,
                        correlation_id,
                        attempt_started,
                        command_text=text if command else None,
                        seen_ids=seen_ids,
                        deadline=deadline,
                    )
                except GraphRequestError:
                    raise PostOutcomeUnknown("Graph POST outcome unknown") from None
                if found:
                    return found
                await self.sleep(min(3, max(0, deadline - self.now())))
            raise PostOutcomeUnknown("Graph POST outcome unknown") from None

    async def read_replies(
        self, persona: str, message_id: str, deadline: float | None = None
    ) -> list[dict[str, Any]]:
        """Read replies to one channel message."""
        if not message_id or "/" in message_id:
            raise ValueError("invalid message id")
        prefix = self.channel + f"/messages/{message_id}/replies"
        return await self._paged_messages(persona, prefix, prefix, deadline)

    async def wait_for_reply(
        self,
        persona: str,
        message_id: str,
        predicate: Callable[[dict[str, Any]], bool],
        deadline: float,
    ) -> dict[str, Any] | None:
        """Poll replies with backoff until a match or the hard deadline."""
        delay = 1.0
        while self.now() < deadline:
            for reply in await self.read_replies(persona, message_id, deadline):
                if predicate(reply):
                    return reply
            await self.sleep(min(delay, max(0, deadline - self.now())))
            delay = min(delay * 2, 8)
        return None

    async def chat_messages_with_bot(
        self, persona: str, since: datetime, deadline: float | None = None
    ) -> list[dict[str, Any]]:
        """Find the sole bot chat and return its messages in the time window."""
        data = await self._request(
            persona, CHAT, "GET", "/me/chats?$expand=members", deadline=deadline
        )
        candidates = [chat for chat in data.get("value", []) if chat.get("chatType") == "oneOnOne"]
        matches: list[list[dict[str, Any]]] = []
        for chat in candidates:
            chat_id = chat.get("id")
            if not isinstance(chat_id, str) or "/" in chat_id:
                continue
            response = await self._request(
                persona, CHAT, "GET", f"/chats/{chat_id}/messages", deadline=deadline
            )
            messages = list(response.get("value", []))
            sender_match = any(
                bot_sender_id(message) == self.config.bot_app_id for message in messages
            )
            if sender_match:
                matches.append(messages)
        if len(matches) > 1:
            raise ValueError("bot chat ambiguous")
        if not matches:
            return []
        start = since - timedelta(seconds=10)
        return [
            message
            for message in matches[0]
            if (created := graph_time(message.get("createdDateTime"))) is not None
            and created >= start
        ]
