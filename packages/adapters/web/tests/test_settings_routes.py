"""The web app's Settings group: approvals and the cost footer beside the switches.

Every write goes through `daimon.core.scope_settings.change_setting`. Approvals need the
person's linked chat account to pass the live native admin check; a web-only owner is
refused. Stale writes are 409s, members are 403s, and the cross-site check still applies.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from daimon.core import scope_settings, web_identity
from daimon.core.authz import Subject
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import PUBLIC_URL, WebClientFactory
from .test_channels_routes import _channels  # pyright: ignore[reportPrivateUsage]

_APPROVALS = [
    ("/api/channels/c1/approvals", {"mode": "off", "expected": None}),
    ("/api/channel-defaults/approvals", {"mode": "off", "expected": None}),
]
_FOOTER = ("/api/channel-defaults/cost-footer", {"shown": False, "expected": True})


def _linked(monkeypatch: pytest.MonkeyPatch, *, admin: bool) -> list[str]:
    """The signed-in person has a linked Discord account; its live admin check answers `admin`."""
    checked: list[str] = []

    async def chat_identity(*_: object, **__: object) -> tuple[str, str]:
        return "discord", "u-linked"

    async def approval_admin(*_: object, user_id: str, **__: object) -> Subject:
        checked.append(user_id)
        return Subject(is_admin=admin, platform_user_id=user_id)

    monkeypatch.setattr(web_identity, "chat_identity", chat_identity)
    monkeypatch.setattr(scope_settings, "approval_admin", approval_admin)
    return checked


@pytest.mark.parametrize("path", [_APPROVALS[0][0], _APPROVALS[1][0], _FOOTER[0]])
async def test_writes_without_the_cross_site_header_are_refused(
    web_client: WebClientFactory, path: str
) -> None:
    async with web_client() as client:
        res = await client.put(path, json={}, headers={"Origin": PUBLIC_URL})
    assert res.status_code == 403


async def test_members_read_approvals_and_the_footer_but_never_write(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with _channels(web_client, db_session_factory, tmp_path) as admin:
        admin.as_member()
        defaults = await admin.client.get("/api/channel-defaults")
        writes = [
            (await admin.client.put(url, json=body)).status_code
            for url, body in [*_APPROVALS, _FOOTER]
        ]

    assert defaults.status_code == 200, defaults.text
    assert defaults.json()["approvals"]["editable"] is False
    assert writes == [403, 403, 403]


async def test_a_web_only_owner_cannot_change_approvals(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with _channels(web_client, db_session_factory, tmp_path) as admin:
        writes = [(await admin.client.put(url, json=body)).status_code for url, body in _APPROVALS]
        settings = (await admin.client.get("/api/channels/c1/settings")).json()

    assert writes == [403, 403], "an owner with no linked chat account is no chat admin"
    assert settings["approvals"]["own"] is None, "a refused change writes nothing"
    assert settings["approvals"]["editable"] is False


async def test_a_linked_member_who_is_no_chat_admin_is_refused(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked = _linked(monkeypatch, admin=False)
    async with _channels(web_client, db_session_factory, tmp_path) as admin:
        res = await admin.client.put(_APPROVALS[0][0], json=_APPROVALS[0][1])

    assert res.status_code == 403, res.text
    assert checked == ["u-linked"], "the live check names the linked account"


async def test_a_linked_chat_admin_changes_approvals_recorded_and_stale_is_409(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked = _linked(monkeypatch, admin=True)
    async with _channels(web_client, db_session_factory, tmp_path) as admin:
        first = await admin.client.put(_APPROVALS[0][0], json=_APPROVALS[0][1])
        stale = await admin.client.put(_APPROVALS[0][0], json=_APPROVALS[0][1])
        settings = (await admin.client.get("/api/channels/c1/settings")).json()
        history = (await admin.client.get("/api/channels/c1/history")).json()["entries"]

    assert first.status_code == 200, first.text
    assert stale.status_code == 409, "the value read was inherit; it is off now"
    assert settings["approvals"] == {
        "available": True,
        "own": "off",
        "value": "off",
        "source": "channel",
        "active": False,
        "editable": True,
    }
    assert [(h["setting"], h["after"]) for h in history] == [("approvals", "off")]
    assert checked and set(checked) == {"u-linked"}


async def test_an_owner_hides_the_cost_footer_and_stale_is_409(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    async with _channels(web_client, db_session_factory, tmp_path) as admin:
        hidden = await admin.client.put(_FOOTER[0], json=_FOOTER[1])
        stale = await admin.client.put(_FOOTER[0], json=_FOOTER[1])

    assert hidden.status_code == 200, hidden.text
    assert hidden.json()["cost_footer"] == {"shown": False, "editable": True}
    assert stale.status_code == 409
