"""One rule on every surface: Privacy is always linked, Terms only when it is set.

The web route has its own test beside the route; here the three chat panels
read the same `legal_links` helper, so they show and hide Terms together.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import MagicMock

import discord
import pytest
from daimon.adapters.discord.privacy_panel.panel import PrivacyPanelView
from daimon.adapters.slack.privacy_panel.views import build_privacy_main_container
from daimon.adapters.teams.privacy_card import panel_card
from daimon.core.config import Settings
from daimon.core.privacy import legal_links

PRIVACY = "https://example.com/privacy"
TERMS = "https://example.com/terms"


def _settings(terms: str | None) -> Settings:
    return Settings(  # pyright: ignore[reportCallIssue]
        _env_file=None,  # pyright: ignore[reportCallIssue]
        database={"url": "postgresql+asyncpg://u:p@h/db"},  # pyright: ignore[reportArgumentType]
        privacy_policy_url=PRIVACY,  # pyright: ignore[reportArgumentType]
        terms_url=terms,  # pyright: ignore[reportArgumentType]
    )


def _discord(settings: Settings) -> list[str]:
    runtime = MagicMock()
    runtime.settings = settings

    async def build() -> list[str]:
        view = PrivacyPanelView(
            runtime=runtime, account_id=uuid.uuid4(), allowed_user_id=1, user_name="a"
        )
        return [
            c.url
            for c in view.walk_children()
            if isinstance(c, discord.ui.Button) and c.url is not None
        ]

    return asyncio.run(build())


def _slack(settings: Settings) -> list[str]:
    links = legal_links(settings)
    view = build_privacy_main_container(
        is_slack_connected=False,
        slack_connect_url=None,
        policy_url=links.privacy,
        terms_url=links.terms,
    )
    return [
        el["url"]
        for block in view["blocks"]
        if block["type"] == "actions"
        for el in block["elements"]
        if "url" in el
    ]


def _teams(settings: Settings) -> list[str]:
    links = legal_links(settings)
    card = panel_card(bot="daimon", policy_url=links.privacy, terms_url=links.terms)
    buttons = card.model_dump(by_alias=True)["body"][-1]["actions"]
    return [a["url"] for a in buttons if a["type"] == "Action.OpenUrl"]


@pytest.mark.parametrize("surface", [_discord, _slack, _teams], ids=["discord", "slack", "teams"])
@pytest.mark.parametrize(("terms", "expected"), [(None, [PRIVACY]), (TERMS, [PRIVACY, TERMS])])
def test_every_panel_links_privacy_and_terms_only_when_set(
    surface: Any, terms: str | None, expected: list[str]
) -> None:
    """The same settings give the same legal links on each platform's privacy panel."""
    assert surface(_settings(terms)) == expected, (
        f"{surface.__name__} must link {expected} for terms={terms!r}"
    )
