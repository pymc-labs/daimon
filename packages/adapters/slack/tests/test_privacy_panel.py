"""Tests for privacy_panel/read.py and privacy_panel/views.py.

Covers:
- resolve_privacy_account: returns account_id for existing slack principal, None on miss (no create)
- load_purge_preview: thin wrapper that returns a PurgePreview
- build_privacy_main_container: renders held-data summary + Policy url button per category
- build_delete_modal: single modal with callback_id, plain_text_input, parseable private_metadata
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from daimon.adapters.slack.privacy_panel.read import load_purge_preview, resolve_privacy_account
from daimon.adapters.slack.privacy_panel.views import (
    build_delete_modal,
    build_privacy_main_container,
)
from daimon.core._models import PlatformPrincipal
from daimon.core.stores import routines as routines_store
from daimon.testing.factories import make_account, make_platform_principal, make_tenant
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_POLICY_URL = "https://daimon.dev/privacy"


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

    count_before = (
        await db_session.execute(
            select(func.count()).select_from(PlatformPrincipal)  # type: ignore[arg-type]
        )
    ).scalar_one()

    async with db_session_factory() as s:
        result = await resolve_privacy_account(
            s,
            tenant_id=tenant.id,
            platform_user_id="U_UNKNOWN_PRIV",
        )

    assert result is None, "resolve_privacy_account should return None for an unknown user"

    count_after = (
        await db_session.execute(
            select(func.count()).select_from(PlatformPrincipal)  # type: ignore[arg-type]
        )
    ).scalar_one()
    assert count_after == count_before, (
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
