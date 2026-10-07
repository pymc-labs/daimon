"""Slack avatar form and bounded file download."""

from __future__ import annotations

import io
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from daimon.adapters.slack.agent_setup import avatar as avatar_module
from daimon.adapters.slack.agent_setup.avatar import (
    evaluate_avatar_submission,
    fetch_avatar_file,
    may_edit_avatar,
    reset_agent_avatar,
    run_avatar_submission,
)
from daimon.adapters.slack.agent_setup.panel_views import (
    AVATAR_FILE_INPUT_ID,
    build_avatar_upload_form,
)
from daimon.adapters.slack.agent_setup.state import PanelMetadata, encode_panel_metadata
from daimon.core.agent_avatar_image import MAX_UPLOAD_BYTES
from daimon.core.scope import DeploymentDefault
from daimon.core.stores.agent_avatars import get_avatar_by_token, get_or_create_avatar
from daimon.testing.factories import make_tenant
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_META = PanelMetadata(team_id="T1", channel_id="C1", view="avatar_upload", agent_name="Ada")


def _payload(files: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "view": {
            "private_metadata": encode_panel_metadata(_META),
            "state": {"values": {AVATAR_FILE_INPUT_ID: {AVATAR_FILE_INPUT_ID: {"files": files}}}},
        }
    }


def test_upload_form_and_submission_validation() -> None:
    form = build_avatar_upload_form(meta=_META)
    assert form["callback_id"] == "agent_setup__avatar_upload"
    assert any(block.get("element", {}).get("type") == "file_input" for block in form["blocks"])
    assert evaluate_avatar_submission(_payload([])).response_payload is not None
    assert (
        evaluate_avatar_submission(_payload([{"id": "F1", "size": "bad"}])).response_payload
        is not None
    )
    assert (
        evaluate_avatar_submission(
            _payload([{"id": "F1", "size": MAX_UPLOAD_BYTES + 1}])
        ).response_payload
        is not None
    )
    decision = evaluate_avatar_submission(_payload([{"id": "F1", "size": 42}]))
    assert decision.proceed and decision.file_id == "F1"


@pytest.mark.asyncio
async def test_fetch_avatar_uses_only_slack_file_url_and_bounds_bytes() -> None:
    requests: list[str] = []

    def reply(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        assert request.headers["Authorization"] == "Bearer xoxb-test"
        if request.url.host == "slack.com":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "file": {
                        "id": "F1",
                        "size": 3,
                        "url_private_download": "https://files.slack.com/files-pri/F1",
                    },
                },
            )
        return httpx.Response(200, content=b"png")

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        assert await fetch_avatar_file(client, token="xoxb-test", file_id="F1") == b"png"
    assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/image",
        "http://files.slack.com/image",
        "https://files.slack.com.evil.test/image",
    ],
)
async def test_fetch_avatar_rejects_non_slack_urls(url: str) -> None:
    requests: list[str] = []

    def reply(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            200, json={"ok": True, "file": {"id": "F1", "size": 3, "url_private_download": url}}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        with pytest.raises(ValueError, match="valid file download"):
            await fetch_avatar_file(client, token="xoxb-test", file_id="F1")
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_fetch_avatar_rejects_oversize_download() -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        if request.url.host == "slack.com":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "file": {
                        "id": "F1",
                        "size": 3,
                        "url_private_download": "https://files.slack.com/files-pri/F1",
                    },
                },
            )
        return httpx.Response(200, content=b"x" * (MAX_UPLOAD_BYTES + 1))

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        with pytest.raises(ValueError, match="2 MB"):
            await fetch_avatar_file(client, token="xoxb-test", file_id="F1")


@pytest.mark.asyncio
async def test_avatar_edit_requires_admin_and_non_builtin_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = SimpleNamespace(
        anthropic=MagicMock(), deployment_default=DeploymentDefault(agent_name="Main")
    )
    client = AsyncMock()
    admin = AsyncMock(return_value=False)
    lookup = AsyncMock(return_value=SimpleNamespace(name="Ada", metadata={}))
    monkeypatch.setattr(avatar_module, "resolve_is_admin", admin)
    monkeypatch.setattr(avatar_module, "find_agent_by_daimon_tag", lookup)
    tenant_id = uuid.uuid4()
    assert not await may_edit_avatar(
        runtime, client, tenant_id=tenant_id, user_id="U", agent_name="Ada"
    )  # type: ignore[arg-type]
    lookup.assert_not_awaited()
    admin.return_value = True
    assert await may_edit_avatar(
        runtime, client, tenant_id=tenant_id, user_id="U", agent_name="Ada"
    )  # type: ignore[arg-type]
    lookup.return_value = SimpleNamespace(name="Main", metadata={})
    assert not await may_edit_avatar(
        runtime, client, tenant_id=tenant_id, user_id="U", agent_name="Main"
    )  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_reset_rotates_token_and_restores_default(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T1")
    old = await get_or_create_avatar(db_session, tenant_id=tenant.id, agent_name="Ada")
    await db_session.commit()
    runtime = SimpleNamespace(sessionmaker=db_session_factory)
    client = AsyncMock()
    monkeypatch.setattr(avatar_module, "may_edit_avatar", AsyncMock(return_value=True))
    monkeypatch.setattr(avatar_module, "_refresh_details", AsyncMock())
    await reset_agent_avatar(runtime, client, meta=_META, user_id="U_ADMIN", view_id="V1")  # type: ignore[arg-type]
    async with db_session_factory() as session:
        new = await get_or_create_avatar(session, tenant_id=tenant.id, agent_name="Ada")
    assert new.token != old.token and new.source == "default"
    async with db_session_factory() as session:
        assert await get_avatar_by_token(session, token=old.token) is None


@pytest.mark.asyncio
async def test_upload_rotates_token_and_records_upload(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T1")
    old = await get_or_create_avatar(db_session, tenant_id=tenant.id, agent_name="Ada")
    await db_session.commit()
    image = io.BytesIO()
    Image.new("RGB", (80, 60), "blue").save(image, format="JPEG")
    runtime = SimpleNamespace(sessionmaker=db_session_factory)
    client = AsyncMock()
    client.token = "xoxb-test"
    monkeypatch.setattr(avatar_module, "may_edit_avatar", AsyncMock(return_value=True))
    monkeypatch.setattr(
        avatar_module, "fetch_avatar_file", AsyncMock(return_value=image.getvalue())
    )
    monkeypatch.setattr(avatar_module, "_refresh_details", AsyncMock())
    decision = evaluate_avatar_submission(_payload([{"id": "F1", "size": 100}]))
    await run_avatar_submission(
        runtime, client, team_id="T1", user_id="U_ADMIN", submission=decision
    )  # type: ignore[arg-type]
    async with db_session_factory() as session:
        new = await get_or_create_avatar(session, tenant_id=tenant.id, agent_name="Ada")
    assert new.token != old.token and new.source == "upload"
