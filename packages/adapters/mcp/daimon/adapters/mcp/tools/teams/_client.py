"""Bot Framework REST client for Teams: token, send, cards, thread, membership, teams.

Plain httpx, so the MCP process needs no Teams SDK. Tokens come from the
client-credentials flow against the deployment's one Entra tenant, one per
scope (Bot Framework, and Graph for `graph_token`), each cached until shortly
before it expires. Every request carries its own timeout.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Mapping

import httpx
from daimon.core.config import TeamsSettings
from daimon.core.posted_controls.teams_card import ADAPTIVE_CARD_TYPE
from daimon.core.teams_bot_framework import SERVICE_URL, retry_throttled
from daimon.core.teams_file_offers import UploadOffer, sign_offer
from daimon.core.teams_graph import GRAPH_SCOPE
from pydantic import BaseModel, Field

_CONVERSATIONS_URL = f"{SERVICE_URL}/v3/conversations"
_TEAMS_URL = f"{SERVICE_URL}/v3/teams"
_SCOPE = "https://api.botframework.com/.default"
_TIMEOUT_S = 15.0
# Channel lists change rarely; a short cache keeps one read tool call to one listing.
_CHANNELS_TTL_S = 300.0
_PATH_ID = re.compile(r"[\w:@.;=+-]+")
_REFRESH_MARGIN_S = 300.0
# Teams' "AI generated" label, as the SDK's `add_ai_generated` renders it.
_AI_LABEL = {
    "type": "https://schema.org/Message",
    "@type": "Message",
    "@context": "https://schema.org",
    "@id": "",
    "additionalType": ["AIGeneratedContent"],
}


class _Token(BaseModel):
    access_token: str
    expires_in: float


class _Sent(BaseModel):
    id: str


class _Conversation(BaseModel):
    id: str
    activity_id: str = Field(default="", alias="activityId")


class TeamsMember(BaseModel):
    """A roster entry: `id` is the Bot Framework id (`29:…`) a 1:1 chat is opened with."""

    id: str = ""
    name: str | None = None
    aad_object_id: str | None = Field(default=None, alias="aadObjectId")
    user_role: str | None = Field(default=None, alias="userRole")


class TeamDetails(BaseModel):
    id: str
    name: str | None = None
    aad_group_id: str | None = Field(default=None, alias="aadGroupId")


class TeamChannel(BaseModel):
    """One channel of a team. General has no name; `type` is standard, private or shared."""

    id: str
    name: str | None = None
    type: str | None = None


class _Channels(BaseModel):
    conversations: list[TeamChannel] = Field(default_factory=list[TeamChannel])


def _id(value: str) -> str:
    """A Teams id for a URL path; anything that could leave its segment is refused."""
    if not _PATH_ID.fullmatch(value) or ".." in value:
        raise ValueError("not a Teams id")
    return value


def _message(text: str) -> dict[str, object]:
    return {"type": "message", "text": text, "textFormat": "markdown", "entities": [_AI_LABEL]}


def _card_message(card: Mapping[str, object]) -> dict[str, object]:
    return {
        "type": "message",
        "attachments": [{"contentType": ADAPTIVE_CARD_TYPE, "content": card}],
    }


class TeamsBotClient:
    """Sends as the bot. Raises `httpx.HTTPError` or `ValueError` (bad JSON) on failure."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        client_id: str,
        client_secret: str,
        tenant_id: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret
        self._tenant_id = tenant_id
        self._clock = clock
        self._tokens: dict[str, tuple[str, float]] = {}
        self._channels: dict[str, tuple[list[TeamChannel], float]] = {}
        self._lock = asyncio.Lock()

    @property
    def tenant_id(self) -> str:
        """The deployment's Entra tenant."""
        return self._tenant_id

    async def _bearer(self, scope: str = _SCOPE) -> str:
        async with self._lock:
            cached = self._tokens.get(scope)
            if cached is None or self._clock() >= cached[1]:
                response = await self._http.post(
                    f"https://login.microsoftonline.com/{self._tenant_id}/oauth2/v2.0/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "scope": scope,
                    },
                    timeout=_TIMEOUT_S,
                )
                response.raise_for_status()
                token = _Token.model_validate_json(response.content)
                cached = (token.access_token, self._clock() + token.expires_in - _REFRESH_MARGIN_S)
                self._tokens[scope] = cached
            return cached[0]

    async def graph_token(self) -> str:
        """An app-only Microsoft Graph token, for `daimon.core.teams_graph.GraphClient`."""
        return await self._bearer(GRAPH_SCOPE)

    @property
    def http(self) -> httpx.AsyncClient:
        return self._http

    async def _request(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
        *,
        base: str = _CONVERSATIONS_URL,
    ) -> httpx.Response:
        async def attempt() -> httpx.Response:
            response = await self._http.request(
                method,
                f"{base}{path}",
                json=body,
                headers={"Authorization": f"Bearer {await self._bearer()}"},
                timeout=_TIMEOUT_S,
            )
            return response.raise_for_status()

        return await retry_throttled(attempt)

    async def send(self, conversation_id: str, text: str) -> str:
        """Post a markdown message; returns its activity id."""
        return await self._send(conversation_id, _message(text))

    async def send_card(self, conversation_id: str, card: Mapping[str, object]) -> str:
        """Post one Adaptive Card; returns its activity id."""
        return await self._send(conversation_id, _card_message(card))

    async def _send(self, conversation_id: str, body: dict[str, object]) -> str:
        response = await self._request("POST", f"/{conversation_id}/activities", body)
        return _Sent.model_validate_json(response.content).id

    async def update_card(
        self, conversation_id: str, activity_id: str, card: Mapping[str, object]
    ) -> None:
        """Replace a card the bot posted."""
        body = {**_card_message(card), "id": activity_id}
        await self._request("PUT", f"/{conversation_id}/activities/{activity_id}", body)

    async def create_thread(self, channel_id: str, text: str) -> tuple[str, str]:
        """Start a new post in a channel; returns (thread conversation id, activity id)."""
        body: dict[str, object] = {
            "isGroup": True,
            "channelData": {"channel": {"id": channel_id}},
            "activity": _message(text),
            "tenantId": self._tenant_id,
        }
        created = _Conversation.model_validate_json((await self._request("POST", "", body)).content)
        return created.id, created.activity_id

    async def get_member(self, conversation_id: str, aad_object_id: str) -> TeamsMember | None:
        """This Entra user's roster entry in the conversation, or None (403/404) if absent."""
        try:
            response = await self._request(
                "GET", f"/{_id(conversation_id)}/members/{_id(aad_object_id)}"
            )
        except httpx.HTTPStatusError as err:
            if err.response.status_code in (403, 404):
                return None
            raise
        member = TeamsMember.model_validate_json(response.content)
        if (member.aad_object_id or "").lower() != aad_object_id.lower():
            return None
        return member

    async def is_member(self, conversation_id: str, aad_object_id: str) -> bool:
        """Whether this Entra user is on the conversation's roster; 403/404 mean no."""
        return await self.get_member(conversation_id, aad_object_id) is not None

    async def get_team(self, team_id: str) -> TeamDetails | None:
        """A team by its Bot Framework id (its General channel's id); None if not found."""
        try:
            response = await self._request("GET", f"/{_id(team_id)}", base=_TEAMS_URL)
        except httpx.HTTPStatusError as err:
            if err.response.status_code in (400, 403, 404):
                return None
            raise
        return TeamDetails.model_validate_json(response.content)

    async def list_team_channels(self, team_id: str) -> list[TeamChannel]:
        """The team's channels the bot can see, General included (unnamed)."""
        response = await self._request("GET", f"/{_id(team_id)}/conversations", base=_TEAMS_URL)
        return _Channels.model_validate_json(response.content).conversations

    async def team_channels(self, team_id: str, *, fresh: bool = False) -> list[TeamChannel]:
        """`list_team_channels`, cached for a few minutes; `fresh` skips the cache."""
        cached = self._channels.get(team_id)
        if fresh or cached is None or self._clock() >= cached[1]:
            cached = (await self.list_team_channels(team_id), self._clock() + _CHANNELS_TTL_S)
            self._channels[team_id] = cached
        return cached[0]

    async def open_personal_chat(self, member_id: str) -> str:
        """The 1:1 chat with a roster member (`29:…`); Teams refuses if they lack the app."""
        body: dict[str, object] = {
            "isGroup": False,
            "members": [{"id": member_id}],
            "tenantId": self._tenant_id,
            "channelData": {"tenant": {"id": self._tenant_id}},
        }
        return _Conversation.model_validate_json((await self._request("POST", "", body)).content).id

    async def send_activity(self, conversation_id: str, activity: dict[str, object]) -> str:
        """Post a prepared activity; returns its id."""
        return await self._send(conversation_id, activity)

    def file_offer_token(self, offer: UploadOffer) -> str:
        """A consent-card token the adapter verifies with the same client secret."""
        return sign_offer(offer, secret=self._client_secret, now=time.time())


def build_teams_client(settings: TeamsSettings) -> TeamsBotClient:
    """The process's client, over its own bounded httpx client."""
    return TeamsBotClient(
        httpx.AsyncClient(timeout=_TIMEOUT_S),
        client_id=settings.client_id,
        client_secret=settings.client_secret.get_secret_value(),
        tenant_id=settings.tenant_id,
    )
