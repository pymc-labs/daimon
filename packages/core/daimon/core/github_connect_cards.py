"""Shared copy and Block Kit rendering for GitHub connection cards."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from daimon.core.agent_identity import (
    agent_display_name,
    identity_enabled_for,
    resolve_agent_identity,
)
from daimon.core.config import Settings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

CONNECT_GITHUB_EMOJI = "🔗"
CONNECT_CARD_COLOUR = "#0C1F40"
VARIANT: Literal["A", "B", "B_NO_FOOTER"] = "A"

_MACHINE_NAME = re.compile(r"^[a-z]+-[a-z0-9-]*[0-9a-f]{6,}$")
_HEX_NAME = re.compile(r"^[0-9a-f]{16,}$", re.IGNORECASE)


@dataclass(frozen=True)
class ConnectCard:
    author_name: str
    author_icon_url: str
    description: str
    github_mark_url: str
    variant: Literal["A", "B", "B_NO_FOOTER"]
    title: str = "Connect GitHub"

    @property
    def detail(self) -> str | None:
        return None

    @property
    def footer(self) -> str | None:
        return "Only you can see the link." if self.variant == "B" else None


def safe_agent_name(name: str | None) -> str | None:
    """Keep generated names and bare identifiers out of person-facing cards."""
    shown = (name or "").strip()
    if not shown or _MACHINE_NAME.fullmatch(shown) or _HEX_NAME.fullmatch(shown):
        return None
    try:
        uuid.UUID(shown)
    except ValueError:
        return shown[:80].rstrip()
    return None


def build_connect_card(
    *,
    agent_name: str | None,
    identity_enabled: bool,
    avatar_url: str | None,
    public_base_url: str,
    variant: Literal["A", "B", "B_NO_FOOTER"] | None = None,
    requested_repo: str | None = None,
) -> ConnectCard:
    chosen = VARIANT if variant is None else variant
    if chosen not in ("A", "B", "B_NO_FOOTER"):
        raise ValueError("Unknown GitHub connect card variant")
    root = public_base_url.rstrip("/")
    shown = safe_agent_name(agent_name)
    author = shown or "This agent" if identity_enabled and agent_name else "Daimon"
    return ConnectCard(
        author_name=author,
        author_icon_url=(avatar_url if identity_enabled and avatar_url else None)
        or f"{root}/web/daimon-face.png",
        description="Nothing is connected yet.\n\n"
        + (
            f"Tap the button. On the page, click Add repos, make sure {requested_repo} is ticked, "
            "click Add 1 repo, then Save changes."
            if requested_repo
            else f"Tap the button and tick the repos for {shown or 'this agent'}. "
            "They stay until you remove them."
            if agent_name
            else "Tap the button and tick the repos to connect."
        ),
        github_mark_url=f"{root}/web/github-mark.png",
        variant=chosen,
    )


async def resolve_connect_card(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    tenant_id: uuid.UUID,
    platform: Literal["discord", "slack", "teams"],
    workspace_id: str,
    agent_name: str | None,
    agent_id: str | None = None,
    requested_repo: str | None = None,
) -> ConnectCard:
    """Resolve one face after the interaction is acknowledged.

    `agent_id` (the agent's Managed Agents id) lets the card name the agent by its
    display name; without it the card shows `agent_name`.
    """
    root = settings.mcp.app_root_url
    if root is None:
        raise ValueError("GitHub connection page is unavailable")
    enabled = bool(agent_name) and identity_enabled_for(settings, platform, workspace_id)
    avatar_url = None
    shown = agent_name
    if agent_name:
        shown = await agent_display_name(
            sessionmaker, tenant_id=tenant_id, agent_id=agent_id, fallback=agent_name
        )
    if enabled and agent_name:
        async with sessionmaker() as session:
            identity = await resolve_agent_identity(
                session,
                tenant_id=tenant_id,
                agent_name=agent_name,
                is_builtin=False,
                public_base_url=str(root),
                enabled=True,
                background_sessionmaker=sessionmaker,
                wait_for_face=True,
            )
            avatar_url = identity.avatar_url
    return build_connect_card(
        agent_name=shown,
        identity_enabled=enabled,
        avatar_url=avatar_url,
        public_base_url=str(root),
        requested_repo=requested_repo,
    )


def connect_button_blocks(
    url: str,
    *,
    card: ConnectCard,
) -> list[dict[str, Any]]:
    """One branded Slack card, with the link only in the button action."""
    blocks: list[dict[str, Any]] = [
        {
            "type": "context",
            "elements": [
                {"type": "image", "image_url": card.author_icon_url, "alt_text": card.author_name},
                {"type": "plain_text", "text": card.author_name, "emoji": True},
            ],
        },
        {"type": "header", "text": {"type": "plain_text", "text": card.title, "emoji": True}},
        {
            "type": "section",
            "text": {"type": "plain_text", "text": card.description, "emoji": True},
            "accessory": {
                "type": "image",
                "image_url": card.github_mark_url,
                "alt_text": "GitHub",
            },
        },
    ]
    if card.detail:
        blocks.append({"type": "section", "text": {"type": "plain_text", "text": card.detail}})
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "github_link__open",
                    "text": {
                        "type": "plain_text",
                        "text": f"{CONNECT_GITHUB_EMOJI} Connect GitHub",
                        "emoji": True,
                    },
                    "style": "primary",
                    "url": url,
                }
            ],
        }
    )
    if card.footer:
        blocks.append(
            {"type": "context", "elements": [{"type": "plain_text", "text": card.footer}]}
        )
    return blocks


def connect_attachment(url: str, *, card: ConnectCard) -> dict[str, Any]:
    """Slack's coloured shell around the shared Block Kit card."""
    return {
        "color": CONNECT_CARD_COLOUR,
        "fallback": card.title,
        "blocks": connect_button_blocks(url, card=card),
    }


def teams_connect_card(url: str, *, card: ConnectCard) -> dict[str, object]:
    """The shared card as a Teams Adaptive Card, with the link only in the button."""
    body: list[dict[str, object]] = [
        {
            "type": "ColumnSet",
            "columns": [
                {
                    "type": "Column",
                    "width": "auto",
                    "items": [
                        {
                            "type": "Image",
                            "url": card.author_icon_url,
                            "size": "Small",
                            "style": "Person",
                            "altText": card.author_name,
                        }
                    ],
                },
                {
                    "type": "Column",
                    "width": "stretch",
                    "verticalContentAlignment": "Center",
                    "items": [{"type": "TextBlock", "text": card.author_name, "wrap": True}],
                },
            ],
        },
        {"type": "TextBlock", "text": card.title, "size": "Large", "weight": "Bolder"},
        {"type": "TextBlock", "text": card.description, "wrap": True},
    ]
    if card.footer:
        body.append({"type": "TextBlock", "text": card.footer, "isSubtle": True, "wrap": True})
    return {
        "type": "AdaptiveCard",
        "version": "1.5",
        "fallbackText": card.title,
        "body": body,
        "actions": [
            {
                "type": "Action.OpenUrl",
                "title": f"{CONNECT_GITHUB_EMOJI} Connect GitHub",
                "url": url,
            }
        ],
    }


def discord_embed_payload(card: ConnectCard) -> dict[str, Any]:
    """The shared Discord embed data, without an adapter dependency in core."""
    description = card.description
    if card.detail:
        description += f"\n{card.detail}"
    payload: dict[str, Any] = {
        "title": card.title,
        "description": description,
        "color": int(CONNECT_CARD_COLOUR.removeprefix("#"), 16),
        "author": {"name": card.author_name, "icon_url": card.author_icon_url},
        "thumbnail": {"url": card.github_mark_url},
    }
    if card.footer:
        payload["footer"] = {"text": card.footer}
    return payload


# Words for an agent's own repos, shared by Discord, Slack, Teams and the web
# picker so every surface says the same thing.
DETAILS_LABEL = "Details"
ALREADY_ADDED = "Already added"
NEEDS_WRITE = "Needs write"
"""A repo the agent has, or must have, read only but needs to change."""
NEEDED = "Needed"
"""A repo the agent must have before it can drop its old GitHub token."""
CLOSE_TAB = "You can close this tab."


def audience_line(agent_name: str, *, write: bool) -> str:
    """Who can use the repos. `write` only when every repo allows changes."""
    verb = "read and change"
    return f"Anyone who talks to {agent_name} can ask it to {verb} them."


def ask_manager_line(agent_name: str) -> str:
    return f"Ask whoever manages {agent_name} to add repos."


def repo_count(count: int) -> str:
    return f"{count} {'repo' if count == 1 else 'repos'}"


def repos_come_too_line(agent_name: str) -> str:
    return f"{agent_name}'s repos come too."


@dataclass(frozen=True)
class AgentRepoLine:
    """One repo as a person sees it on the agent's GitHub section."""

    full_name: str
    access: Literal["read", "write"]
    added_by: str | None = None
    added_on: str | None = None
