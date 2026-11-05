"""The "Open in web" button on Teams cards: shown with a url, absent without one."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

from daimon.adapters.teams.card_actions import web_links
from daimon.adapters.teams.channel_settings_card import ChannelSettings, channel_settings_form
from daimon.adapters.teams.here import here_card
from daimon.adapters.teams.routines_card import panel_card as routines_card
from daimon.adapters.teams.runtime import TeamsRuntime
from daimon.adapters.teams.setup_card import details_card
from daimon.core.access_policy import TenantAccessPolicy
from daimon.core.agent_details import AgentDetails
from daimon.core.config import WebSettings
from daimon.core.here_card import assemble_here_card

URL = "https://app.example.com/w/t/agents/a"
LABEL = "Open in web"
TENANT = uuid.UUID(int=7)


def _dump(card: Any) -> str:
    return json.dumps(card.model_dump(by_alias=True, exclude_none=True))


def _opens(card: Any, url: str) -> bool:
    return f'"title": "{LABEL}"' in _dump(card) and f'"url": "{url}"' in _dump(card)


def _details(web_url: str | None) -> AgentDetails:
    return AgentDetails(
        ma_agent_id="ag_helper",
        name="Helper",
        model_id="model",
        model_display_name="Model",
        daimon_managed=False,
        created_by_is_workspace=True,
        created_at=datetime.now(UTC),
        answers_here=False,
        applies_note="Changes apply next turn.",
        web_url=web_url,
    )


def test_agent_details_card_has_the_button_only_with_a_url() -> None:
    kwargs: dict[str, Any] = {"here": "c", "page": 0, "coding_tools": False, "is_admin": True}
    assert _opens(details_card(_details(URL), **kwargs), URL)
    assert LABEL not in _dump(details_card(_details(None), **kwargs))


def test_routines_card_has_the_button_only_with_a_url() -> None:
    url = "https://app.example.com/w/t/routines"
    shown = routines_card([], 0, user_id="u", is_admin=False, web_url=url)
    assert _opens(shown, url)
    assert LABEL not in _dump(routines_card([], 0, user_id="u", is_admin=False))


def test_channel_settings_card_has_the_button_only_with_a_url() -> None:
    url = "https://app.example.com/w/t/channels/c"

    def form(web_url: str | None) -> Any:
        settings = ChannelSettings(
            channel_id="c", label="growth", picker=None, rule=None, web_url=web_url
        )
        return channel_settings_form(settings)

    assert _opens(form(url), url)
    assert LABEL not in _dump(form(None))


def test_here_card_has_the_button_only_with_a_url() -> None:
    url = "https://app.example.com/w/t/channels/c"

    def card(web_url: str | None) -> Any:
        shown = assemble_here_card(
            channel_id="c",
            platform="teams",
            agent_name="daimon",
            tier="channel",
            channel=None,
            tenant=None,
            configuration_target_name=None,
            policy=TenantAccessPolicy(),
            details=None,
        ).model_copy(update={"web_url": web_url})
        return here_card(shown)

    assert _opens(card(url), url)
    assert LABEL not in _dump(card(None))


def _runtime(web: WebSettings | None) -> TeamsRuntime:
    return cast(TeamsRuntime, SimpleNamespace(settings=SimpleNamespace(web=web)))


def test_web_links_follow_the_web_block_and_the_sender() -> None:
    on = _runtime(WebSettings(public_url="https://app.example.com/"))  # type: ignore[arg-type]
    links = web_links(on, TENANT, admin=False)
    assert links is not None
    assert (links.spend() or "").startswith("https://app.example.com/w/")
    assert web_links(on, TENANT, admin=False, foreign=True) is None, "no links for a foreign sender"
    assert web_links(_runtime(None), TENANT, admin=True) is None, "self-host: no web block"
