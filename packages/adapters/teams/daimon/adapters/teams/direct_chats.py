"""The bot's 1:1 chats with team members, opened proactively over Bot Framework.

A person's roster id (`29:…`) comes from a conversation they are in; Teams
lets the bot open a 1:1 chat with anyone in a team it is installed in.
Routine fallbacks and commands sent in a channel go through here.
"""

from __future__ import annotations

from typing import Protocol

import httpx
from daimon.adapters.teams.lifecycle import TeamsSender
from daimon.core.teams_bot_framework import SERVICE_URL
from microsoft_teams.api import Account, MessageActivityInput
from microsoft_teams.api.clients.conversation import CreateConversationParams
from microsoft_teams.apps import App

__all__ = ["DirectChats", "SdkDirectChats"]


class DirectChats(Protocol):
    """The Bot Framework calls a proactive 1:1 message needs."""

    async def member(self, conversation_id: str, aad_object_id: str) -> str | None:
        """The person's roster id (`29:…`) in the conversation, or None when absent."""
        ...

    async def open_chat(self, member_id: str) -> str:
        """The bot's 1:1 chat with a roster member."""
        ...

    async def post(self, conversation_id: str, text: str) -> None: ...


class SdkDirectChats:
    """`DirectChats` over the SDK app, sending through the adapter's timed sender."""

    def __init__(self, app: App, sender: TeamsSender, *, entra_tenant_id: str) -> None:
        self._app = app
        self._sender = sender
        self._entra_tenant_id = entra_tenant_id

    async def member(self, conversation_id: str, aad_object_id: str) -> str | None:
        conversations = self._app.api.from_service_url(SERVICE_URL).conversations
        try:
            account = await conversations.get_member_by_id(conversation_id, aad_object_id)
        except httpx.HTTPStatusError as err:
            if err.response.status_code in (403, 404):
                return None
            raise
        return account.id or None

    async def open_chat(self, member_id: str) -> str:
        params = CreateConversationParams(
            members=[Account(id=member_id)],
            tenant_id=self._entra_tenant_id,
            channel_data={"tenant": {"id": self._entra_tenant_id}},
        )
        conversations = self._app.api.from_service_url(SERVICE_URL).conversations
        return (await conversations.create(params)).id

    async def post(self, conversation_id: str, text: str) -> None:
        activity = MessageActivityInput(text=text, text_format="markdown").add_ai_generated()
        await self._sender.send(conversation_id, activity, service_url=SERVICE_URL)
