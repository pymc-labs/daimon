"""Pydantic rows returned by the Slack channel tools."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class SlackChannelRow(BaseModel):
    id: str
    name: str
    is_private: bool
    topic: str | None = None
    num_members: int | None = None


class SlackMessageRow(BaseModel):
    ts: str
    user_id: str | None = None
    username: str | None = None
    text: str
    thread_ts: str | None = None
    reply_count: int | None = None


class SlackChannelResult(BaseModel):
    messages: list[SlackMessageRow]
    next_cursor: str | None = None
    hint: str | None = None


class SlackThreadResult(BaseModel):
    channel_id: str
    thread_ts: str
    messages: list[SlackMessageRow]
    has_more: bool


class SlackSearchMatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    channel_id: str
    channel_name: str | None = None
    ts: str
    username: str | None = None
    text: str
    permalink: str | None = None


class SlackSearchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    matches: list[SlackSearchMatch]
    total: int


class SlackParsedLink(BaseModel):
    channel_id: str
    message_ts: str  # dotted, e.g. "1717171717.123456"
    thread_ts: str | None = None  # parent ts, when the link is a reply
    hint: str
