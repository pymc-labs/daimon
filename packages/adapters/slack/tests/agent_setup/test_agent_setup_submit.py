"""Tests for agent_setup/submit.py.

Covers:
- Pure evaluators: response_action keyed to correct input block_id
- Secret-paste: key-name validation, cap, byte limit, value-absence guarantee
- edit-repo: blank PAT = keep (proceed=True, pat_replace=False)
- run_* handlers via FakeSlackWebClient:
  - run_new_agent_submission (admin, write succeeds) posts :white_check_mark: ephemeral; no views_update
  - run_paste_secrets_submission (admin, 2 pairs) posts count ephemeral without secret values; no views_update
  - non-admin users.info → no write, :x: ephemeral sent (T-83-14)
"""

from __future__ import annotations

import json
import re
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import yarl
from daimon.adapters.slack.agent_setup.submit import (
    _SECRET_CAP,
    SubmitDecision,
    evaluate_edit_agent_submission,
    evaluate_edit_repo_submission,
    evaluate_fork_agent_submission,
    evaluate_new_agent_submission,
    evaluate_paste_secrets_submission,
    run_edit_repo_submission,
    run_new_agent_submission,
    run_paste_secrets_submission,
)
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.testing.ma import build_fake_anthropic, make_fake_ma_handler
from pydantic import SecretStr

# ---------------------------------------------------------------------------
# Helpers for building minimal Slack view_submission payloads
# ---------------------------------------------------------------------------

_TEAM_ID = "T_TEST"
_USER_ID = "U_TEST"
_CHANNEL_ID = "C_TEST"
_AGENT_NAME = "my-agent"

_USERS_INFO_PATTERN = re.compile(r"https://slack\.com/api/users\.info.*")

_ADMIN_USERS_INFO_PAYLOAD = {
    "ok": True,
    "user": {
        "id": _USER_ID,
        "name": "admin",
        "is_admin": True,
        "is_owner": False,
        "is_primary_owner": False,
    },
}


def _override_users_info_admin(mock: Any) -> None:
    """Replace the conftest non-admin users.info stub with an admin one.

    aioresponses stores matchers by uuid key in insertion order — the first
    matching entry wins. The conftest registers the non-admin baseline with
    repeat=True so a plain .get() append never takes effect. This helper removes
    existing pattern-matched users.info entries and re-registers an admin payload.
    """
    to_remove = [
        k
        for k, v in mock._matches.items()  # type: ignore[attr-defined]
        if getattr(v, "url_or_pattern", None) == _USERS_INFO_PATTERN
    ]
    for k in to_remove:
        del mock._matches[k]  # type: ignore[attr-defined]
    mock.get(  # pyright: ignore[reportUnknownMemberType]
        _USERS_INFO_PATTERN,
        payload=_ADMIN_USERS_INFO_PAYLOAD,
        repeat=True,
    )


def _build_runtime_no_db(fernet_key: str = "dummy") -> SlackRuntime:
    """Build a SlackRuntime with fake MA transport and a dummy (unused) sessionmaker.

    Suitable for run_* handlers that do not use runtime.sessionmaker
    (e.g. run_new_agent_submission).
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    settings: MagicMock = MagicMock()
    settings.crypto.keys = (SecretStr(fernet_key),)
    settings.mcp.public_url = None
    settings.mcp.jwt_secret = None
    settings.github = MagicMock()
    settings.github.app_id = None
    return SlackRuntime(
        settings=settings,
        anthropic=build_fake_anthropic(make_fake_ma_handler()),
        sessionmaker=async_sessionmaker(),  # pyright: ignore[reportArgumentType]
        http_client=MagicMock(spec=httpx.AsyncClient),
    )


def _meta(
    *,
    team_id: str = _TEAM_ID,
    agent_name: str = _AGENT_NAME,
    active_section: str = "agent",
) -> str:
    return json.dumps(
        {
            "team_id": team_id,
            "channel_id": _CHANNEL_ID,
            "agent_name": agent_name,
            "active_section": active_section,
        },
        separators=(",", ":"),
    )


def _payload(
    *,
    callback_id: str,
    values: dict[str, Any],
    team_id: str = _TEAM_ID,
    user_id: str = _USER_ID,
    agent_name: str = _AGENT_NAME,
) -> dict[str, Any]:
    """Build a minimal view_submission payload with the given state values."""
    return {
        "user": {"id": user_id},
        "view": {
            "callback_id": callback_id,
            "private_metadata": _meta(team_id=team_id, agent_name=agent_name),
            "state": {"values": values},
        },
    }


def _input_value(block_id: str, action_id: str, value: str) -> dict[str, Any]:
    """Build a minimal state.values entry for a plain_text_input."""
    return {block_id: {action_id: {"type": "plain_text_input", "value": value}}}


# ---------------------------------------------------------------------------
# Pure evaluator tests — evaluate_new_agent_submission
# ---------------------------------------------------------------------------


def test_evaluate_new_agent_submission_when_name_invalid_returns_errors_keyed_new_agent_name() -> (
    None
):
    values = _input_value("new_agent__name", "new_agent__name", "bad name!")  # spaces + bang
    payload = _payload(callback_id="agent_setup__new_agent", values=values)

    decision = evaluate_new_agent_submission(payload)

    assert isinstance(decision, SubmitDecision), "should return SubmitDecision"
    assert decision.proceed is False, "invalid name should not proceed"
    assert decision.response_payload.get("response_action") == "errors", (
        "should return response_action: errors"
    )
    errors: dict[str, str] = decision.response_payload.get("errors", {})
    assert "new_agent__name" in errors, (
        "error must be keyed to new_agent__name (the input block_id)"
    )


def test_evaluate_new_agent_submission_when_name_valid_returns_clear_and_proceed() -> None:
    values = _input_value("new_agent__name", "new_agent__name", "my-agent")
    payload = _payload(callback_id="agent_setup__new_agent", values=values)

    decision = evaluate_new_agent_submission(payload)

    assert decision.proceed is True, "valid name should proceed"
    assert decision.response_payload.get("response_action") == "clear", (
        "successful new-agent submit should clear (pop to L1)"
    )
    assert decision.extra.get("name") == "my-agent", "name should be carried to extra"


def test_evaluate_new_agent_submission_when_model_invalid_returns_errors_keyed_new_agent_model() -> (
    None
):
    values = {
        **_input_value("new_agent__name", "new_agent__name", "valid-name"),
        **_input_value("new_agent__model", "new_agent__model", "gpt-4-turbo"),
    }
    payload = _payload(callback_id="agent_setup__new_agent", values=values)

    decision = evaluate_new_agent_submission(payload)

    assert decision.proceed is False, "unknown model should not proceed"
    errors = decision.response_payload.get("errors", {})
    assert "new_agent__model" in errors, (
        "error must be keyed to new_agent__model (the input block_id)"
    )


# ---------------------------------------------------------------------------
# Pure evaluator tests — evaluate_fork_agent_submission
# ---------------------------------------------------------------------------


def test_evaluate_fork_agent_submission_when_new_name_invalid_returns_errors_keyed_fork_agent_name() -> (
    None
):
    values = _input_value("fork_agent__name", "fork_agent__name", "bad name!")
    payload = _payload(callback_id="agent_setup__fork_agent", values=values)

    decision = evaluate_fork_agent_submission(payload)

    assert decision.proceed is False, "invalid fork name should not proceed"
    errors = decision.response_payload.get("errors", {})
    assert "fork_agent__name" in errors, "error must be keyed to fork_agent__name"


def test_evaluate_fork_agent_submission_when_name_valid_returns_proceed() -> None:
    values = _input_value("fork_agent__name", "fork_agent__name", "my-fork")
    payload = _payload(callback_id="agent_setup__fork_agent", values=values)

    decision = evaluate_fork_agent_submission(payload)

    assert decision.proceed is True, "valid fork name should proceed"
    assert decision.extra.get("new_name") == "my-fork", "new_name should be in extra"
    assert decision.extra.get("source_name") == _AGENT_NAME, (
        "source_name should come from private_metadata agent_name"
    )


# ---------------------------------------------------------------------------
# Pure evaluator tests — evaluate_edit_agent_submission
# ---------------------------------------------------------------------------


def test_evaluate_edit_agent_submission_when_model_invalid_returns_errors_keyed_edit_agent_model() -> (
    None
):
    values = _input_value("edit_agent__model", "edit_agent__model", "gpt-4-turbo")
    payload = _payload(callback_id="agent_setup__edit_agent", values=values)

    decision = evaluate_edit_agent_submission(payload)

    assert decision.proceed is False, "unknown model should not proceed"
    errors = decision.response_payload.get("errors", {})
    assert "edit_agent__model" in errors, "error must be keyed to edit_agent__model"


def test_evaluate_edit_agent_submission_when_model_blank_returns_proceed() -> None:
    values = _input_value("edit_agent__model", "edit_agent__model", "")
    payload = _payload(callback_id="agent_setup__edit_agent", values=values)

    decision = evaluate_edit_agent_submission(payload)

    assert decision.proceed is True, "blank model (keep current) should proceed"


# ---------------------------------------------------------------------------
# Pure evaluator tests — evaluate_edit_repo_submission
# ---------------------------------------------------------------------------


def test_evaluate_edit_repo_submission_when_pat_blank_proceeds_with_keep_flag() -> None:
    """Blank PAT = keep stored token (D-08, T-83-16): proceed=True, pat_replace=False."""
    values = {
        **_input_value("edit_repo__url", "edit_repo__url", "https://github.com/org/repo"),
        **_input_value("edit_repo__pat", "edit_repo__pat", ""),
    }
    payload = _payload(callback_id="agent_setup__edit_repo", values=values)

    decision = evaluate_edit_repo_submission(payload)

    assert decision.proceed is True, "blank PAT should proceed (empty=keep)"
    assert decision.extra.get("pat_replace") is False, (
        "blank PAT must not set pat_replace (D-08: never overwrite stored token on blank)"
    )
    assert decision.extra.get("pat") is None, "blank PAT should produce None in extra"


def test_evaluate_edit_repo_submission_when_pat_provided_sets_replace_flag() -> None:
    values = {
        **_input_value("edit_repo__url", "edit_repo__url", "https://github.com/org/repo"),
        **_input_value("edit_repo__pat", "edit_repo__pat", "ghp_test1234"),
    }
    payload = _payload(callback_id="agent_setup__edit_repo", values=values)

    decision = evaluate_edit_repo_submission(payload)

    assert decision.proceed is True, "valid PAT should proceed"
    assert decision.extra.get("pat_replace") is True, "non-blank PAT should set pat_replace"


# ---------------------------------------------------------------------------
# Pure evaluator tests — evaluate_paste_secrets_submission (D-09, T-83-15, T-83-17)
# ---------------------------------------------------------------------------


def test_evaluate_paste_secrets_when_key_invalid_returns_errors_keyed_paste_secrets_content() -> (
    None
):
    content = "123_STARTS_WITH_DIGIT=value"  # invalid: starts with digit
    values = _input_value("paste_secrets__content", "paste_secrets__content", content)
    payload = _payload(callback_id="agent_setup__paste_secrets", values=values)

    decision = evaluate_paste_secrets_submission(payload)

    assert decision.proceed is False, "invalid key name should not proceed"
    errors = decision.response_payload.get("errors", {})
    assert "paste_secrets__content" in errors, (
        "error must be keyed to paste_secrets__content (the input block_id)"
    )


def test_evaluate_paste_secrets_when_count_exceeds_cap_returns_cap_error() -> None:
    # Build _SECRET_CAP + 1 keys
    lines = "\n".join(f"KEY_{i}=value_{i}" for i in range(_SECRET_CAP + 1))
    values = _input_value("paste_secrets__content", "paste_secrets__content", lines)
    payload = _payload(callback_id="agent_setup__paste_secrets", values=values)

    decision = evaluate_paste_secrets_submission(payload)

    assert decision.proceed is False, f">{_SECRET_CAP} secrets should not proceed"
    errors = decision.response_payload.get("errors", {})
    assert "paste_secrets__content" in errors, "cap error must be keyed to paste_secrets__content"
    assert str(_SECRET_CAP) in errors["paste_secrets__content"], (
        "error text should mention the cap limit"
    )


def test_evaluate_paste_secrets_when_value_oversized_returns_byte_cap_error() -> None:
    from daimon.adapters.slack.agent_setup.submit import _MAX_SECRET_VALUE_BYTES

    oversized_value = "x" * (_MAX_SECRET_VALUE_BYTES + 1)
    content = f"MY_KEY={oversized_value}"
    values = _input_value("paste_secrets__content", "paste_secrets__content", content)
    payload = _payload(callback_id="agent_setup__paste_secrets", values=values)

    decision = evaluate_paste_secrets_submission(payload)

    assert decision.proceed is False, "oversized value should not proceed"
    errors = decision.response_payload.get("errors", {})
    assert "paste_secrets__content" in errors, (
        "byte-cap error must be keyed to paste_secrets__content"
    )
    # CRITICAL (D-09): the error message must reference the KEY name, not the value.
    error_text = errors["paste_secrets__content"]
    assert "MY_KEY" in error_text, "error text should name the offending key"
    assert oversized_value not in error_text, (
        "secret VALUE must never appear in the error message (D-09, T-83-15)"
    )


def test_evaluate_paste_secrets_when_valid_response_payload_does_not_contain_values() -> None:
    """Serialized response_payload must not contain any secret value (D-09, T-83-15)."""
    secret_value = "s3cr3t_val_that_should_not_leak"
    content = f"API_KEY={secret_value}"
    values = _input_value("paste_secrets__content", "paste_secrets__content", content)
    payload = _payload(callback_id="agent_setup__paste_secrets", values=values)

    decision = evaluate_paste_secrets_submission(payload)

    assert decision.proceed is True, "valid secret should proceed"
    # Serialize the response_payload and assert the value is absent.
    serialized = json.dumps(decision.response_payload)
    assert secret_value not in serialized, (
        "secret VALUE must never appear in the response_action payload (D-09, T-83-15)"
    )


def test_evaluate_paste_secrets_when_valid_extra_carries_pairs() -> None:
    content = "FOO=bar\nBAZ=qux"
    values = _input_value("paste_secrets__content", "paste_secrets__content", content)
    payload = _payload(callback_id="agent_setup__paste_secrets", values=values)

    decision = evaluate_paste_secrets_submission(payload)

    assert decision.proceed is True, "valid secrets should proceed"
    pairs: list[tuple[str, str]] = decision.extra.get("pairs", [])
    assert len(pairs) == 2, "should parse 2 key-value pairs"
    assert ("FOO", "bar") in pairs, "FOO=bar should be in parsed pairs"
    assert ("BAZ", "qux") in pairs, "BAZ=qux should be in parsed pairs"


# ---------------------------------------------------------------------------
# run_* handler tests via FakeSlackWebClient
# ---------------------------------------------------------------------------
# These tests require DAIMON_DATABASE__TEST_URL (real Postgres) for the DB write.
# The non-admin refusal test needs no DB write — it only checks the ephemeral.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_paste_secrets_when_non_admin_sends_permission_denied_ephemeral(
    fake_slack_web_client: Any,  # FakeSlackWebClient from conftest
) -> None:
    """Non-admin users.info default → no write, :x: ephemeral sent (T-83-14)."""
    # conftest default users.info is non-admin (fail-closed baseline)
    # We do NOT override admin here — this test verifies the fail-closed path.

    # Build a decision with valid pairs (would succeed if admin).
    _decision = SubmitDecision(
        response_payload={"response_action": "clear"},
        proceed=True,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        agent_name=_AGENT_NAME,
        parent_section="secrets",
        extra={"pairs": [("SOME_KEY", "some_val")]},
    )

    # run_paste_secrets needs a runtime with sessionmaker; we can't provide a
    # full SlackRuntime without real settings. We test the fail-closed gate
    # directly by calling _refuse_non_admin.
    from daimon.adapters.slack.agent_setup.submit import _refuse_non_admin

    refused = await _refuse_non_admin(
        fake_slack_web_client.client,
        team_id=_TEAM_ID,
        channel_id=_CHANNEL_ID,
        user_id=_USER_ID,
    )

    assert refused is True, "non-admin user should be refused (T-83-14 fail-closed)"

    # Verify the :x: ephemeral was posted to the channel.
    import yarl

    post_key = ("POST", yarl.URL("https://slack.com/api/chat.postEphemeral"))
    assert post_key in fake_slack_web_client.mock.requests, (
        "non-admin refusal should post a chat.postEphemeral"
    )


@pytest.mark.asyncio
async def test_run_refuse_non_admin_when_admin_returns_false() -> None:
    """Admin users.info → _refuse_non_admin returns False (T-83-14 admin pass-through).

    Uses a fresh AioResponsesMock context (not the conftest fixture) so the
    non-admin repeat=True default doesn't shadow the admin registration.
    """
    import re

    from aioresponses import aioresponses as AioResponsesMock
    from daimon.adapters.slack.agent_setup.submit import _refuse_non_admin
    from slack_sdk.web.async_client import AsyncWebClient

    _USERS_INFO_PATTERN = re.compile(r"https://slack\.com/api/users\.info.*")

    with AioResponsesMock() as mock:
        mock.get(  # pyright: ignore[reportUnknownMemberType]
            _USERS_INFO_PATTERN,
            payload={
                "ok": True,
                "user": {
                    "id": _USER_ID,
                    "is_admin": True,
                    "is_owner": False,
                    "is_primary_owner": False,
                },
            },
            repeat=True,
        )
        # views.push fallback for the ephemeral that would be sent on refusal
        # (not sent when admin, so this is just safety registration)
        mock.post(  # pyright: ignore[reportUnknownMemberType]
            "https://slack.com/api/chat.postEphemeral",
            payload={"ok": True},
            repeat=True,
        )
        client = AsyncWebClient(token="xoxb-test")
        refused = await _refuse_non_admin(
            client,
            team_id=_TEAM_ID,
            channel_id=_CHANNEL_ID,
            user_id=_USER_ID,
        )

    assert refused is False, "admin user should NOT be refused (T-83-14 admin pass-through)"


@pytest.mark.asyncio
async def test_run_refuse_non_admin_with_dev_allow_all_returns_false_without_users_info() -> None:
    """dev_allow_all opens the submit gate for a non-admin without any users.info I/O.

    Mirrors the actions.py wiring: the write layer (submit.py) must honor the
    DAIMON_SLACK__DEV_ALLOW_ALL_ADMIN escape hatch too, else fork/edit submissions
    are refused before the MA write and nothing persists. No users.info response
    is registered — reaching False proves the short-circuit skipped the network.
    """
    from aioresponses import aioresponses as AioResponsesMock
    from daimon.adapters.slack.agent_setup.submit import _refuse_non_admin
    from slack_sdk.web.async_client import AsyncWebClient

    with AioResponsesMock():
        client = AsyncWebClient(token="xoxb-test")
        refused = await _refuse_non_admin(
            client,
            team_id=_TEAM_ID,
            channel_id=_CHANNEL_ID,
            user_id=_USER_ID,
            dev_allow_all=True,
        )

    assert refused is False, "dev_allow_all=True must let a non-admin proceed (no refusal)"


# ---------------------------------------------------------------------------
# CR-02 regression: success ephemerals and NO views_update on cleared view
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_new_agent_submission_when_admin_and_write_succeeds_posts_success_ephemeral_and_no_views_update(
    fake_slack_web_client: Any,
) -> None:
    """Admin create succeeds → :white_check_mark: ephemeral posted; views_update NOT called.

    CR-02 fix: _refresh_l1 was removed; views_update on the cleared L3 view_id
    would return not_found and produce a spurious :x: failure. The fix posts a
    :white_check_mark: chat_postEphemeral instead.
    """
    client_fake: Any = fake_slack_web_client
    _override_users_info_admin(client_fake.mock)

    runtime = _build_runtime_no_db()

    await run_new_agent_submission(
        runtime,
        client_fake.client,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        view_id="V_SUBMIT_TEST",
        extra={"name": "fresh-agent", "model": "claude-sonnet-4-6", "system": None},
    )

    ephemeral_key = ("POST", yarl.URL("https://slack.com/api/chat.postEphemeral"))
    views_update_key = ("POST", yarl.URL("https://slack.com/api/views.update"))

    assert ephemeral_key in client_fake.mock.requests, (
        "successful create should post a chat_postEphemeral"
    )
    ephemeral_calls: list[Any] = client_fake.mock.requests[ephemeral_key]
    assert len(ephemeral_calls) == 1, "exactly one ephemeral should be posted"

    # The Slack SDK sends chat.postEphemeral as JSON (kwargs["json"]).
    ephemeral_text: str = ephemeral_calls[0].kwargs["json"]["text"]
    assert ":white_check_mark:" in ephemeral_text, (
        "success ephemeral text must contain :white_check_mark:"
    )

    assert views_update_key not in client_fake.mock.requests, (
        "run_new_agent_submission must NOT call views_update on the cleared L3 view (CR-02)"
    )


@pytest.mark.asyncio
async def test_run_paste_secrets_submission_when_admin_and_two_pairs_posts_count_ephemeral_without_values_and_no_views_update(
    fake_slack_web_client: Any,
    db_session_factory: Any,
) -> None:
    """Admin paste-secrets (2 pairs) → count confirmation ephemeral without secret values; no views_update.

    Threat T-83-22: the confirmation references only key names/count — never pair values.
    CR-02 fix: no views_update call on the cleared L3 view.
    """
    from cryptography.fernet import Fernet
    from daimon.adapters.slack.runtime import SlackRuntime
    from daimon.core._models import Tenant
    from daimon.core.ma_identity import derive_tenant_uuid

    # We need a Tenant row so put_agent_file's FK resolves. Seed it via a
    # one-shot session from the factory (per-test schema isolation is active).
    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=_TEAM_ID)
    async with db_session_factory() as session:
        session.add(Tenant(id=tenant_id, platform="slack", external_id=_TEAM_ID))
        await session.commit()

    client_fake: Any = fake_slack_web_client
    _override_users_info_admin(client_fake.mock)

    fernet_key = Fernet.generate_key().decode()
    settings: MagicMock = MagicMock()
    settings.crypto.keys = (SecretStr(fernet_key),)
    settings.mcp.public_url = None
    settings.mcp.jwt_secret = None
    settings.github = MagicMock()
    settings.github.app_id = None

    # MA handler: the agent must exist so run_paste_secrets can find it via find_agent_by_daimon_tag.
    from datetime import UTC, datetime

    from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT

    _ma_agent_id = f"agent_{'b' * 24}"
    now = datetime.now(UTC).isoformat()
    _agent_data: dict[str, object] = {
        "id": _ma_agent_id,
        "type": "agent",
        "name": _AGENT_NAME,
        "version": 1,
        "model": {"id": "claude-sonnet-4-6", "speed": "standard"},
        "system": None,
        "metadata": {
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_NAME: _AGENT_NAME,
        },
        "mcp_servers": [],
        "tools": [],
        "skills": [],
        "created_at": now,
        "updated_at": now,
        "archived_at": None,
        "description": None,
    }

    import httpx

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if method == "GET" and path == "/v1/agents":
            return httpx.Response(200, json={"data": [_agent_data], "has_more": False})
        if method == "GET" and path == "/v1/environments":
            return httpx.Response(200, json={"data": [], "has_more": False})
        return httpx.Response(404, json={"error": f"unhandled {method} {path}"})

    runtime = SlackRuntime(
        settings=settings,
        anthropic=build_fake_anthropic(_handler),
        sessionmaker=db_session_factory,
        http_client=MagicMock(spec=httpx.AsyncClient),
    )

    secret_val_1 = "s3cr3t_one"
    secret_val_2 = "s3cr3t_two"

    await run_paste_secrets_submission(
        runtime,
        client_fake.client,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        view_id="V_SUBMIT_TEST",
        agent_name=_AGENT_NAME,
        parent_section="secrets",
        extra={"pairs": [("KEY_ONE", secret_val_1), ("KEY_TWO", secret_val_2)]},
    )

    ephemeral_key = ("POST", yarl.URL("https://slack.com/api/chat.postEphemeral"))
    views_update_key = ("POST", yarl.URL("https://slack.com/api/views.update"))

    assert ephemeral_key in client_fake.mock.requests, (
        "successful paste-secrets should post a chat_postEphemeral"
    )
    ephemeral_calls = client_fake.mock.requests[ephemeral_key]
    assert len(ephemeral_calls) >= 1, "at least one ephemeral should be posted"

    # Find the success ephemeral (the Slack SDK sends JSON body; text is in kwargs["json"]["text"]).
    ephemeral_texts = [call.kwargs["json"]["text"] for call in ephemeral_calls]
    success_texts = [t for t in ephemeral_texts if ":white_check_mark:" in t]
    assert len(success_texts) >= 1, "success confirmation ephemeral must include :white_check_mark:"

    success_text = success_texts[0]
    assert "2" in success_text or "secrets" in success_text, (
        "success text for 2 pairs must reference the count (e.g. '2 secrets')"
    )

    # T-83-22: secret values must NOT appear in the confirmation text.
    assert secret_val_1 not in success_text, (
        f"secret value '{secret_val_1}' must not appear in the confirmation (T-83-22)"
    )
    assert secret_val_2 not in success_text, (
        f"secret value '{secret_val_2}' must not appear in the confirmation (T-83-22)"
    )

    assert views_update_key not in client_fake.mock.requests, (
        "run_paste_secrets_submission must NOT call views_update on the cleared L3 view (CR-02)"
    )


# ---------------------------------------------------------------------------
# Phase 94 (PAT-CLOBBER): run_edit_repo_submission preserves ma_secret_ref
# ---------------------------------------------------------------------------


def _build_edit_repo_ma_handler(*, tenant_id: Any, ma_agent_id: str) -> Any:
    """Build an httpx handler exposing a single agent for find_agent_by_daimon_tag."""
    import httpx
    from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT

    now = _iso_now()
    agent_data: dict[str, object] = {
        "id": ma_agent_id,
        "type": "agent",
        "name": _AGENT_NAME,
        "version": 1,
        "model": {"id": "claude-sonnet-4-6", "speed": "standard"},
        "system": None,
        "metadata": {
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_NAME: _AGENT_NAME,
        },
        "mcp_servers": [],
        "tools": [],
        "skills": [],
        "created_at": now,
        "updated_at": now,
        "archived_at": None,
        "description": None,
    }

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if method == "GET" and path == "/v1/agents":
            return httpx.Response(200, json={"data": [agent_data], "has_more": False})
        if method == "GET" and path == "/v1/environments":
            return httpx.Response(200, json={"data": [], "has_more": False})
        return httpx.Response(404, json={"error": f"unhandled {method} {path}"})

    return _handler


def _iso_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


@pytest.mark.asyncio
async def test_run_edit_repo_submission_when_pat_replace_false_preserves_inline_pat(
    fake_slack_web_client: Any,
    db_session_factory: Any,
) -> None:
    """PAT-CLOBBER regression: blank PAT edit-repo preserves the stored ma_secret_ref.

    Store an inline PAT (ma_secret_ref=inline-pat:{agent_uuid}) directly on the
    binding, then submit edit-repo with a new URL + pat_replace=False (blank PAT).
    The binding's ma_secret_ref must still equal inline-pat:{agent_uuid} and
    repo_url must reflect the new URL.
    """
    from daimon.adapters.slack.runtime import SlackRuntime
    from daimon.core._models import Tenant
    from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
    from daimon.core.stores.agent_repo_binding import get_binding, set_binding

    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=_TEAM_ID)
    ma_agent_id = f"agent_{'c' * 24}"
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)

    async with db_session_factory() as session:
        session.add(Tenant(id=tenant_id, platform="slack", external_id=_TEAM_ID))
        await session.commit()

    async with db_session_factory() as session:
        await set_binding(
            session,
            tenant_id=tenant_id,
            agent_id=agent_uuid,
            repo_url="https://github.com/example/old.git",
            default_branch="main",
            ma_secret_ref=f"inline-pat:{agent_uuid}",
        )
        await session.commit()

    client_fake: Any = fake_slack_web_client
    _override_users_info_admin(client_fake.mock)

    settings: MagicMock = MagicMock()
    settings.crypto.keys = (SecretStr("dummy"),)
    settings.mcp.public_url = None
    settings.mcp.jwt_secret = None
    settings.github = MagicMock()
    settings.github.app_id = None

    runtime = SlackRuntime(
        settings=settings,
        anthropic=build_fake_anthropic(
            _build_edit_repo_ma_handler(tenant_id=tenant_id, ma_agent_id=ma_agent_id)
        ),
        sessionmaker=db_session_factory,
        http_client=MagicMock(spec=httpx.AsyncClient),
    )

    await run_edit_repo_submission(
        runtime,
        client_fake.client,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        view_id="V_SUBMIT_TEST",
        agent_name=_AGENT_NAME,
        parent_section="repo",
        extra={"repo_url": "https://github.com/example/new.git", "pat": "", "pat_replace": False},
    )

    async with db_session_factory() as session:
        row = await get_binding(session, tenant_id=tenant_id, agent_id=agent_uuid)

    assert row is not None, "binding should still exist after edit"
    assert row.ma_secret_ref == f"inline-pat:{agent_uuid}", (
        "blank-PAT edit-repo must preserve the stored ma_secret_ref, not clobber it to anon:"
    )
    assert row.repo_url == "example/new", "repo_url should be updated to the new value"

    ephemeral_key = ("POST", yarl.URL("https://slack.com/api/chat.postEphemeral"))
    assert ephemeral_key in client_fake.mock.requests, "success ephemeral should be posted"
    ephemeral_texts = [
        call.kwargs["json"]["text"] for call in client_fake.mock.requests[ephemeral_key]
    ]
    assert any(":white_check_mark:" in t for t in ephemeral_texts), (
        "success ephemeral must be posted for the blank-PAT edit"
    )


@pytest.mark.asyncio
async def test_run_edit_repo_submission_when_pat_replace_true_stores_new_inline_pat(
    fake_slack_web_client: Any,
    db_session_factory: Any,
) -> None:
    """pat_replace=True + a typed PAT still replaces (existing behavior preserved)."""
    from cryptography.fernet import Fernet
    from daimon.adapters.slack.runtime import SlackRuntime
    from daimon.core._models import Tenant
    from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
    from daimon.core.stores.agent_repo_binding import get_binding

    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=_TEAM_ID)
    ma_agent_id = f"agent_{'d' * 24}"
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)

    async with db_session_factory() as session:
        session.add(Tenant(id=tenant_id, platform="slack", external_id=_TEAM_ID))
        await session.commit()

    client_fake: Any = fake_slack_web_client
    _override_users_info_admin(client_fake.mock)

    fernet_key = Fernet.generate_key().decode()
    settings: MagicMock = MagicMock()
    settings.crypto.keys = (SecretStr(fernet_key),)
    settings.mcp.public_url = None
    settings.mcp.jwt_secret = None
    settings.github = MagicMock()
    settings.github.app_id = None
    settings.github.oauth_scopes = ("repo",)

    runtime = SlackRuntime(
        settings=settings,
        anthropic=build_fake_anthropic(
            _build_edit_repo_ma_handler(tenant_id=tenant_id, ma_agent_id=ma_agent_id)
        ),
        sessionmaker=db_session_factory,
        http_client=MagicMock(spec=httpx.AsyncClient),
    )

    await run_edit_repo_submission(
        runtime,
        client_fake.client,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        view_id="V_SUBMIT_TEST",
        agent_name=_AGENT_NAME,
        parent_section="repo",
        extra={
            "repo_url": "https://github.com/example/private.git",
            "pat": "ghp_newtoken1234",
            "pat_replace": True,
        },
    )

    async with db_session_factory() as session:
        row = await get_binding(session, tenant_id=tenant_id, agent_id=agent_uuid)

    assert row is not None, "binding should exist after first-time bind with a replaced PAT"
    assert row.ma_secret_ref == f"inline-pat:{agent_uuid}", (
        "pat_replace=True must store the inline PAT reference"
    )
    assert row.repo_url == "example/private", "repo_url should be bound to the new value"


# ---------------------------------------------------------------------------
# Phase 97: run_edit_repo_submission first-time no-PAT bind writes anon:
# ---------------------------------------------------------------------------


def _build_edit_repo_settings(*, app_id: str | None) -> MagicMock:
    settings: MagicMock = MagicMock()
    settings.crypto.keys = (SecretStr("dummy"),)
    settings.mcp.public_url = None
    settings.mcp.jwt_secret = None
    settings.github = MagicMock()
    settings.github.app_id = app_id
    return settings


@pytest.mark.asyncio
async def test_run_edit_repo_submission_no_pat_binds_anon(
    fake_slack_web_client: Any,
    db_session_factory: Any,
) -> None:
    """First-time no-PAT bind writes an anon: binding and reports success.

    No App-coverage probe on Slack: the Slack create_session call does not
    thread session_factory, so the repo-clone path never runs on Slack today —
    the panel must not advertise App coverage. Wiring Slack repo clone is a
    tracked follow-up.
    """
    from daimon.adapters.slack.runtime import SlackRuntime
    from daimon.core._models import Tenant
    from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
    from daimon.core.stores.agent_repo_binding import get_binding

    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=_TEAM_ID)
    ma_agent_id = f"agent_{'e' * 24}"
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)

    async with db_session_factory() as session:
        session.add(Tenant(id=tenant_id, platform="slack", external_id=_TEAM_ID))
        await session.commit()

    client_fake: Any = fake_slack_web_client
    _override_users_info_admin(client_fake.mock)

    runtime = SlackRuntime(
        settings=_build_edit_repo_settings(app_id=None),
        anthropic=build_fake_anthropic(
            _build_edit_repo_ma_handler(tenant_id=tenant_id, ma_agent_id=ma_agent_id)
        ),
        sessionmaker=db_session_factory,
        http_client=MagicMock(spec=httpx.AsyncClient),
    )

    await run_edit_repo_submission(
        runtime,
        client_fake.client,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        view_id="V_SUBMIT_TEST",
        agent_name=_AGENT_NAME,
        parent_section="repo",
        extra={
            "repo_url": "https://github.com/example/covered.git",
            "pat": "",
            "pat_replace": False,
        },
    )

    async with db_session_factory() as session:
        row = await get_binding(session, tenant_id=tenant_id, agent_id=agent_uuid)

    assert row is not None, "binding should exist after first-time no-PAT bind"
    assert row.ma_secret_ref == "anon:", "no-PAT bind writes an anon: binding"

    ephemeral_key = ("POST", yarl.URL("https://slack.com/api/chat.postEphemeral"))
    ephemeral_texts = [
        call.kwargs["json"]["text"] for call in client_fake.mock.requests[ephemeral_key]
    ]
    assert any("Saved repo + auth" in t for t in ephemeral_texts), (
        "user should see the plain save confirmation"
    )
    assert not any("App-covered" in t for t in ephemeral_texts), (
        "Slack panel must not advertise App coverage (Slack does not clone yet)"
    )
