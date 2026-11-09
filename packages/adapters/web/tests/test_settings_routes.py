"""The web app reads approvals and the cost footer; they are changed in Settings, not here.

The channel and defaults reads say whether the signed-in person may edit approvals. That
needs a linked chat account; a web-only owner may not. The live native admin check runs when a change is made.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from daimon.core import web_identity
from daimon.core.authz import Subject
from daimon.core.features import service as feature_service
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import WebClientFactory
from .test_channels_routes import _channels  # pyright: ignore[reportPrivateUsage]


def _linked(monkeypatch: pytest.MonkeyPatch, *, admin: bool) -> list[str]:
    """The signed-in person has a linked Discord account; its live admin check answers `admin`."""
    checked: list[str] = []

    async def chat_identity(*_: object, **__: object) -> tuple[str, str]:
        return "discord", "u-linked"

    async def approval_admin(*_: object, user_id: str, **__: object) -> Subject:
        checked.append(user_id)
        return Subject(is_admin=admin, platform_user_id=user_id)

    monkeypatch.setattr(web_identity, "chat_identity", chat_identity)
    monkeypatch.setattr(feature_service, "approval_admin", approval_admin)
    return checked


async def test_members_read_approvals_and_the_footer_as_not_editable(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with _channels(web_client, db_session_factory, tmp_path) as admin:
        admin.as_member()
        defaults = await admin.client.get("/api/channel-defaults")

    assert defaults.status_code == 200, defaults.text
    assert defaults.json()["approvals"]["editable"] is False
    assert defaults.json()["cost_footer"]["editable"] is False


@pytest.mark.parametrize("linked", [False, True], ids=["web_only_owner", "linked_non_admin"])
async def test_approvals_are_editable_only_with_a_linked_chat_account(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    linked: bool,
) -> None:
    if linked:
        _linked(monkeypatch, admin=False)
    async with _channels(web_client, db_session_factory, tmp_path) as admin:
        settings = (await admin.client.get("/api/channels/c1/settings")).json()

    assert settings["approvals"]["own"] is None
    assert settings["approvals"]["editable"] is linked
