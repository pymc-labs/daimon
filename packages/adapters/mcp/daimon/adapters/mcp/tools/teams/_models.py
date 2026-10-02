"""Pydantic rows returned by the Teams channel read tools."""

from __future__ import annotations

from typing import Literal

from daimon.adapters.mcp.tools._untrusted import UntrustedResult
from pydantic import BaseModel, ConfigDict


class TeamsChannelRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    type: str
    """standard, private or shared."""
    team_id: str
    team_name: str | None = None


class TeamsReadMessage(BaseModel):
    """One message read back from a channel."""

    model_config = ConfigDict(frozen=True)

    id: str
    thread_id: str
    """The thread's conversation id: send_message and read_thread take it."""
    author_id: str | None = None
    """An Entra user id, or an app id for a bot."""
    author_name: str | None = None
    is_bot: bool = False
    text: str
    timestamp: str | None = None
    subject: str | None = None
    files: list[str] = []
    """Names only: daimon cannot fetch a channel's files for you here."""
    images: int = 0
    web_url: str | None = None


class TeamsPost(TeamsReadMessage):
    replies: list[TeamsReadMessage] = []
    """Oldest first."""
    more_replies: bool = False
    """True when the post has replies not shown; read_thread pages them."""


class TeamsChannelResult(UntrustedResult):
    channel_id: str
    channel_name: str
    team_name: str | None = None
    posts: list[TeamsPost]
    """Oldest first: the page's least recently active post opens it."""
    next_cursor: str | None = None
    hint: str | None = None


class TeamsThreadResult(UntrustedResult):
    thread_id: str
    messages: list[TeamsReadMessage]
    """The root, then replies oldest first."""
    next_cursor: str | None = None
    hint: str | None = None


class TeamsThreadRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    thread_id: str
    subject: str | None = None
    preview: str
    author_name: str | None = None
    created_at: str | None = None
    reply_count: int
    more_replies: bool = False


class TeamsParsedLink(BaseModel):
    link_type: Literal["channel", "message"]
    channel_id: str
    message_id: str | None = None
    thread_id: str | None = None
    hint: str


class TeamsSearchMatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    channel_id: str
    channel_name: str
    message: TeamsReadMessage


class TeamsSearchResult(UntrustedResult):
    matches: list[TeamsSearchMatch]
    scanned_channels: int
    scanned_posts: int
    complete: bool
    """False when the scan stopped at its bound before reading everything."""
    hint: str | None = None
