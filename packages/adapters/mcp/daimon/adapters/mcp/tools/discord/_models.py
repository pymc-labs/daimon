"""Pydantic row models for Discord MCP tool results."""

from __future__ import annotations

from typing import Literal

import discord
from pydantic import BaseModel


class AttachmentRow(BaseModel):
    id: str
    filename: str
    url: str
    size: int


class MessageRow(BaseModel):
    id: str
    channel_id: str
    author_id: str
    content: str
    timestamp: str  # ISO-8601
    attachments: list[AttachmentRow] = []
    author_username: str = ""
    role: Literal["user", "assistant"] = "user"


class ChannelRow(BaseModel):
    id: str
    name: str
    type: str  # e.g. "text", "forum", "voice", "category"
    category_id: str | None = None


class ThreadRow(BaseModel):
    id: str
    name: str
    parent_id: str
    archived: bool
    message_count: int
    last_activity: str  # ISO-8601


class ParsedLink(BaseModel):
    guild_id: str
    channel_id: str
    message_id: str | None = None
    link_type: Literal["channel", "message_or_thread"]
    hint: str


class ReadThreadResult(BaseModel):
    rows: list[MessageRow]
    next_before: str | None = None
    hint: str | None = None


class SearchResult(BaseModel):
    total_results: int
    showing: int
    offset: int
    rows: list[MessageRow]
    hint: str | None = None


def _to_message_row(m: discord.Message) -> MessageRow:  # pyright: ignore[reportUnusedFunction]  # imported by _read/_send
    return MessageRow(
        id=str(m.id),
        channel_id=str(m.channel.id),
        author_id=str(m.author.id),
        content=m.content,
        timestamp=m.created_at.isoformat(),
        attachments=[
            AttachmentRow(id=str(a.id), filename=a.filename, url=a.url, size=a.size)
            for a in m.attachments
        ],
        author_username=m.author.name,
        role="assistant" if m.author.bot else "user",
    )
