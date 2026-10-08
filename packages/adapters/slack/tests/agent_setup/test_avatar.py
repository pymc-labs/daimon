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
from daimon.core.stores.security_audit import list_events
from daimon.testing.factories import make_tenant
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_META = PanelMetadata(team_id="T1", channel_id="C1", view="avatar_upload", agent_name="Ada")


def _payload(files: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "view": {
            "id": "V_UPLOAD",
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
    assert decision.response_payload is not None
    assert decision.response_payload["response_action"] == "update"
    assert "Checking image" in str(decision.response_payload["view"])
    assert decision.response_payload["view"]["external_id"] == decision.external_id


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["files.slack.com", "files.slack-gov.com"])
async def test_fetch_avatar_uses_only_slack_file_url_and_bounds_bytes(host: str) -> None:
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
                        "user": "U1",
                        "size": 3,
                        "url_private_download": f"https://{host}/files-pri/F1",
                    },
                },
            )
        return httpx.Response(200, content=b"png")

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        assert (
            await fetch_avatar_file(client, token="xoxb-test", file_id="F1", user_id="U1") == b"png"
        )
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
            200,
            json={
                "ok": True,
                "file": {"id": "F1", "user": "U1", "size": 3, "url_private_download": url},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        with pytest.raises(ValueError, match="valid file download"):
            await fetch_avatar_file(client, token="xoxb-test", file_id="F1", user_id="U1")
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("owner", "shares"), [("OTHER", {}), ("U1", {"public": {"C1": [{}]}})])
async def test_fetch_avatar_rejects_other_users_and_shared_files(
    owner: str, shares: dict[str, Any]
) -> None:
    requests: list[str] = []

    def reply(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "ok": True,
                "file": {
                    "id": "F1",
                    "user": owner,
                    "shares": shares,
                    "size": 3,
                    "url_private_download": "https://files.slack.com/files-pri/F1",
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        with pytest.raises(ValueError, match="privately"):
            await fetch_avatar_file(client, token="xoxb-test", file_id="F1", user_id="U1")
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
                        "user": "U1",
                        "size": 3,
                        "url_private_download": "https://files.slack.com/files-pri/F1",
                    },
                },
            )
        return httpx.Response(200, content=b"x" * (MAX_UPLOAD_BYTES + 1))

    async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
        with pytest.raises(ValueError, match="2 MB"):
            await fetch_avatar_file(client, token="xoxb-test", file_id="F1", user_id="U1")


@pytest.mark.asyncio
async def test_avatar_edit_requires_admin_and_non_builtin_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = SimpleNamespace(
        anthropic=MagicMock(),
        deployment_default=DeploymentDefault(agent_name="Main"),
        settings=SimpleNamespace(agent_identity=SimpleNamespace(enabled=True)),
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
    runtime.settings.agent_identity.enabled = False
    assert not await may_edit_avatar(
        runtime, client, tenant_id=tenant_id, user_id="U", agent_name="Ada"
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
    await reset_agent_avatar(
        runtime, client, meta=_META, team_id="T1", user_id="U_ADMIN", view_id="V1"
    )  # type: ignore[arg-type]
    async with db_session_factory() as session:
        new = await get_or_create_avatar(session, tenant_id=tenant.id, agent_name="Ada")
    assert new.token != old.token and new.source == "default"
    async with db_session_factory() as session:
        assert await get_avatar_by_token(session, token=old.token) is None
        events = await list_events(session, tenant_id=tenant.id)
    assert any(
        event.operation == "agent_avatar_reset" and event.agent_name == "Ada" for event in events
    )


@pytest.mark.asyncio
async def test_reset_rejects_metadata_from_another_team(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = AsyncMock(return_value=True)
    monkeypatch.setattr(avatar_module, "may_edit_avatar", gate)
    await reset_agent_avatar(
        SimpleNamespace(), AsyncMock(), meta=_META, team_id="OTHER", user_id="U", view_id="V1"
    )  # type: ignore[arg-type]
    gate.assert_not_awaited()


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
        events = await list_events(session, tenant_id=tenant.id)
    assert new.token != old.token and new.source == "upload"
    assert any(
        event.operation == "agent_avatar_change" and event.agent_name == "Ada" for event in events
    )
    assert client.views_update.await_count >= 1
    assert "Avatar changed" in str(client.views_update.await_args_list[0].kwargs["view"])
    assert client.views_update.await_args_list[0].kwargs["external_id"] == decision.external_id


@pytest.mark.asyncio
async def test_upload_failure_updates_modal_without_ephemeral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = SimpleNamespace(sessionmaker=MagicMock())
    client = AsyncMock()
    client.token = "xoxb-test"
    monkeypatch.setattr(avatar_module, "may_edit_avatar", AsyncMock(return_value=True))
    monkeypatch.setattr(
        avatar_module, "fetch_avatar_file", AsyncMock(side_effect=ValueError("bad"))
    )
    monkeypatch.setattr(avatar_module, "_audit", AsyncMock())
    decision = evaluate_avatar_submission(_payload([{"id": "F1", "size": 100}]))

    await run_avatar_submission(
        runtime, client, team_id="T1", user_id="U_ADMIN", submission=decision
    )  # type: ignore[arg-type]

    assert "could not use" in str(client.views_update.await_args.kwargs["view"])
    client.chat_postEphemeral.assert_not_awaited()


@pytest.mark.asyncio
async def test_upload_rechecks_agent_after_download(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = SimpleNamespace(sessionmaker=MagicMock())
    client = AsyncMock()
    client.token = "xoxb-test"
    checks = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr(avatar_module, "may_edit_avatar", checks)
    monkeypatch.setattr(avatar_module, "fetch_avatar_file", AsyncMock(return_value=_jpeg_bytes()))
    audit = AsyncMock()
    monkeypatch.setattr(avatar_module, "_audit", audit)
    decision = evaluate_avatar_submission(_payload([{"id": "F1", "size": 100}]))

    await run_avatar_submission(
        runtime, client, team_id="T1", user_id="U_ADMIN", submission=decision
    )  # type: ignore[arg-type]

    assert checks.await_count == 2
    assert audit.await_args.kwargs["outcome"] == "denied"
    assert "no longer available" in str(client.views_update.await_args.kwargs["view"])


def _jpeg_bytes() -> bytes:
    image = io.BytesIO()
    Image.new("RGB", (80, 60), "blue").save(image, format="JPEG")
    return image.getvalue()
