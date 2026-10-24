"""Tool results name the caller's platform words, and neutral ones for no platform."""

from __future__ import annotations

import uuid
from typing import get_args

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools._ctx import (
    _refusal_nouns,  # pyright: ignore[reportPrivateUsage]
    _require_admin,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools._words import (
    admin_noun,
    setup_instruction,
    space_noun,
    top_up_noun,
)
from daimon.adapters.mcp.tools.skill_uploads import (
    _no_card_refusal,  # pyright: ignore[reportPrivateUsage]
    _panel,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.stores.domain import ChatPlatform, Role
from fastmcp.exceptions import ToolError

_FOREIGN: dict[str, tuple[str, ...]] = {
    "teams": ("/agent-setup", "/billing", "server admin", "workspace admin", "Manage Server"),
    "slack": ("server admin", "Manage Server"),
    "discord": ("workspace admin",),
}


def _auth(platform: str | None) -> AuthIdentity:
    return AuthIdentity(
        account_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role=Role.USER, platform=platform
    )


def test_nouns_come_from_the_table() -> None:
    assert admin_noun("discord") == "a server admin"
    assert admin_noun("slack") == "a workspace admin"
    assert admin_noun("teams") == "an admin"
    assert space_noun("teams") == "organisation"
    assert top_up_noun("slack") == "`/billing`"
    assert top_up_noun("teams") == "`billing` in a 1:1 chat with me"


@pytest.mark.parametrize("platform", [None, "cli", "web"])
def test_a_caller_with_no_chat_platform_gets_neutral_words(platform: str | None) -> None:
    assert admin_noun(platform) == "an admin"
    assert space_noun(platform) == "workspace"
    assert setup_instruction(platform) == "open the setup panel"
    assert "/" not in top_up_noun(platform)


def test_setup_instruction_per_platform() -> None:
    assert setup_instruction("discord") == "run `/agent-setup`"
    assert setup_instruction("slack") == "run `/agent-setup`"
    assert setup_instruction("teams") == "send `setup` in a 1:1 chat with Daimon"


@pytest.mark.parametrize("platform", get_args(ChatPlatform))
def test_results_show_no_foreign_words(platform: ChatPlatform) -> None:
    with pytest.raises(ToolError) as admin_refusal:
        _require_admin(_auth(platform))
    nouns = _refusal_nouns(_auth(platform))
    texts = [
        str(admin_refusal.value),
        _panel(platform, "helper"),
        _no_card_refusal(platform, "cards_off", "helper"),
        nouns.admin,
        nouns.billing,
        nouns.setup,
    ]
    for text in texts:
        for word in _FOREIGN[platform]:
            assert word not in text, f"{platform} result shows {word!r}: {text!r}"


def test_the_admin_refusal_names_the_platforms_admin() -> None:
    with pytest.raises(ToolError, match="requires a workspace admin"):
        _require_admin(_auth("slack"))
    with pytest.raises(ToolError, match="requires an admin"):
        _require_admin(_auth(None))
