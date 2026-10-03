"""Operator tokens from Who answers where: mint (shown once), list and revoke.

Workspace admins only. The form's submission and the revoke select each
re-check admin status live, are audited, and refresh the routing view.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import functools
import uuid
from typing import Any, Final

import structlog
from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.agent_setup.actions import (
    OPERATOR_TOKENS_NEED_ADMIN_MESSAGE,
    load_routing_view,
)
from daimon.adapters.slack.agent_setup.panel_views import (
    OPERATOR_LABEL_INPUT_ID,
    OPERATOR_SCOPES_INPUT_ID,
)
from daimon.adapters.slack.agent_setup.state import PanelMetadata, decode_panel_metadata
from daimon.adapters.slack.credential_submissions import post_ephemeral
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.operator_tokens import OperatorTokenError
from daimon.core.panel_audit import PanelOp, record_panel_write
from daimon.core.panel_operator_tokens import (
    mint_panel_operator_token,
    revoke_panel_operator_token,
)
from slack_sdk.web.async_client import AsyncWebClient

log = structlog.get_logger()

NOT_CONFIGURED_MESSAGE: Final = "Operator tokens need the MCP server's signing key."


@dataclasses.dataclass(frozen=True)
class OperatorTokenSubmission:
    meta: PanelMetadata
    scopes: tuple[str, ...]
    label: str


def _input(values: dict[str, Any], block_id: str) -> dict[str, Any]:
    block: dict[str, Any] = values.get(block_id) or {}
    element: dict[str, Any] = block.get(block_id) or {}
    return element


def evaluate_operator_token_submission(payload: dict[str, Any]) -> OperatorTokenSubmission | None:
    """The form's panel metadata, picked scopes and label, or None when unreadable. Pure."""
    view: dict[str, Any] = payload.get("view") or {}
    meta = decode_panel_metadata(str(view.get("private_metadata") or ""))
    if meta is None:
        return None
    state: dict[str, Any] = view.get("state") or {}
    values: dict[str, Any] = state.get("values") or {}
    scopes = _input(values, OPERATOR_SCOPES_INPUT_ID)
    label = _input(values, OPERATOR_LABEL_INPUT_ID)
    options: list[dict[str, Any]] = scopes.get("selected_options") or []
    picked = tuple(str(option.get("value") or "") for option in options)
    return OperatorTokenSubmission(meta=meta, scopes=picked, label=str(label.get("value") or ""))


def _audit(
    runtime: SlackRuntime, *, team_id: str, user_id: str, op: PanelOp
) -> functools.partial[Any]:
    return functools.partial(
        record_panel_write,
        runtime.sessionmaker,
        tenant_id=derive_tenant_uuid(platform="slack", workspace_id=team_id),
        platform="slack",
        platform_user_id=user_id,
        op=op,
        token_kind="operator",
    )


async def _refresh(
    runtime: SlackRuntime, client: AsyncWebClient, *, meta: PanelMetadata, view_id: str | None
) -> None:
    if view_id:
        await client.views_update(  # pyright: ignore[reportUnknownMemberType]
            view_id=view_id,
            view=await load_routing_view(
                runtime,
                client,
                tenant_id=derive_tenant_uuid(platform="slack", workspace_id=meta.team_id),
                meta=meta.with_view("routing"),
                is_admin=True,
            ),
        )


async def run_operator_token_submission(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    team_id: str,
    user_id: str,
    submission: OperatorTokenSubmission,
) -> None:
    """Mint the token, post it once to the clicker, then refresh Who answers where."""
    meta = submission.meta
    audit = _audit(runtime, team_id=team_id, user_id=user_id, op="operator_token_mint")
    reply = functools.partial(
        post_ephemeral, client, channel_id=meta.channel_id or user_id, user_id=user_id
    )
    if not await resolve_is_admin(client, user_id=user_id):
        await audit(outcome="denied", reason="needs_admin")
        await reply(text=OPERATOR_TOKENS_NEED_ADMIN_MESSAGE)
        return
    secret = runtime.settings.mcp.jwt_secret
    if secret is None:
        await reply(text=NOT_CONFIGURED_MESSAGE)
        return
    try:
        async with runtime.sessionmaker.begin() as session:
            minted = await mint_panel_operator_token(
                session,
                tenant_id=derive_tenant_uuid(platform="slack", workspace_id=team_id),
                platform="slack",
                platform_user_id=user_id,
                scopes=submission.scopes,
                label=submission.label,
                secret=secret.get_secret_value().encode(),
                now=dt.datetime.now(dt.UTC),
            )
    except OperatorTokenError as exc:
        await audit(outcome="denied", reason="scopes")
        await reply(text=f"{exc}. Nothing was minted.")
        return
    await audit(outcome="allowed", reason="completed", token_jti=minted.jti)
    log.info("slack.agent_setup.operator_token.minted", jti=str(minted.jti))  # never the token
    await reply(
        text=(
            f"```{minted.token}```\nScopes: {', '.join(sorted(minted.scopes))}. Expires "
            f"{minted.expires_at.date().isoformat()}. This is the one time it is shown."
        )
    )
    await _refresh(runtime, client, meta=meta, view_id=meta.root_view_id)


async def handle_operator_token_revoke(
    runtime: SlackRuntime,
    client: AsyncWebClient,
    *,
    meta: PanelMetadata,
    user_id: str,
    is_admin: bool,
    jti: uuid.UUID,
    view_id: str,
) -> None:
    """Revoke the picked token for a live workspace admin, and say so."""
    audit = _audit(runtime, team_id=meta.team_id, user_id=user_id, op="operator_token_revoke")
    reply = functools.partial(
        post_ephemeral, client, channel_id=meta.channel_id or user_id, user_id=user_id
    )
    if not is_admin:
        await audit(outcome="denied", reason="needs_admin", token_jti=jti)
        await reply(text=OPERATOR_TOKENS_NEED_ADMIN_MESSAGE)
        return
    async with runtime.sessionmaker.begin() as session:
        revoked = await revoke_panel_operator_token(
            session,
            tenant_id=derive_tenant_uuid(platform="slack", workspace_id=meta.team_id),
            jti=jti,
            now=dt.datetime.now(dt.UTC),
        )
    if revoked:
        await audit(outcome="allowed", reason="completed", token_jti=jti)
    else:
        await audit(outcome="error", reason="already_revoked", token_jti=jti)
    log.info("slack.agent_setup.operator_token.revoked", jti=str(jti), revoked=revoked)
    await reply(text="Token revoked." if revoked else "That token was already revoked.")
    await _refresh(runtime, client, meta=meta, view_id=view_id)
