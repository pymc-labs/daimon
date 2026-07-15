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
