"""enable_channel_files: the agent's ask for the Teams Enable files card."""

from __future__ import annotations

import uuid

import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.tools.enable_files import enable_channel_files_impl
from daimon.core.stores.domain import Role
from fastmcp.exceptions import ToolError


def _auth(*, platform: str = "teams", is_admin: bool = True) -> AuthIdentity:
    return AuthIdentity(
        account_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        role=Role.ADMIN if is_admin else Role.USER,
        platform=platform,
        platform_user_id="11111111-2222-3333-4444-555555555555",
        is_admin=is_admin,
    )


def test_an_admin_on_teams_is_told_the_card_follows_the_reply() -> None:
    """The adapter posts the card on seeing this call; the agent only says what comes."""
    reply = enable_channel_files_impl(_auth())
    assert "card will be posted" in reply and "member of this channel" in reply, reply


@pytest.mark.parametrize(
    ("auth", "match"),
    [
        (_auth(is_admin=False), "requires a workspace or server admin"),
        (_auth(platform="slack"), "Only Teams channels"),
    ],
)
def test_anyone_else_is_refused_so_no_card_is_promised(auth: AuthIdentity, match: str) -> None:
    """A refused call is an error, which the adapter does not take as an ask."""
    with pytest.raises(ToolError, match=match):
        enable_channel_files_impl(auth)
