"""Shared copy and Block Kit rendering for GitHub connection cards."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from daimon.core.agent_identity import identity_enabled_for, resolve_agent_identity
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
        return "Default access: Read and write" if self.variant != "A" else None

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
            f"Tap the button and tick the repos {shown or 'this agent'} can use."
            if agent_name
            else "Tap the button and tick the repos your agents can use."
        ),
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
ADD_REPOS_LABEL = "Add repos"
DETAILS_LABEL = "Details"
ALREADY_ADDED = "Already added"
NEEDS_WRITE = "Needs write"
"""A repo the agent has, or must have, read only but needs to change."""
NEEDED = "Needed"
"""A repo the agent must have before it can drop its old GitHub token."""
CLOSE_TAB = "You can close this tab."


def access_label(level: Literal["read", "write"] | None) -> str:
    return "Read and write" if level == "write" else "Read only"


def agent_repos_title(agent_name: str) -> str:
    return f"{agent_name}'s repos"


def audience_line(agent_name: str, *, write: bool) -> str:
    """Who can use the repos. `write` only when every repo allows changes."""
    verb = "read and change" if write else "read"
    return f"Anyone who talks to {agent_name} can ask it to {verb} them."


def empty_line(agent_name: str) -> str:
    return f"{agent_name} has no repos yet."


def remove_label(agent_name: str) -> str:
    return f"Remove from {agent_name}"


def ask_manager_line(agent_name: str) -> str:
    return f"Ask whoever manages {agent_name} to add repos."


def picker_title(agent_name: str) -> str:
    return f"Add repos to {agent_name}"


def repo_count(count: int) -> str:
    return f"{count} {'repo' if count == 1 else 'repos'}"


def add_button_label(count: int) -> str:
    return f"Add {repo_count(count)}" if count else ADD_REPOS_LABEL


def added_line(count: int, agent_name: str) -> str:
    return f"Added {repo_count(count)} to {agent_name}."


def old_token_line(agent_name: str) -> str:
    """Only once the switch to the App has finished, never while it is pending."""
    return f"{agent_name} no longer uses its old GitHub token."


def repos_come_too_line(agent_name: str) -> str:
    return f"{agent_name}'s repos come too."


@dataclass(frozen=True)
class AgentRepoLine:
    """One repo as a person sees it on the agent's GitHub section."""

    full_name: str
    access: Literal["read", "write"]
    added_by: str | None = None
    added_on: str | None = None


def agent_section_lines(agent_name: str, repos: tuple[AgentRepoLine, ...]) -> tuple[str, ...]:
    """The list and the one line saying who can use it; empty agents get one line."""
    if not repos:
        return (empty_line(agent_name),)
    return (
        *(repo.full_name for repo in repos),
        audience_line(agent_name, write=all(repo.access == "write" for repo in repos)),
    )


def repo_detail_lines(repo: AgentRepoLine) -> tuple[str, ...]:
    """What [Details] shows for one repo: who added it, when, and its access."""
    lines: list[str] = []
    if repo.added_by and repo.added_on:
        lines.append(f"Added by {repo.added_by} on {repo.added_on}")
    elif repo.added_by:
        lines.append(f"Added by {repo.added_by}")
    elif repo.added_on:
        lines.append(f"Added on {repo.added_on}")
    lines.append(access_label(repo.access))
    return tuple(lines)


async def load_agent_repo_lines(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    platform: Literal["discord", "slack"],
) -> tuple[AgentRepoLine, ...]:
    """The repos the agent uses now, with who added each as a platform mention."""
    from daimon.core.stores.github_access import list_agent_repos
    from daimon.core.stores.identity import (
        get_discord_principal_for_account,
        get_slack_principal_for_account,
    )

    lookup = (
        get_discord_principal_for_account
        if platform == "discord"
        else get_slack_principal_for_account
    )
    lines: list[AgentRepoLine] = []
    for repo in await list_agent_repos(session, tenant_id=tenant_id, agent_id=agent_id):
        if repo.staged or repo.status != "active":
            continue
        shared = repo.scope == "shared"
        account_id = repo.granted_by_account_id if shared else repo.added_by_account_id
        user_id = await lookup(session, account_id=account_id) if account_id else None
        added_at = repo.granted_at if shared else repo.added_at
        lines.append(
            AgentRepoLine(
                full_name=repo.full_name,
                access=repo.ceiling_access,
                added_by=f"<@{user_id}>" if user_id else None,
                added_on=f"{added_at.day} {added_at:%b %Y}",
            )
        )
    return tuple(lines)
