"""The `privacy` panel, driven through the real SDK route: command, export, confirmed delete.

Only the outbound Bot Framework transport and MA are faked.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from daimon.adapters.teams.http_service import TeamsHttpService, create_teams_http_service
from daimon.adapters.teams.identity import DENIED
from daimon.adapters.teams.privacy_panel import DELETING, NAME_MISMATCH, STALE
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.identity import find_platform_principal, get_or_create_platform_principal
from daimon.testing.asgi import asgi_lifespan
from daimon.testing.ma import build_fake_anthropic, make_fake_ma_handler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    BOT_ACCOUNT_ID,
    CONVERSATION_ID,
    ENTRA_TENANT_ID,
    OTHER_AAD_OBJECT_ID,
    SERVICE_URL,
    TeamsApiFake,
    build_teams_client,
    build_teams_runtime,
    make_message_activity,
    teams_settings,
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token")
TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
POLICY_URL = "https://example.com/privacy"
NAME = "Ada Lovelace"


@pytest.fixture(autouse=True)
async def provisioned_tenant(db_session_factory: async_sessionmaker[AsyncSession]) -> None:
    await provision_tenant(db_session_factory, platform="teams", workspace_id=ENTRA_TENANT_ID)


@asynccontextmanager
async def _running(
    db_factory: async_sessionmaker[AsyncSession], fake: TeamsApiFake
) -> AsyncIterator[TeamsHttpService]:
    settings = teams_settings()
    runtime = build_teams_runtime(
        db_factory, anthropic=build_fake_anthropic(make_fake_ma_handler()), teams=settings
    )
    runtime.settings.privacy_policy_url = POLICY_URL
    service = create_teams_http_service(
        settings=settings, runtime=runtime, client=build_teams_client(fake)
    )
    async with asgi_lifespan(service.app):
        await service.turns.start()
        yield service


async def _post(service: TeamsHttpService, payload: dict[str, object]) -> Any:
    transport = httpx.ASGITransport(app=service.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/messages", json=payload)
    assert response.status_code == 200, response.text
    return response.json() if response.content else None


def _click(op: str, *, user: str = AAD_OBJECT_ID, **extra: str) -> dict[str, object]:
    data: dict[str, object] = {"action": "privacy", "op": op} | extra
    action = {"type": "Action.Execute", "verb": "privacy", "data": data}
    return {
        "type": "invoke",
        "name": "adaptiveCard/action",
        "id": f"invoke-{uuid.uuid4()}",
        "channelId": "msteams",
        "serviceUrl": SERVICE_URL,
        "from": {"id": f"29:{user}", "aadObjectId": user, "name": NAME},
        "recipient": {"id": BOT_ACCOUNT_ID, "name": "daimon"},
        "conversation": {
            "id": CONVERSATION_ID,
            "conversationType": "personal",
            "tenantId": ENTRA_TENANT_ID,
        },
        "replyToId": "m-7",
        "value": {"action": action, "trigger": "manual"},
    }


async def _account(db_factory: async_sessionmaker[AsyncSession], user: str) -> uuid.UUID:
    async with db_factory.begin() as session:
        principal = await get_or_create_platform_principal(
            session, tenant_id=TENANT, platform="teams", external_id=user
        )
    return principal.account_id


async def _has_principal(db_factory: async_sessionmaker[AsyncSession], user: str) -> bool:
    async with db_factory() as session:
        principal = await find_platform_principal(
            session, tenant_id=TENANT, platform="teams", external_id=user
        )
    return principal is not None


@pytest.mark.asyncio
async def test_command_without_data_says_so_and_creates_nothing(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as service:
        await _post(service, make_message_activity(text="privacy"))
        await service.turns.drain(timeout=30)

    assert "no data on file" in json.dumps(teams_api_fake.activity_requests[-1].body)
    assert not await _has_principal(db_session_factory, AAD_OBJECT_ID), "the read is read-only"


@pytest.mark.asyncio
async def test_command_shows_holdings_and_export_summarises_them(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    await _account(db_session_factory, AAD_OBJECT_ID)
    async with _running(db_session_factory, teams_api_fake) as service:
        await _post(service, make_message_activity(text="privacy"))
        await service.turns.drain(timeout=30)
        export = await _post(service, _click("export"))

    panel = json.dumps(teams_api_fake.activity_requests[-1].body)
    assert "holds: 1 linked principal(s)" in panel, "the panel summarises what is held"
    assert POLICY_URL in panel and "Action.OpenUrl" in panel, "the policy opens as a link"
    assert "holds: 1 linked principal(s)" in json.dumps(export), "export shows the summary"


@pytest.mark.asyncio
async def test_delete_asks_for_the_typed_name_and_refuses_a_mismatch(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    account_id = str(await _account(db_session_factory, AAD_OBJECT_ID))
    async with _running(db_session_factory, teams_api_fake) as service:
        confirm = await _post(service, _click("delete"))
        wrong = await _post(
            service, _click("confirm_delete", account=account_id, confirm_name="ada")
        )

    assert f"Type '{NAME}' to confirm" in json.dumps(confirm), "delete asks for the name first"
    assert NAME_MISMATCH in json.dumps(wrong), "a wrong name re-asks"
    assert await _has_principal(db_session_factory, AAD_OBJECT_ID), "nothing was deleted"


@pytest.mark.asyncio
async def test_a_forwarded_confirmation_deletes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    victim = str(await _account(db_session_factory, AAD_OBJECT_ID))
    await _account(db_session_factory, OTHER_AAD_OBJECT_ID)
    async with _running(db_session_factory, teams_api_fake) as service:
        response = await _post(
            service,
            _click("confirm_delete", user=OTHER_AAD_OBJECT_ID, account=victim, confirm_name=NAME),
        )

    assert STALE in json.dumps(response), "the clicker's own account must match the card's"
    assert await _has_principal(db_session_factory, AAD_OBJECT_ID), "the owner keeps their data"
    assert await _has_principal(db_session_factory, OTHER_AAD_OBJECT_ID), "so does the clicker"


@pytest.mark.asyncio
async def test_a_confirmed_delete_purges_and_edits_the_card_in_place(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    account_id = str(await _account(db_session_factory, AAD_OBJECT_ID))
    async with _running(db_session_factory, teams_api_fake) as service:
        response = await _post(
            service, _click("confirm_delete", account=account_id, confirm_name=NAME)
        )
        async with asyncio.timeout(10):
            while not [r for r in teams_api_fake.activity_requests if r.method == "PUT"]:
                await asyncio.sleep(0.01)

    assert DELETING in json.dumps(response, ensure_ascii=False), "the click answers at once"
    [edit] = [r for r in teams_api_fake.activity_requests if r.method == "PUT"]
    assert edit.url.endswith("/activities/m-7"), "the outcome replaces the confirmation card"
    assert "account row removed" in json.dumps(edit.body), "the outcome lists what went"
    assert not await _has_principal(db_session_factory, AAD_OBJECT_ID), "the account is purged"


@pytest.mark.asyncio
async def test_a_click_from_another_organisation_is_refused(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    await _account(db_session_factory, AAD_OBJECT_ID)
    click = _click("export")
    click["conversation"] = {"id": CONVERSATION_ID, "tenantId": str(uuid.UUID(int=99))}
    async with _running(db_session_factory, teams_api_fake) as service:
        response = await _post(service, click)

    assert response["value"] == DENIED, "an unverified clicker sees nothing"
