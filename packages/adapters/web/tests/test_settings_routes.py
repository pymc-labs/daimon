"""The web app reads approvals and the cost footer; they are changed in Settings, not here.

The channel and defaults reads say whether the signed-in person may edit approvals. That
needs a linked chat account; a web-only owner may not. The live native admin check runs when a change is made.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from daimon.adapters.web import channel_settings
from daimon.core import web_identity
from daimon.core.access_policy import AgentApprovals, TenantAccessPolicy
from daimon.core.authz import Subject
from daimon.core.features import service as feature_service
from daimon.core.stores.access_policy import set_access_policy
from daimon.core.stores.channel_changes import write_workspace_default
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


async def test_a_channel_reads_the_approvals_of_the_agent_answering_there(
    web_client: WebClientFactory,
    db_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent's own Approvals applies in the channel it answers in: the page shows that
    value and where it came from, not the workspace's."""
    async with _channels(web_client, db_session_factory, tmp_path) as admin:
        answering = "helper"
        agent = SimpleNamespace(id="ag_answering", name=answering, metadata={})

        async def listed(*_: object) -> list[SimpleNamespace]:
            return [agent]

        monkeypatch.setattr(channel_settings, "listed_agents", listed)
        async with db_session_factory.begin() as db:
            await write_workspace_default(
                db,
                tenant_id=admin.ws.tenant_id,
                platform="discord",
                agent_name=answering,
                actor_account_id=None,
                surface="web",
            )
            await set_access_policy(
                db,
                tenant_id=admin.ws.tenant_id,
                policy=TenantAccessPolicy(
                    agent_approvals={
                        agent.id: AgentApprovals(approvals="off", approvals_set_by="admin-1")
                    }
                ),
            )
        read = (await admin.client.get("/api/channels/c1/settings")).json()["approvals"]

    assert (read["value"], read["source"]) == ("off", "agent"), read
    assert read["own"] is None, "the channel itself sets nothing"
