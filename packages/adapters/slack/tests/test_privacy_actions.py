"""Tests for privacy_panel/actions.py — the "Disconnect Slack" block action.

Covers handle_privacy_block_action's privacy_slack_disconnect branch (D-05
ordering: delete the slack_user_tokens row first, then best-effort revoke,
then views.update either way):

- Connected user: row deleted, auth.revoke attempted.
- Connected user whose auth.revoke fails: row still deleted, views.update
  still called (best-effort revoke never blocks the delete).
- No stored token: views.update reports nothing was connected.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import yarl
from cryptography.fernet import Fernet
from daimon.adapters.slack.privacy_panel.actions import (
    handle_privacy_block_action,
    handle_privacy_command,
)
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.defaults.provisioning import provision_tenant
from daimon.core.github_credentials import build_multifernet, encrypt_token
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.stores.slack_bot_tokens import upsert_slack_bot_token
from daimon.core.stores.slack_user_tokens import get_slack_user_token, upsert_slack_user_token
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_AUTH_REVOKE_PATTERN = re.compile(r"https://slack\.com/api/auth\.revoke.*")


def _build_runtime(
    fernet_key: str, db_factory: async_sessionmaker[AsyncSession], *, mintable: bool = False
) -> SlackRuntime:
    settings = MagicMock()
    settings.crypto.keys = (SecretStr(fernet_key),)
    if mintable:
        settings.slack.signing_secret = SecretStr("shh-secret")
        settings.mcp.app_root_url = "https://mcp.example.com"
    else:
        settings.slack = None
        settings.mcp.app_root_url = None
    return SlackRuntime(
        settings=settings,
        anthropic=MagicMock(),
        sessionmaker=db_factory,
        http_client=MagicMock(spec=httpx.AsyncClient),
    )


def _disconnect_payload(*, team_id: str, user_id: str, view_id: str) -> dict[str, Any]:
    return {
        "team": {"id": team_id},
        "user": {"id": user_id, "username": "tester"},
        "trigger_id": "TRIGGER_TEST",
        "view": {"id": view_id},
        "actions": [{"action_id": "privacy_slack_disconnect"}],
    }


async def _seed_tenant_principal_and_bot_token(
    db_factory: async_sessionmaker[AsyncSession], *, team_id: str, user_id: str, fernet_key: str
) -> None:
    fernet = build_multifernet((fernet_key,))
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
    await provision_tenant(db_factory, platform="slack", workspace_id=team_id)
    async with db_factory() as s:
        await upsert_slack_bot_token(
            s, team_id=team_id, encrypted_token=encrypt_token(fernet, "xoxb-test")
        )
        await get_or_create_platform_principal(
            s, tenant_id=tenant_id, platform="slack", external_id=user_id
        )
        await s.commit()


def _modal_action_payload(*, team_id: str, user_id: str, action_id: str) -> dict[str, Any]:
    """block_actions payload as sent from inside a modal — no top-level channel."""
    return {
        "team": {"id": team_id},
        "user": {"id": user_id, "username": "tester"},
        "trigger_id": "TRIGGER_TEST",
        "view": {"id": "V_PRIVACY_MAIN"},
        "actions": [{"action_id": action_id}],
    }


async def test_privacy_command_offers_connect_button_when_not_connected(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """/privacy for a caller with no user token must render a Connect Slack
    url button (signed connect link) — the panel is the way back after a
    disconnect, not just JIT denial hints."""
    team_id = "T_CONNECT_BTN_01"
    user_id = "U_CONNECT_BTN_01"
    fernet_key = Fernet.generate_key().decode()
    await _seed_tenant_principal_and_bot_token(
        db_session_factory, team_id=team_id, user_id=user_id, fernet_key=fernet_key
    )

    runtime = _build_runtime(fernet_key, db_session_factory, mintable=True)
    payload: dict[str, Any] = {
        "team_id": team_id,
        "user_id": user_id,
        "trigger_id": "TRIGGER_TEST",
    }

    await handle_privacy_command(runtime, payload)

    update_key = ("POST", yarl.URL("https://slack.com/api/views.update"))
    update_calls = fake_slack_web_client.mock.requests.get(update_key)
    assert update_calls, "/privacy must update the loading modal with the main view"
    body: dict[str, Any] = update_calls[-1].kwargs["json"]
    buttons = [
        el
        for block in body["view"]["blocks"]
        if block["type"] == "actions"
        for el in block["elements"]
    ]
    connect_buttons = [el for el in buttons if el["action_id"] == "privacy_slack_connect"]
    assert connect_buttons, "disconnected caller must get a Connect Slack button"
    assert "/oauth/slack/connect?state=" in str(connect_buttons[0]["url"]), (
        "Connect Slack must link to the signed connect URL"
    )
    assert not any(el["action_id"] == "privacy_slack_disconnect" for el in buttons), (
        "disconnected caller must not get a Disconnect button"
    )


async def test_disconnect_result_offers_reconnect_link_when_mintable(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """The post-disconnect view must offer the signed reconnect link so the
    user isn't stranded until they next hit an unreadable channel."""
    team_id = "T_RECONNECT_01"
    user_id = "U_RECONNECT_01"
    fernet_key = Fernet.generate_key().decode()
    fernet = build_multifernet((fernet_key,))
    async with db_session_factory() as s, s.begin():
        await upsert_slack_bot_token(
            s, team_id=team_id, encrypted_token=encrypt_token(fernet, "xoxb-test")
        )
        await upsert_slack_user_token(
            s,
            team_id=team_id,
            slack_user_id=user_id,
            encrypted_token=encrypt_token(fernet, "xoxp-user-test"),
            scopes="channels:history",
        )
    fake_slack_web_client.mock.get(  # pyright: ignore[reportUnknownMemberType]
        _AUTH_REVOKE_PATTERN,
        payload={"ok": True, "revoked": True},
    )

    runtime = _build_runtime(fernet_key, db_session_factory, mintable=True)
    payload = _disconnect_payload(team_id=team_id, user_id=user_id, view_id="V_RECONNECT_01")

    await handle_privacy_block_action(runtime, payload)

    update_key = ("POST", yarl.URL("https://slack.com/api/views.update"))
    update_calls = fake_slack_web_client.mock.requests.get(update_key)
    assert update_calls, "disconnect must update the modal with the result view"
    body: dict[str, Any] = update_calls[-1].kwargs["json"]
    buttons = [
        el
        for block in body["view"]["blocks"]
        if block["type"] == "actions"
        for el in block["elements"]
    ]
    reconnect = [el for el in buttons if el["action_id"] == "privacy_slack_connect"]
    assert reconnect, "post-disconnect view must offer a Reconnect Slack button"
    assert "/oauth/slack/connect?state=" in str(reconnect[0]["url"]), (
        "reconnect button must link to the signed connect URL"
    )


async def test_delete_open_pushes_confirm_modal_onto_stack(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """Delete… is clicked inside the open privacy modal, so the confirm modal
    must be stacked with views.push — Slack answers ok to views.open from a
    modal trigger but renders nothing (observed on staging)."""
    team_id = "T_DELETE_OPEN_01"
    user_id = "U_DELETE_OPEN_01"
    fernet_key = Fernet.generate_key().decode()
    await _seed_tenant_principal_and_bot_token(
        db_session_factory, team_id=team_id, user_id=user_id, fernet_key=fernet_key
    )

    runtime = _build_runtime(fernet_key, db_session_factory)
    payload = _modal_action_payload(
        team_id=team_id, user_id=user_id, action_id="privacy_delete_open"
    )

    await handle_privacy_block_action(runtime, payload)

    push_key = ("POST", yarl.URL("https://slack.com/api/views.push"))
    open_key = ("POST", yarl.URL("https://slack.com/api/views.open"))
    push_calls = fake_slack_web_client.mock.requests.get(push_key)
    assert push_calls, "confirm modal must be stacked via views.push"
    assert open_key not in fake_slack_web_client.mock.requests, (
        "views.open from a modal trigger silently renders nothing — must not be used"
    )
    body: dict[str, Any] = push_calls[0].kwargs["json"]
    assert body["view"]["callback_id"] == "privacy_delete", (
        "pushed view must be the delete confirmation modal"
    )


async def test_export_pushes_summary_modal_not_ephemeral(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """Export is clicked inside the privacy modal, which carries no channel —
    the summary must be pushed as a modal, not chat.postEphemeral into a
    channel id the payload doesn't have."""
    team_id = "T_EXPORT_01"
    user_id = "U_EXPORT_01"
    fernet_key = Fernet.generate_key().decode()
    await _seed_tenant_principal_and_bot_token(
        db_session_factory, team_id=team_id, user_id=user_id, fernet_key=fernet_key
    )

    runtime = _build_runtime(fernet_key, db_session_factory)
    payload = _modal_action_payload(team_id=team_id, user_id=user_id, action_id="privacy_export")

    await handle_privacy_block_action(runtime, payload)

    push_key = ("POST", yarl.URL("https://slack.com/api/views.push"))
    ephemeral_key = ("POST", yarl.URL("https://slack.com/api/chat.postEphemeral"))
    push_calls = fake_slack_web_client.mock.requests.get(push_key)
    assert push_calls, "export summary must be pushed as a modal"
    assert ephemeral_key not in fake_slack_web_client.mock.requests, (
        "modal payloads have no channel — chat.postEphemeral cannot work here"
    )
    body: dict[str, Any] = push_calls[0].kwargs["json"]
    view_text = str(body["view"]["blocks"][0]["text"]["text"])
    assert "Privacy export" in view_text, "pushed modal must carry the export summary"


async def test_export_with_no_account_pushes_no_data_modal(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A caller with no daimon account still gets a pushed result modal."""
    team_id = "T_EXPORT_02"
    user_id = "U_EXPORT_02"
    fernet_key = Fernet.generate_key().decode()
    fernet = build_multifernet((fernet_key,))
    async with db_session_factory() as s, s.begin():
        await upsert_slack_bot_token(
            s, team_id=team_id, encrypted_token=encrypt_token(fernet, "xoxb-test")
        )
        # Intentionally no principal/account.

    runtime = _build_runtime(fernet_key, db_session_factory)
    payload = _modal_action_payload(team_id=team_id, user_id=user_id, action_id="privacy_export")

    await handle_privacy_block_action(runtime, payload)

    push_key = ("POST", yarl.URL("https://slack.com/api/views.push"))
    push_calls = fake_slack_web_client.mock.requests.get(push_key)
    assert push_calls, "no-account export must still push a result modal"
    body: dict[str, Any] = push_calls[0].kwargs["json"]
    view_text = str(body["view"]["blocks"][0]["text"]["text"])
    assert "no data" in view_text, "no-account export must say there is nothing on file"


async def test_disconnect_deletes_row_and_revokes(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A connected user's token row is deleted and auth.revoke is attempted."""
    team_id = "T_DISCONNECT_01"
    user_id = "U_DISCONNECT_01"
    fernet_key = Fernet.generate_key().decode()
    fernet = build_multifernet((fernet_key,))

    async with db_session_factory() as s, s.begin():
        await upsert_slack_bot_token(
            s, team_id=team_id, encrypted_token=encrypt_token(fernet, "xoxb-test")
        )
        await upsert_slack_user_token(
            s,
            team_id=team_id,
            slack_user_id=user_id,
            encrypted_token=encrypt_token(fernet, "xoxp-user-test"),
            scopes="channels:history",
        )

    fake_slack_web_client.mock.get(  # pyright: ignore[reportUnknownMemberType]
        _AUTH_REVOKE_PATTERN,
        payload={"ok": True, "revoked": True},
    )

    runtime = _build_runtime(fernet_key, db_session_factory)
    payload = _disconnect_payload(team_id=team_id, user_id=user_id, view_id="V_DISCONNECT_01")

    await handle_privacy_block_action(runtime, payload)

    async with db_session_factory() as s:
        assert await get_slack_user_token(s, team_id=team_id, slack_user_id=user_id) is None, (
            "disconnect must delete the stored token row"
        )

    revoke_calls = [
        req
        for (method, url), reqs in fake_slack_web_client.mock.requests.items()
        if method == "GET" and url.path == "/api/auth.revoke"
        for req in reqs
    ]
    assert len(revoke_calls) == 1, "revoke is attempted exactly once"

    views_update_key = ("POST", yarl.URL("https://slack.com/api/views.update"))
    assert views_update_key in fake_slack_web_client.mock.requests, (
        "disconnect must update the modal view with the result"
    )


async def test_disconnect_deletes_row_even_when_revoke_fails(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """A failing auth.revoke must not block the row delete or the views.update."""
    team_id = "T_DISCONNECT_02"
    user_id = "U_DISCONNECT_02"
    fernet_key = Fernet.generate_key().decode()
    fernet = build_multifernet((fernet_key,))

    async with db_session_factory() as s, s.begin():
        await upsert_slack_bot_token(
            s, team_id=team_id, encrypted_token=encrypt_token(fernet, "xoxb-test")
        )
        await upsert_slack_user_token(
            s,
            team_id=team_id,
            slack_user_id=user_id,
            encrypted_token=encrypt_token(fernet, "xoxp-user-test"),
            scopes="channels:history",
        )

    fake_slack_web_client.mock.get(  # pyright: ignore[reportUnknownMemberType]
        _AUTH_REVOKE_PATTERN,
        payload={"ok": False, "error": "invalid_auth"},
    )

    runtime = _build_runtime(fernet_key, db_session_factory)
    payload = _disconnect_payload(team_id=team_id, user_id=user_id, view_id="V_DISCONNECT_02")

    await handle_privacy_block_action(runtime, payload)

    async with db_session_factory() as s:
        assert await get_slack_user_token(s, team_id=team_id, slack_user_id=user_id) is None, (
            "row must be deleted even when the best-effort revoke fails"
        )

    views_update_key = ("POST", yarl.URL("https://slack.com/api/views.update"))
    assert views_update_key in fake_slack_web_client.mock.requests, (
        "views.update must still be called when revoke fails"
    )


async def test_disconnect_fernet_build_failure_still_updates_view(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """build_multifernet can raise ValueError (e.g. a misconfigured empty key
    set) for the best-effort revoke's own fernet build — that must be caught
    by the same suppress as a failed auth.revoke, not escape and skip the
    delete's views.update."""
    team_id = "T_DISCONNECT_04"
    user_id = "U_DISCONNECT_04"
    fernet_key = Fernet.generate_key().decode()
    fernet = build_multifernet((fernet_key,))

    async with db_session_factory() as s, s.begin():
        await upsert_slack_bot_token(
            s, team_id=team_id, encrypted_token=encrypt_token(fernet, "xoxb-test")
        )
        await upsert_slack_user_token(
            s,
            team_id=team_id,
            slack_user_id=user_id,
            encrypted_token=encrypt_token(fernet, "xoxp-user-test"),
            scopes="channels:history",
        )

    runtime = _build_runtime(fernet_key, db_session_factory)
    payload = _disconnect_payload(team_id=team_id, user_id=user_id, view_id="V_DISCONNECT_04")

    with patch(
        "daimon.adapters.slack.privacy_panel.actions.build_multifernet",
        side_effect=ValueError("settings.crypto.keys is empty"),
    ):
        await handle_privacy_block_action(runtime, payload)

    async with db_session_factory() as s:
        assert await get_slack_user_token(s, team_id=team_id, slack_user_id=user_id) is None, (
            "row must be deleted even when the fernet build for revoke fails"
        )

    revoke_calls = [
        req
        for (method, url), reqs in fake_slack_web_client.mock.requests.items()
        if method == "GET" and url.path == "/api/auth.revoke"
        for req in reqs
    ]
    assert revoke_calls == [], "revoke is never attempted when the fernet build fails first"

    views_update_key = ("POST", yarl.URL("https://slack.com/api/views.update"))
    assert views_update_key in fake_slack_web_client.mock.requests, (
        "views.update must still be called when the fernet build raises ValueError"
    )


async def test_disconnect_with_no_row_reports_nothing_connected(
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
    fake_slack_web_client: Any,
) -> None:
    """With no stored token, views.update reports nothing was connected and no
    revoke is attempted."""
    team_id = "T_DISCONNECT_03"
    user_id = "U_DISCONNECT_03"
    fernet_key = Fernet.generate_key().decode()
    fernet = build_multifernet((fernet_key,))

    async with db_session_factory() as s, s.begin():
        await upsert_slack_bot_token(
            s, team_id=team_id, encrypted_token=encrypt_token(fernet, "xoxb-test")
        )
        # Intentionally no slack_user_tokens row.

    runtime = _build_runtime(fernet_key, db_session_factory)
    payload = _disconnect_payload(team_id=team_id, user_id=user_id, view_id="V_DISCONNECT_03")

    await handle_privacy_block_action(runtime, payload)

    revoke_calls = [
        req
        for (method, url), reqs in fake_slack_web_client.mock.requests.items()
        if method == "GET" and url.path == "/api/auth.revoke"
        for req in reqs
    ]
    assert revoke_calls == [], "no token means no revoke attempt"

    views_update_key = ("POST", yarl.URL("https://slack.com/api/views.update"))
    update_calls = fake_slack_web_client.mock.requests.get(views_update_key)
    assert update_calls, "disconnect must still update the modal view"
    body: dict[str, Any] = update_calls[0].kwargs["json"]
    view_text = str(body["view"]["blocks"][0]["text"]["text"])
    assert "Nothing to disconnect" in view_text, (
        "views.update body must report that nothing was connected"
    )
