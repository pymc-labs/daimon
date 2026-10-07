"""Avatar upload and reset from the Slack agent details panel."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
import structlog
from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.agent_setup.panel_views import AVATAR_FILE_INPUT_ID
from daimon.adapters.slack.agent_setup.state import PanelMetadata, decode_panel_metadata
from daimon.adapters.slack.credential_submissions import post_ephemeral
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.agent_avatar_image import MAX_UPLOAD_BYTES, normalize_avatar_image
from daimon.core.agent_identity import is_builtin_agent
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.panel_audit import PanelOutcome, record_panel_write
from daimon.core.slack_files import SLACK_FILES_INFO_URL
from daimon.core.stores.agent_avatars import replace_avatar, reset_avatar
from daimon.core.stores.identity import get_or_create_platform_principal
from slack_sdk.web.async_client import AsyncWebClient

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class AvatarSubmission:
    response_payload: dict[str, Any] | None
    meta: PanelMetadata | None = None
    file_id: str | None = None

    @property
    def proceed(self) -> bool:
        return self.meta is not None and self.file_id is not None


def evaluate_avatar_submission(payload: dict[str, Any]) -> AvatarSubmission:
    """Validate the modal's file reference before acknowledging Slack."""
    view: dict[str, Any] = payload.get("view") or {}
    meta = decode_panel_metadata(str(view.get("private_metadata") or ""))
    if meta is None or meta.view != "avatar_upload" or not meta.agent_name:
        return AvatarSubmission(None)
    state: dict[str, Any] = view.get("state") or {}
    values: dict[str, Any] = state.get("values") or {}
    block = cast(dict[str, Any], values.get(AVATAR_FILE_INPUT_ID) or {})
    item = cast(dict[str, Any], block.get(AVATAR_FILE_INPUT_ID) or {})
    files: object = item.get("files")
    if not isinstance(files, list):
        return AvatarSubmission(
            {"response_action": "errors", "errors": {AVATAR_FILE_INPUT_ID: "Choose one image."}}
        )
    files = cast(list[object], files)
    if len(files) != 1 or not isinstance(files[0], dict):
        return AvatarSubmission(
            {"response_action": "errors", "errors": {AVATAR_FILE_INPUT_ID: "Choose one image."}}
        )
    file = cast(dict[str, object], files[0])
    file_id = file.get("id")
    if not isinstance(file_id, str) or not file_id:
        return AvatarSubmission(
            {"response_action": "errors", "errors": {AVATAR_FILE_INPUT_ID: "Choose one image."}}
        )
    try:
        size = int(str(file.get("size") or 0))
    except ValueError:
        size = MAX_UPLOAD_BYTES + 1
    if size > MAX_UPLOAD_BYTES:
        return AvatarSubmission(
            {
                "response_action": "errors",
                "errors": {AVATAR_FILE_INPUT_ID: "Choose an image of at most 2 MB."},
            }
        )
    return AvatarSubmission(None, meta=meta, file_id=file_id)


async def fetch_avatar_file(http: httpx.AsyncClient, *, token: str, file_id: str) -> bytes:
    """Fetch one bounded Slack-hosted file; never follow an arbitrary URL."""
    headers = {"Authorization": f"Bearer {token}"}
    info = await http.get(SLACK_FILES_INFO_URL, params={"file": file_id}, headers=headers)
    info.raise_for_status()
    raw_data: object = info.json()
    if not isinstance(raw_data, dict):
        raise ValueError("Slack could not read that file.")
    data = cast(dict[str, object], raw_data)
    if not data.get("ok"):
        raise ValueError("Slack could not read that file.")
    raw_file = data.get("file")
    if not isinstance(raw_file, dict):
        raise ValueError("Slack returned a different file.")
    file = cast(dict[str, object], raw_file)
    if file.get("id") != file_id:
        raise ValueError("Slack returned a different file.")
    reported_size = file.get("size")
    if not isinstance(reported_size, int | str):
        raise ValueError("Slack did not report the file size.")
    if int(reported_size) > MAX_UPLOAD_BYTES:
        raise ValueError("Choose an image of at most 2 MB.")
    url = str(file.get("url_private_download") or "")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "files.slack.com"
        or parsed.port is not None
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Slack did not provide a valid file download.")
    async with http.stream("GET", url, headers=headers, follow_redirects=False) as response:
        response.raise_for_status()
        if (
            response.is_redirect
            or int(response.headers.get("content-length") or 0) > MAX_UPLOAD_BYTES
        ):
            raise ValueError("Choose an image of at most 2 MB.")
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > MAX_UPLOAD_BYTES:
                raise ValueError("Choose an image of at most 2 MB.")
    return bytes(body)


async def may_edit_avatar(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    tenant_id: uuid.UUID,
    user_id: str,
    agent_name: str,
) -> bool:
    if not await resolve_is_admin(client, user_id=user_id):
        return False
    agent = await find_agent_by_daimon_tag(runtime.anthropic, tenant_id=tenant_id, name=agent_name)
    return agent is not None and not is_builtin_agent(
        name=agent.name,
        metadata=agent.metadata,
        default_agent_name=runtime.deployment_default.agent_name,
    )


async def _audit(
    runtime: SlackRuntime,
    *,
    tenant_id: uuid.UUID,
    user_id: str,
    change: bool,
    outcome: PanelOutcome,
    reason: str,
) -> None:
    await record_panel_write(
        runtime.sessionmaker,
        tenant_id=tenant_id,
        platform="slack",
        platform_user_id=user_id,
        op="agent_avatar_change" if change else "agent_avatar_reset",
        outcome=outcome,
        reason=reason,
    )


async def _refresh_details(
    runtime: SlackRuntime, client: AsyncWebClient, *, tenant_id: uuid.UUID, meta: PanelMetadata
) -> None:
    if not meta.root_view_id or not meta.agent_name:
        return
    from daimon.adapters.slack.agent_setup.actions import load_details_view

    view = await load_details_view(
        runtime, tenant_id=tenant_id, meta=meta, agent_name=meta.agent_name, is_admin=True
    )
    if view is not None:
        await client.views_update(view_id=meta.root_view_id, view=view)  # pyright: ignore[reportUnknownMemberType]


async def reset_agent_avatar(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    meta: PanelMetadata,
    user_id: str,
    view_id: str,
) -> None:
    if not meta.agent_name:
        return
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=meta.team_id)
    if not await may_edit_avatar(
        runtime, client, tenant_id=tenant_id, user_id=user_id, agent_name=meta.agent_name
    ):
        await _audit(
            runtime,
            tenant_id=tenant_id,
            user_id=user_id,
            change=False,
            outcome="denied",
            reason="needs_admin_or_agent_gone",
        )
        return
    async with runtime.sessionmaker.begin() as session:
        actor = await get_or_create_platform_principal(
            session, platform="slack", external_id=user_id, tenant_id=tenant_id
        )
        await reset_avatar(
            session,
            tenant_id=tenant_id,
            agent_name=meta.agent_name,
            updated_by_account_id=actor.account_id,
        )
    await _audit(
        runtime,
        tenant_id=tenant_id,
        user_id=user_id,
        change=False,
        outcome="allowed",
        reason="completed",
    )
    await _refresh_details(
        runtime,
        client,
        tenant_id=tenant_id,
        meta=meta.with_view("details", agent_name=meta.agent_name, root_view_id=view_id),
    )


async def run_avatar_submission(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    team_id: str,
    user_id: str,
    submission: AvatarSubmission,
) -> None:
    meta, file_id = submission.meta, submission.file_id
    if meta is None or file_id is None or not meta.agent_name or meta.team_id != team_id:
        return
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    if not await may_edit_avatar(
        runtime, client, tenant_id=tenant_id, user_id=user_id, agent_name=meta.agent_name
    ):
        await _audit(
            runtime,
            tenant_id=tenant_id,
            user_id=user_id,
            change=True,
            outcome="denied",
            reason="needs_admin_or_agent_gone",
        )
        return
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as http:
            async with asyncio.timeout(15):
                body = await fetch_avatar_file(http, token=client.token or "", file_id=file_id)
        png = await asyncio.to_thread(normalize_avatar_image, body)
    except (httpx.HTTPError, TimeoutError, ValueError) as exc:
        log.warning("slack.agent_avatar.upload_rejected", error_type=type(exc).__name__)
        await _audit(
            runtime,
            tenant_id=tenant_id,
            user_id=user_id,
            change=True,
            outcome="error",
            reason="invalid_image",
        )
        await post_ephemeral(
            client,
            channel_id=meta.channel_id or user_id,
            user_id=user_id,
            text="I could not use that image. Upload one PNG, JPG, GIF, or WebP under 2 MB.",
        )
        return
    async with runtime.sessionmaker.begin() as session:
        actor = await get_or_create_platform_principal(
            session, platform="slack", external_id=user_id, tenant_id=tenant_id
        )
        await replace_avatar(
            session,
            tenant_id=tenant_id,
            agent_name=meta.agent_name,
            png=png,
            source="upload",
            updated_by_account_id=actor.account_id,
        )
    await _audit(
        runtime,
        tenant_id=tenant_id,
        user_id=user_id,
        change=True,
        outcome="allowed",
        reason="completed",
    )
    await _refresh_details(runtime, client, tenant_id=tenant_id, meta=meta)
