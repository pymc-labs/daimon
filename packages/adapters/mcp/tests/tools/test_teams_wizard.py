"""post_wizard on Teams: the first screen is an Adaptive Card, stored against where it lives."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools.teams._client import TeamsBotClient
from daimon.adapters.mcp.tools.wizard import (
    _post_wizard_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.domain import Role
from daimon.core.stores.wizard_session import get_wizard_session
from daimon.core.wizard.spec import Option, Step, StepKind
from daimon.testing.factories import make_account, make_tenant
from fastmcp.exceptions import ToolError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_ENTRA = "99999999-8888-7777-6666-555555555555"
_CALLER = "11111111-2222-3333-4444-555555555555"
_CHANNEL = "19:chan@thread.tacv2"
_BASE = "https://smba.trafficmanager.net/teams/v3/conversations"
_STEP = Step(
    key="color",
    question="Pick a color",
    kind=StepKind.CHOICE,
    options=[Option(label="Red", value="red")],
)


class _Fake:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if request.url.host == "login.microsoftonline.com":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if "/members/" in url:
            return httpx.Response(200, json={"id": "29:x", "aadObjectId": _CALLER})
        if request.method == "POST" and url == _BASE:
            return httpx.Response(200, json={"id": f"{_CHANNEL};messageid=77", "activityId": "77"})
        return httpx.Response(200, json={"id": "act-1"})

    def card(self) -> dict[str, object]:
        [post] = [r for r in self.requests if r.method == "POST" and "login." not in str(r.url)]
        body = json.loads(post.content)
        return (body.get("activity") or body)["attachments"][0]["content"]


def _runtime(fake: _Fake, sessionmaker: async_sessionmaker[AsyncSession]) -> McpRuntime:
    client = TeamsBotClient(
        httpx.AsyncClient(transport=httpx.MockTransport(fake)),
        client_id="app-id",
        client_secret="secret",
        tenant_id=_ENTRA,
    )
    return McpRuntime(
        session_factory=sessionmaker,
        client=MagicMock(),  # type: ignore[arg-type]  # unused by the Teams impls
        settings=MagicMock(),  # type: ignore[arg-type]  # unused by the Teams impls
        deployment_default=DeploymentDefault(),
        teams_client=client,
    )


async def _auth(db_session: AsyncSession) -> AuthIdentity:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    return AuthIdentity(
        account_id=account.id,
        tenant_id=tenant.id,
        role=Role.USER,
        platform="teams",
        platform_user_id=_CALLER,
    )


async def _only_row(db_session: AsyncSession) -> str:
    return (await db_session.execute(text("SELECT id FROM wizard_session"))).scalar_one()


@pytest.mark.parametrize(
    ("channel_id", "stored", "message_id"),
    [
        (_CHANNEL, f"{_CHANNEL};messageid=77", "77"),
        ("a:chat-1", "a:chat-1", "act-1"),
    ],
)
async def test_a_form_is_a_card_stored_where_it_lives(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    channel_id: str,
    stored: str,
    message_id: str,
) -> None:
    fake = _Fake()
    result = await _post_wizard_impl(
        _runtime(fake, sessionmaker),
        await _auth(db_session),
        prompt="Order form",
        steps=[_STEP],
        channel_id=channel_id,
    )
    assert result.startswith("WIZARD_POSTED")
    assert fake.card()["type"] == "AdaptiveCard"
    row = await get_wizard_session(db_session, short_id=await _only_row(db_session))
    assert row is not None
    assert (row.channel_id, row.message_id) == (stored, message_id), (
        "a bare channel's form starts a post and lives in its thread"
    )


@pytest.mark.parametrize(
    ("channel_id", "step", "match"),
    [
        ("19:group@thread.v2", _STEP, "not a group chat"),
        (_CHANNEL, _STEP.model_copy(update={"image_handle": "h1"}), "Discord-only"),
    ],
)
async def test_a_form_is_refused_in_a_group_chat_or_with_images(
    db_session: AsyncSession,
    sessionmaker: async_sessionmaker[AsyncSession],
    channel_id: str,
    step: Step,
    match: str,
) -> None:
    fake = _Fake()
    with pytest.raises(ToolError, match=match):
        await _post_wizard_impl(
            _runtime(fake, sessionmaker),
            await _auth(db_session),
            prompt="Order form",
            steps=[step],
            channel_id=channel_id,
        )
    assert [r for r in fake.requests if "/activities" in str(r.url)] == []
