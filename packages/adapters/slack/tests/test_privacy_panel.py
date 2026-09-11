"""Tests for privacy_panel/read.py and privacy_panel/views.py.

Covers:
- resolve_privacy_account: returns account_id for existing slack principal, None on miss (no create)
- load_purge_preview: thin wrapper that returns a PurgePreview
- build_privacy_main_container: renders held-data summary + Policy url button per category
- build_delete_modal: single modal with callback_id, plain_text_input, parseable private_metadata
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from daimon.adapters.slack.privacy_panel.read import load_purge_preview, resolve_privacy_account
from daimon.adapters.slack.privacy_panel.views import (
    build_delete_modal,
    build_privacy_main_container,
    summary_line,
)
from daimon.core.privacy import PurgePreview, PurgePreviewRow
from daimon.core.stores import routines as routines_store
from daimon.core.stores.identity import find_platform_principal
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_POLICY_URL = "https://github.com/pymc-labs/daimon/blob/main/PRIVACY.md"


@pytest.mark.asyncio
async def test_resolve_privacy_account_returns_account_id_for_known_slack_user(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """resolve_privacy_account returns account_id when a slack principal exists."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_PRIV_R01")
    account = await make_account(db_session, tenant=tenant)
    await make_platform_principal(
        db_session,
        platform="slack",
        external_id="U_PRIV_R01",
        tenant=tenant,
        account=account,
    )
    await db_session.commit()

    async with db_session_factory() as s:
        result = await resolve_privacy_account(
            s,
            tenant_id=tenant.id,
            platform_user_id="U_PRIV_R01",
        )

    assert result == account.id, (
        "resolve_privacy_account should return the account_id for a known slack user"
    )


@pytest.mark.asyncio
async def test_resolve_privacy_account_returns_none_and_creates_no_principal_on_miss(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """resolve_privacy_account returns None without creating a principal on miss (read-only)."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_PRIV_R02")
    await db_session.commit()

    async def _lookup() -> object | None:
        return await find_platform_principal(
            db_session, tenant_id=tenant.id, platform="slack", external_id="U_UNKNOWN_PRIV"
        )

    principal_before = await _lookup()

    async with db_session_factory() as s:
        result = await resolve_privacy_account(
            s,
            tenant_id=tenant.id,
            platform_user_id="U_UNKNOWN_PRIV",
        )

    assert result is None, "resolve_privacy_account should return None for an unknown user"

    principal_after = await _lookup()
    assert principal_after is None and principal_before is None, (
        "resolve_privacy_account must NOT create a new principal row on miss"
    )


@pytest.mark.asyncio
async def test_build_privacy_main_container_renders_summary_and_policy_button(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """build_privacy_main_container includes held-data summary per category + Policy url button."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_PRIV_V01")
    account = await make_account(db_session, tenant=tenant)
    pp = await make_platform_principal(
        db_session,
        platform="slack",
        external_id="U_PRIV_V01",
        tenant=tenant,
        account=account,
    )
    # Seed a routine so the routines category is non-zero in the preview.
    await routines_store.create_routine(
        db_session,
        tenant_id=tenant.id,
        created_by_user_id=pp.external_id,
        agent_id="ag_priv_test",
        agent_name="TestAgent",
        cron_expr="0 9 * * 1",
        timezone_="UTC",
        trigger_message="run weekly",
    )
    await db_session.commit()

    preview = await load_purge_preview(sm=db_session_factory, account_id=account.id)
    view = build_privacy_main_container(
        preview, is_slack_connected=True, slack_connect_url=None, policy_url=_POLICY_URL
    )

    # Extract all mrkdwn/plain_text content from blocks.
    all_text = _extract_text(view).lower()

    assert "linked principal" in all_text, "summary should mention the seeded linked principal"
    assert "routine" in all_text, "summary should mention the seeded routine"

    assert _has_url_button(view, _POLICY_URL), (
        f"view should contain a url button linking to the policy URL ({_POLICY_URL})"
    )


@pytest.mark.asyncio
async def test_build_privacy_main_container_uses_operator_overridden_policy_url(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The Policy button renders whatever policy_url the caller passes in."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_PRIV_URL_OVERRIDE")
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()

    overridden_url = "https://example.com/privacy"
    preview = await load_purge_preview(sm=db_session_factory, account_id=account.id)
    view = build_privacy_main_container(
        preview, is_slack_connected=True, slack_connect_url=None, policy_url=overridden_url
    )

    assert _has_url_button(view, overridden_url), (
        f"view should contain a url button linking to the overridden policy URL ({overridden_url})"
    )


@pytest.mark.asyncio
async def test_privacy_main_view_has_disconnect_button(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A connected caller's panel includes a Disconnect Slack action button."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_PRIV_DISCONNECT")
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()

    preview = await load_purge_preview(sm=db_session_factory, account_id=account.id)
    view = build_privacy_main_container(
        preview, is_slack_connected=True, slack_connect_url=None, policy_url=_POLICY_URL
    )

    action_ids = [
        el["action_id"]
        for block in view["blocks"]
        if block["type"] == "actions"
        for el in block["elements"]
    ]
    assert "privacy_slack_disconnect" in action_ids, "connected panel must offer Disconnect Slack"
    assert "privacy_slack_connect" not in action_ids, (
        "connected panel must not also offer Connect Slack"
    )


@pytest.mark.asyncio
async def test_privacy_main_view_offers_connect_when_not_connected(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A caller with no user token gets a Connect Slack url button instead of
    Disconnect — otherwise the panel offers no way back after disconnecting."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_PRIV_CONNECT")
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()

    connect_url = "https://mcp.example.com/oauth/slack/connect?state=SIGNED"
    preview = await load_purge_preview(sm=db_session_factory, account_id=account.id)
    view = build_privacy_main_container(
        preview,
        is_slack_connected=False,
        slack_connect_url=connect_url,
        policy_url=_POLICY_URL,
    )

    buttons = [
        el for block in view["blocks"] if block["type"] == "actions" for el in block["elements"]
    ]
    action_ids = [el["action_id"] for el in buttons]
    assert "privacy_slack_connect" in action_ids, "disconnected panel must offer Connect Slack"
    assert "privacy_slack_disconnect" not in action_ids, (
        "disconnected panel must not offer Disconnect Slack"
    )
    assert _has_url_button(view, connect_url), (
        "Connect Slack must be a url button pointing at the signed connect link"
    )


@pytest.mark.asyncio
async def test_privacy_main_view_omits_slack_button_when_unmintable(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Not connected and no mintable connect URL (no slack/mcp config): the
    panel shows neither slack-token button rather than a dead one."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_PRIV_NOBTN")
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()

    preview = await load_purge_preview(sm=db_session_factory, account_id=account.id)
    view = build_privacy_main_container(
        preview, is_slack_connected=False, slack_connect_url=None, policy_url=_POLICY_URL
    )

    action_ids = [
        el["action_id"]
        for block in view["blocks"]
        if block["type"] == "actions"
        for el in block["elements"]
    ]
    assert "privacy_slack_connect" not in action_ids, "no connect button without a mintable URL"
    assert "privacy_slack_disconnect" not in action_ids, (
        "no disconnect button when nothing is connected"
    )


@pytest.mark.asyncio
async def test_build_delete_modal_has_correct_callback_id_input_and_metadata(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """build_delete_modal returns a view with callback_id, plain_text_input, parseable metadata."""
    tenant = await make_tenant(db_session, platform="slack", workspace_id="T_PRIV_V02")
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()

    preview = await load_purge_preview(sm=db_session_factory, account_id=account.id)
    view = build_delete_modal(
        preview,
        account_id=account.id,
        user_name="alice",
        view_id="V_MAIN_PRIV_01",
    )

    assert view.get("callback_id") == "privacy_delete", (
        "delete modal must have callback_id 'privacy_delete'"
    )

    # Input block with block_id "confirm_name_block".
    input_block = next(
        (b for b in view.get("blocks", []) if b.get("block_id") == "confirm_name_block"),
        None,
    )
    assert input_block is not None, (
        "view must contain an input block with block_id 'confirm_name_block'"
    )
    element: dict[str, Any] = input_block.get("element") or {}
    assert element.get("type") == "plain_text_input", (
        "input element type must be 'plain_text_input'"
    )
    assert element.get("action_id") == "confirm_name", (
        "input element action_id must be 'confirm_name'"
    )

    # private_metadata must be valid JSON carrying account_id, user_name, view_id.
    raw_meta: str = view.get("private_metadata") or ""
    assert raw_meta, "private_metadata must not be empty"
    assert len(raw_meta) <= 3000, "private_metadata must not exceed 3000 chars (Slack Pitfall 6)"
    meta: dict[str, Any] = json.loads(raw_meta)
    assert "account_id" in meta, "private_metadata must contain account_id"
    assert "user_name" in meta, "private_metadata must contain user_name"
    assert "view_id" in meta, "private_metadata must contain view_id"
    assert meta["user_name"] == "alice", "private_metadata user_name must match the argument"
    assert meta["view_id"] == "V_MAIN_PRIV_01", "private_metadata view_id must match the argument"


def _make_preview(**overrides: Any) -> PurgePreview:
    """Build a PurgePreview with sensible defaults; override any field via kwargs."""
    base: dict[str, Any] = {
        "linked_principals": PurgePreviewRow(count=0, example=None),
        "principal_links": PurgePreviewRow(count=0, example=None),
        "routines": PurgePreviewRow(count=0, example=None),
        "user_configs": PurgePreviewRow(count=0, example=None),
        "account": PurgePreviewRow(count=1, example=None),
        "user_skills": PurgePreviewRow(count=0, example=None),
        "github_credentials": PurgePreviewRow(count=0, example=None),
        "github_oauth_states": PurgePreviewRow(count=0, example=None),
        "mcp_tokens": PurgePreviewRow(count=0, example=None),
        "agent_github_binding": PurgePreviewRow(count=0, example=None),
        "slack_user_tokens": PurgePreviewRow(count=0, example=None),
        "slack_turn_contexts": PurgePreviewRow(count=0, example=None),
        "credential_requests": PurgePreviewRow(count=0, example=None),
        "wizard_sessions": PurgePreviewRow(count=0, example=None),
        "message_feedback": PurgePreviewRow(count=0, example=None),
        "support_escalations": PurgePreviewRow(count=0, example=None),
    }
    base.update(overrides)
    return PurgePreview(**base)


def test_summary_line_includes_mcp_tokens_agent_github_binding_slack_categories() -> None:
    """summary_line reflects all 4 newly-added categories when non-zero."""
    preview = _make_preview(
        mcp_tokens=PurgePreviewRow(count=2, example=None),
        agent_github_binding=PurgePreviewRow(count=1, example=None),
        slack_user_tokens=PurgePreviewRow(count=1, example=None),
        slack_turn_contexts=PurgePreviewRow(count=4, example=None),
    )
    text = summary_line(preview)
    assert "MCP token" in text, "summary must mention mcp_tokens"
    assert "GitHub link" in text, "summary must mention agent_github_binding"
    assert "Slack user token" in text, "summary must mention slack_user_tokens"
    assert "Slack turn context" in text, "summary must mention slack_turn_contexts"


def test_cascade_blocks_render_mcp_tokens_row_when_nonzero() -> None:
    """mcp_tokens row appears in the cascade preview when count > 0."""
    preview = _make_preview(mcp_tokens=PurgePreviewRow(count=3, example=None))
    view = build_delete_modal(preview, account_id=uuid.uuid4(), user_name="alice", view_id="V1")
    joined = _extract_text(view)
    assert "3" in joined, "mcp_tokens count must appear in the cascade"
    assert "MCP token" in joined, "mcp_tokens row must mention MCP token(s)"


def test_cascade_blocks_render_agent_github_binding_row_when_nonzero() -> None:
    """agent_github_binding row appears in the cascade preview when count > 0."""
    preview = _make_preview(agent_github_binding=PurgePreviewRow(count=2, example=None))
    view = build_delete_modal(preview, account_id=uuid.uuid4(), user_name="alice", view_id="V2")
    joined = _extract_text(view)
    assert "2" in joined, "agent_github_binding count must appear in the cascade"
    assert "per-agent GitHub token link" in joined, (
        "agent_github_binding row must mention the per-agent GitHub token link"
    )


def test_cascade_blocks_render_slack_user_tokens_row_when_nonzero() -> None:
    """slack_user_tokens row appears in the cascade preview when count > 0."""
    preview = _make_preview(slack_user_tokens=PurgePreviewRow(count=1, example=None))
    view = build_delete_modal(preview, account_id=uuid.uuid4(), user_name="alice", view_id="V3")
    joined = _extract_text(view)
    assert "1" in joined, "slack_user_tokens count must appear in the cascade"
    assert "Slack user token" in joined, "slack_user_tokens row must mention Slack user token(s)"


def test_cascade_blocks_render_slack_turn_contexts_row_when_nonzero() -> None:
    """slack_turn_contexts row appears in the cascade preview when count > 0."""
    preview = _make_preview(slack_turn_contexts=PurgePreviewRow(count=5, example=None))
    view = build_delete_modal(preview, account_id=uuid.uuid4(), user_name="alice", view_id="V4")
    joined = _extract_text(view)
    assert "5" in joined, "slack_turn_contexts count must appear in the cascade"
    assert "Slack turn context" in joined, (
        "slack_turn_contexts row must mention Slack turn context(s)"
    )


def test_cascade_blocks_zero_count_categories_omit_new_rows() -> None:
    """D-PREVIEW-FMT-01 parity: zero-count new categories must NOT render a row."""
    preview = _make_preview()
    view = build_delete_modal(preview, account_id=uuid.uuid4(), user_name="alice", view_id="V5")
    joined = _extract_text(view).lower()
    assert "mcp token(s)" not in joined, "zero-count mcp_tokens must NOT render a row"
    assert "per-agent github token link" not in joined, (
        "zero-count agent_github_binding must NOT render a row"
    )
    assert "slack user token" not in joined, "zero-count slack_user_tokens must NOT render a row"
    assert "slack turn context" not in joined, (
        "zero-count slack_turn_contexts must NOT render a row"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_text(view: dict[str, Any]) -> str:
    """Recursively extract all text strings from a view dict."""
    parts: list[str] = []
    _collect_text(view, parts)
    return " ".join(parts)


def _collect_text(obj: Any, parts: list[str]) -> None:  # noqa: ANN401 — test helper
    if isinstance(obj, dict):
        text_type = obj.get("type")
        if text_type in ("mrkdwn", "plain_text"):
            t = obj.get("text")
            if t:
                parts.append(str(t))
        for v in obj.values():
            _collect_text(v, parts)
    elif isinstance(obj, list):
        for item in obj:
            _collect_text(item, parts)


def _has_url_button(view: dict[str, Any], url: str) -> bool:
    """Return True if any button element in the view carries the given url."""
    return _find_url_button(view, url)


def _find_url_button(obj: Any, url: str) -> bool:  # noqa: ANN401 — test helper
    if isinstance(obj, dict):
        if obj.get("type") == "button" and obj.get("url") == url:
            return True
        return any(_find_url_button(v, url) for v in obj.values())
    if isinstance(obj, list):
        return any(_find_url_button(item, url) for item in obj)
    return False


def test_privacy_views_use_the_configured_display_name() -> None:
    from daimon.adapters.slack.privacy_panel.views import (
        build_disconnect_result_view,
        build_export_result_view,
    )

    views = [
        build_privacy_main_container(
            _make_preview(),
            is_slack_connected=False,
            slack_connect_url=None,
            policy_url=_POLICY_URL,
            display_name="research-bot",
        ),
        build_export_result_view(summary=None, display_name="research-bot"),
        build_export_result_view(summary="one session", display_name="research-bot"),
        build_disconnect_result_view(
            was_connected=True, reconnect_url=None, display_name="research-bot"
        ),
    ]
    for view in views:
        assert "research-bot" in _extract_text(view), "privacy copy must use the configured name"
