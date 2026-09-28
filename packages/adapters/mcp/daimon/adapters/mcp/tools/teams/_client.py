"""Bot Framework REST client for Teams: token, send, cards, thread, membership.

Plain httpx, so the MCP process needs no Teams SDK. The token comes from the
client-credentials flow against the deployment's one Entra tenant and is cached
until shortly before it expires. Every request carries its own timeout.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping

import httpx
from daimon.core.config import TeamsSettings
from daimon.core.posted_controls.teams_card import ADAPTIVE_CARD_TYPE
from pydantic import BaseModel, Field

# Under the Teams SDK's default service URL for proactive sends.
_CONVERSATIONS_URL = "https://smba.trafficmanager.net/teams/v3/conversations"
_SCOPE = "https://api.botframework.com/.default"
_TIMEOUT_S = 15.0
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


class _Member(BaseModel):
    aad_object_id: str | None = Field(default=None, alias="aadObjectId")


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
        self._token: tuple[str, float] | None = None
        self._lock = asyncio.Lock()

    async def _bearer(self) -> str:
        async with self._lock:
            if self._token is None or self._clock() >= self._token[1]:
                response = await self._http.post(
                    f"https://login.microsoftonline.com/{self._tenant_id}/oauth2/v2.0/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "scope": _SCOPE,
                    },
                    timeout=_TIMEOUT_S,
                )
                response.raise_for_status()
                token = _Token.model_validate_json(response.content)
                expires_at = self._clock() + token.expires_in - _REFRESH_MARGIN_S
                self._token = (token.access_token, expires_at)
            return self._token[0]

    async def _request(
        self, method: str, path: str, body: dict[str, object] | None = None
    ) -> httpx.Response:
        response = await self._http.request(
            method,
            f"{_CONVERSATIONS_URL}{path}",
            json=body,
            headers={"Authorization": f"Bearer {await self._bearer()}"},
            timeout=_TIMEOUT_S,
        )
        response.raise_for_status()
        return response

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

    async def is_member(self, conversation_id: str, aad_object_id: str) -> bool:
        """Whether this Entra user is on the conversation's roster; 403/404 mean no."""
        try:
            response = await self._request("GET", f"/{conversation_id}/members/{aad_object_id}")
        except httpx.HTTPStatusError as err:
            if err.response.status_code in (403, 404):
                return False
            raise
        member = _Member.model_validate_json(response.content)
        return (member.aad_object_id or "").lower() == aad_object_id.lower()


def build_teams_client(settings: TeamsSettings) -> TeamsBotClient:
    """The process's client, over its own bounded httpx client."""
    return TeamsBotClient(
        httpx.AsyncClient(timeout=_TIMEOUT_S),
        client_id=settings.client_id,
        client_secret=settings.client_secret.get_secret_value(),
        tenant_id=settings.tenant_id,
    )
