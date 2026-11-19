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


@pytest.mark.parametrize("linked", [False, True], ids=["web_only_owner", "linked_non_admin"])
async def test_the_web_actor_is_refused_an_approvals_change(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    linked: bool,
) -> None:
    """A workspace owner is not a native admin, so approvals never change from the web."""
    if linked:
        _linked(monkeypatch, admin=False)
    async with _channels(web_client, db_session_factory, tmp_path) as admin:
        refused = await admin.client.put(
            "/api/features?channel=c1",
            json={"key": "approvals", "tier": "channel", "value": "off"},
        )
        read = await admin.client.get("/api/channels/c1/settings")

    assert refused.status_code == 403, refused.text
    assert read.json()["approvals"]["own"] is None, "nothing was written"
