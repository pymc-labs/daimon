"""The `privacy` panel, driven through the real SDK route: command, export, confirmed delete.

Only the outbound Bot Framework transport and MA are faked.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import AbstractAsyncContextManager

import pytest
from daimon.adapters.teams.http_service import TeamsHttpService
from daimon.adapters.teams.identity import DENIED
from daimon.adapters.teams.privacy_panel import DELETING, NAME_MISMATCH, STALE
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.identity import find_platform_principal, get_or_create_platform_principal
from daimon.testing.ma import build_fake_anthropic, make_fake_ma_handler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .conftest import (
    AAD_OBJECT_ID,
    CONVERSATION_ID,
    ENTRA_TENANT_ID,
    OTHER_AAD_OBJECT_ID,
    USER_NAME,
    TeamsApiFake,
    build_teams_runtime,
    make_card_action,
    make_message_activity,
    post_activity,
    running_service,
)

pytestmark = pytest.mark.usefixtures("entra_env", "stub_bot_token", "provisioned_tenant")
TENANT = derive_tenant_uuid(platform="teams", workspace_id=ENTRA_TENANT_ID)
POLICY_URL = "https://example.com/privacy"
NAME = USER_NAME


def _running(
    db_factory: async_sessionmaker[AsyncSession], fake: TeamsApiFake
) -> AbstractAsyncContextManager[TeamsHttpService]:
    runtime = build_teams_runtime(
        db_factory, anthropic=build_fake_anthropic(make_fake_ma_handler())
    )
    runtime.settings.privacy_policy_url = POLICY_URL
    return running_service(runtime, fake)


def _click(op: str, *, user: str = AAD_OBJECT_ID, **extra: str) -> dict[str, object]:
    return make_card_action("privacy", op, user=user, **extra)


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


async def test_command_without_data_says_so_and_creates_nothing(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    async with _running(db_session_factory, teams_api_fake) as service:
        await post_activity(service, make_message_activity(text="privacy"))
        await service.turns.drain(timeout=30)

    assert "no data on file" in json.dumps(teams_api_fake.activity_requests[-1].body)
    assert not await _has_principal(db_session_factory, AAD_OBJECT_ID), "the read is read-only"


async def test_command_shows_holdings_and_export_summarises_them(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    await _account(db_session_factory, AAD_OBJECT_ID)
    async with _running(db_session_factory, teams_api_fake) as service:
        await post_activity(service, make_message_activity(text="privacy"))
        await service.turns.drain(timeout=30)
        export = await post_activity(service, _click("export"))

    panel = json.dumps(teams_api_fake.activity_requests[-1].body)
    assert "holds: 1 linked principal(s)" in panel, "the panel summarises what is held"
    assert POLICY_URL in panel and "Action.OpenUrl" in panel, "the policy opens as a link"
    assert "holds: 1 linked principal(s)" in json.dumps(export), "export shows the summary"


async def test_delete_asks_for_the_typed_name_and_refuses_a_mismatch(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    account_id = str(await _account(db_session_factory, AAD_OBJECT_ID))
    async with _running(db_session_factory, teams_api_fake) as service:
        confirm = await post_activity(service, _click("delete"))
        wrong = await post_activity(
            service, _click("confirm_delete", account=account_id, confirm_name="ada")
        )

    assert f"Type '{NAME}' to confirm" in json.dumps(confirm), "delete asks for the name first"
    assert NAME_MISMATCH in json.dumps(wrong), "a wrong name re-asks"
    assert await _has_principal(db_session_factory, AAD_OBJECT_ID), "nothing was deleted"


async def test_a_forwarded_confirmation_deletes_nothing(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    victim = str(await _account(db_session_factory, AAD_OBJECT_ID))
    await _account(db_session_factory, OTHER_AAD_OBJECT_ID)
    async with _running(db_session_factory, teams_api_fake) as service:
        response = await post_activity(
            service,
            _click("confirm_delete", user=OTHER_AAD_OBJECT_ID, account=victim, confirm_name=NAME),
        )

    assert STALE in json.dumps(response), "the clicker's own account must match the card's"
    assert await _has_principal(db_session_factory, AAD_OBJECT_ID), "the owner keeps their data"
    assert await _has_principal(db_session_factory, OTHER_AAD_OBJECT_ID), "so does the clicker"


async def test_a_confirmed_delete_purges_and_edits_the_card_in_place(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    account_id = str(await _account(db_session_factory, AAD_OBJECT_ID))
    async with _running(db_session_factory, teams_api_fake) as service:
        response = await post_activity(
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


async def test_a_click_from_another_organisation_is_refused(
    db_session_factory: async_sessionmaker[AsyncSession], teams_api_fake: TeamsApiFake
) -> None:
    await _account(db_session_factory, AAD_OBJECT_ID)
    click = _click("export")
    click["conversation"] = {"id": CONVERSATION_ID, "tenantId": str(uuid.UUID(int=99))}
    async with _running(db_session_factory, teams_api_fake) as service:
        response = await post_activity(service, click)

    assert response["value"] == DENIED, "an unverified clicker sees nothing"
