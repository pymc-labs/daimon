"""Shared copy and Block Kit rendering for GitHub connection cards."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from daimon.core.agent_identity import identity_enabled_for, resolve_agent_identity
from daimon.core.config import Settings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

CONNECT_GITHUB_EMOJI = "🔗"
CONNECT_CARD_COLOUR = "#0C1F40"
VARIANT: Literal["A", "B"] = "A"

_MACHINE_NAME = re.compile(r"^[a-z]+-[a-z0-9-]*[0-9a-f]{6,}$")
_HEX_NAME = re.compile(r"^[0-9a-f]{16,}$", re.IGNORECASE)


@dataclass(frozen=True)
class ConnectCard:
    author_name: str
    author_icon_url: str
    description: str
    github_mark_url: str
    variant: Literal["A", "B"]
    title: str = "Connect GitHub"

    @property
    def detail(self) -> str | None:
        return "Default access: Read and write" if self.variant == "B" else None

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
        return shown
    return None


def build_connect_card(
    *,
    agent_name: str | None,
    identity_enabled: bool,
    avatar_url: str | None,
    public_base_url: str,
    variant: Literal["A", "B"] | None = None,
) -> ConnectCard:
    chosen = VARIANT if variant is None else variant
    if chosen not in ("A", "B"):
        raise ValueError("Unknown GitHub connect card variant")
    root = public_base_url.rstrip("/")
    shown = safe_agent_name(agent_name)
    author = shown or "This agent" if identity_enabled and agent_name else "Daimon"
    return ConnectCard(
        author_name=author,
        author_icon_url=(avatar_url if identity_enabled and avatar_url else None)
        or f"{root}/web/daimon-face.png",
        description=f"Pick repos {shown or 'this agent'} can use."
        if agent_name
        else "Pick repos your agents can use.",
        github_mark_url=f"{root}/web/github-mark.png",
        variant=chosen,
    )


async def resolve_connect_card(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    tenant_id: uuid.UUID,
    platform: Literal["discord", "slack"],
    workspace_id: str,
    agent_name: str | None,
) -> ConnectCard:
    """Resolve one face after the interaction is acknowledged."""
    root = settings.mcp.app_root_url
    if root is None:
        raise ValueError("GitHub connection page is unavailable")
    enabled = bool(agent_name) and identity_enabled_for(settings, platform, workspace_id)
    avatar_url = None
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
        agent_name=agent_name,
        identity_enabled=enabled,
        avatar_url=avatar_url,
        public_base_url=str(root),
    )


def connect_button_blocks(
    url: str,
    *,
    card: ConnectCard | None = None,
) -> list[dict[str, Any]]:
    """One branded Slack card, with the link only in the button action."""
    if card is None:
        parts = urlsplit(url)
        card = build_connect_card(
            agent_name=None,
            identity_enabled=False,
            avatar_url=None,
            public_base_url=f"{parts.scheme}://{parts.netloc}",
        )
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
