"""Tests for agent_setup/submit.py.

Covers:
- Pure evaluators: response_action keyed to correct input block_id
- Secret-paste: key-name validation, cap, byte limit, value-absence guarantee
- edit-repo: blank PAT = keep (proceed=True, pat_replace=False)
- run_* handlers via FakeSlackWebClient:
  - run_new_agent_submission (admin, write succeeds) posts :white_check_mark: ephemeral; no views_update
  - run_paste_secrets_submission (admin, 2 pairs) posts count ephemeral without secret values; no views_update
  - creation (new/fork): a non-admin submission succeeds with no
    permission-refusal ephemeral
  - the two per-agent attachment submissions (edit_repo/paste_secrets): a
    non-admin proceeds only when the target agent is neither defaults-managed
    nor currently reachable, is refused before any MA request otherwise, and
    an admin succeeds against the workspace's currently-default agent
  - the three field-conditional paths (edit_agent/add_skill/add_mcp): a
    non-admin submission proceeds on an unreachable agent and is refused
    before any MA request on a reachable one, via the shared
    ``agent_setup.gate`` helper
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import structlog
import structlog.testing
import yarl
from cryptography.fernet import Fernet
from daimon.adapters.slack.agent_setup import submit as submit_mod
from daimon.adapters.slack.agent_setup.submit import (
    _SECRET_CAP,
    SubmitDecision,
    evaluate_edit_agent_submission,
    evaluate_edit_repo_submission,
    evaluate_fork_agent_submission,
    evaluate_new_agent_submission,
    evaluate_paste_secrets_submission,
    run_add_mcp_submission,
    run_add_skill_submission,
    run_edit_agent_submission,
    run_edit_repo_submission,
    run_fork_agent_submission,
    run_new_agent_submission,
    run_paste_secrets_submission,
)
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.scope import TenantScopeRef
from daimon.core.skill_sync import SyncReport
from daimon.core.stores.scoped_config_write import set_fields
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma import build_fake_anthropic, make_fake_ma_handler
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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


def _github_handler_never_called(request: httpx.Request) -> httpx.Response:
    """Default GitHub transport handler for tests that never touch a repo probe.

    A MagicMock client would make every probe return a truthy mock object,
    silently passing every access check — the gate would be inert while the
    suite stayed green. Failing loudly here is what catches a test wiring a
    code path onto the GitHub probe without declaring the response it expects.
    """
    raise AssertionError(f"unexpected GitHub API call: {request.method} {request.url}")


def _github_handler(
    status: int, body: dict[str, Any] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    """Build a fixed-response GET handler for `https://api.github.com/repos/...`.

    Every run_edit_repo_submission invocation makes at most one GitHub call
    per submit, so a single fixed response is enough to drive is_public_repo
    or pat_can_access_repo — whichever the code path under test reaches.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.github.com", f"unexpected host: {request.url}"
        return httpx.Response(status, json=body if body is not None else {})

    return handler


def _build_http_client(
    github_handler: Callable[[httpx.Request], httpx.Response] | None = None,
) -> httpx.AsyncClient:
    """Real httpx.AsyncClient over MockTransport — never a MagicMock stand-in."""
    return httpx.AsyncClient(
        transport=httpx.MockTransport(github_handler or _github_handler_never_called)
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
        billing_config=None,
        http_client=_build_http_client(),
        resolver_cache=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
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
    """Blank PAT = keep stored token: proceed=True, pat_replace=False."""
    values = {
        **_input_value("edit_repo__url", "edit_repo__url", "https://github.com/org/repo"),
        **_input_value("edit_repo__pat", "edit_repo__pat", ""),
    }
    payload = _payload(callback_id="agent_setup__edit_repo", values=values)

    decision = evaluate_edit_repo_submission(payload)

    assert decision.proceed is True, "blank PAT should proceed (empty=keep)"
    assert decision.extra.get("pat_replace") is False, (
        "blank PAT must not set pat_replace (never overwrite stored token on blank)"
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
# Pure evaluator tests — evaluate_paste_secrets_submission
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
    # CRITICAL: the error message must reference the KEY name, not the value.
    error_text = errors["paste_secrets__content"]
    assert "MY_KEY" in error_text, "error text should name the offending key"
    assert oversized_value not in error_text, "secret VALUE must never appear in the error message"


def test_evaluate_paste_secrets_when_valid_response_payload_does_not_contain_values() -> None:
    """Serialized response_payload must not contain any secret value."""
    secret_value = "s3cr3t_val_that_should_not_leak"
    content = f"API_KEY={secret_value}"
    values = _input_value("paste_secrets__content", "paste_secrets__content", content)
    payload = _payload(callback_id="agent_setup__paste_secrets", values=values)

    decision = evaluate_paste_secrets_submission(payload)

    assert decision.proceed is True, "valid secret should proceed"
    # Serialize the response_payload and assert the value is absent.
    serialized = json.dumps(decision.response_payload)
    assert secret_value not in serialized, (
        "secret VALUE must never appear in the response_action payload"
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
# ---------------------------------------------------------------------------


def _iso_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _agent_payload_for_gate_tests(
    *, tenant_id: Any, agent_id: str, agent_name: str = _AGENT_NAME
) -> dict[str, object]:
    """Build a minimal MA agent payload tagged for this tenant/name."""
    from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME, MA_METADATA_KEY_TENANT

    now = _iso_now()
    return {
        "id": agent_id,
        "type": "agent",
        "name": agent_name,
        "version": 1,
        "model": {"id": "claude-sonnet-4-6", "speed": "standard"},
        "system": None,
        "metadata": {
            MA_METADATA_KEY_TENANT: str(tenant_id),
            MA_METADATA_KEY_NAME: agent_name,
        },
        "mcp_servers": [],
        "tools": [],
        "skills": [],
        "created_at": now,
        "updated_at": now,
        "archived_at": None,
        "description": None,
    }


def _make_recording_ma_handler_with_agents_and_update(
    agents: list[dict[str, object]],
    calls: list[tuple[str, str]],
) -> Any:
    """Like a PATCH-capable fake MA handler, but records every (method, path)
    it serves so a refusal test can assert zero MA traffic occurred."""
    agent_store: dict[str, dict[str, object]] = {
        str(ag["id"]): ag
        for ag in agents  # type: ignore[index]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        calls.append((method, path))

        if method == "GET" and path == "/v1/agents":
            return httpx.Response(200, json={"data": list(agent_store.values()), "has_more": False})
        m = re.match(r"^/v1/agents/(?P<id>[^/]+)$", path)
        if m and method == "GET":
            agent_id_req = m.group("id")
            if agent_id_req in agent_store:
                return httpx.Response(200, json=agent_store[agent_id_req])
            return httpx.Response(
                404,
                json={
                    "type": "error",
                    "error": {"type": "not_found_error", "message": "not found"},
                },
            )
        if m and method in {"PATCH", "POST"}:
            agent_id_req = m.group("id")
            if agent_id_req not in agent_store:
                return httpx.Response(
                    404,
                    json={
                        "type": "error",
                        "error": {"type": "not_found_error", "message": "not found"},
                    },
                )
            body: dict[str, Any] = json.loads(request.content)
            existing = agent_store[agent_id_req]
            merged: dict[str, object] = {**existing, **body}
            merged["version"] = int(existing.get("version", 1)) + 1  # type: ignore[arg-type]
            agent_store[agent_id_req] = merged
            return httpx.Response(200, json=merged)
        if method == "GET" and path == "/v1/environments":
            return httpx.Response(200, json={"data": [], "has_more": False})

        return httpx.Response(404, json={"error": f"unhandled {method} {path}"})

    return handler


def _build_runtime_with_db(
    db_factory: async_sessionmaker[AsyncSession],
    *,
    fernet_key: str = "dummy",
    anthropic_handler: Any = None,
) -> SlackRuntime:
    """Build a SlackRuntime with a real DB factory and a fake MA transport."""
    handler = anthropic_handler or make_fake_ma_handler()
    settings: MagicMock = MagicMock()
    settings.crypto.keys = (SecretStr(fernet_key),)
    settings.mcp.public_url = None
    settings.mcp.jwt_secret = None
    settings.github = MagicMock()
    settings.github.app_id = None
    return SlackRuntime(
        settings=settings,
        anthropic=build_fake_anthropic(handler),
        sessionmaker=db_factory,
        billing_config=None,
        http_client=_build_http_client(),
        resolver_cache=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
    )


async def _mark_reachable(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: Any,
    agent_name: str,
) -> None:
    """Write a real tenant-scope propagation row so the named agent is
    currently reachable — never patch the reachability predicate."""
    async with db_session_factory() as session, session.begin():
        await set_fields(
            session,
            scope=TenantScopeRef(tenant_id=tenant_id),
            tenant_id=tenant_id,
            agent_name=agent_name,
            mode="agent",
        )


def _ephemeral_texts(client_fake: Any) -> list[str]:
    ephemeral_key = ("POST", yarl.URL("https://slack.com/api/chat.postEphemeral"))
    return [
        call.kwargs["json"]["text"] for call in client_fake.mock.requests.get(ephemeral_key, [])
    ]


# ---------------------------------------------------------------------------
# Creation paths, open to every member; and the two per-agent attachment
# paths, refused for a non-admin on a shared agent — each with an
# admin-positive sibling proving the shared-agent write still works
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_new_agent_submission_when_non_admin_creates_agent_with_no_refusal(
    fake_slack_web_client: Any,
) -> None:
    """Creation is open to every workspace member — no admin re-check remains."""
    client_fake: Any = fake_slack_web_client
    # conftest default users.info is non-admin; no override needed.
    runtime = _build_runtime_no_db()

    await run_new_agent_submission(
        runtime,
        client_fake.client,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        view_id="V_SUBMIT_TEST",
        extra={"name": "member-created-agent", "model": "claude-sonnet-4-6", "system": None},
    )

    texts = _ephemeral_texts(client_fake)
    assert any(":white_check_mark:" in t for t in texts), (
        "a non-admin's new-agent submission must succeed"
    )
    assert not any("permission" in t for t in texts), (
        "creation must not post a permission-refusal ephemeral"
    )


@pytest.mark.asyncio
async def test_run_new_agent_submission_when_duplicate_name_still_refused(
    fake_slack_web_client: Any,
) -> None:
    """A name collision is still refused by create_blank_agent's own guard,
    independent of the now-removed admin check."""
    client_fake: Any = fake_slack_web_client
    runtime = _build_runtime_no_db()

    extra = {"name": "collide-agent", "model": "claude-sonnet-4-6", "system": None}
    await run_new_agent_submission(
        runtime,
        client_fake.client,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        view_id="V1",
        extra=extra,
    )
    await run_new_agent_submission(
        runtime,
        client_fake.client,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        view_id="V2",
        extra=extra,
    )

    texts = _ephemeral_texts(client_fake)
    assert any(":white_check_mark:" in t for t in texts), "the first create should succeed"
    assert any("Failed to create agent" in t for t in texts), (
        "the second, colliding create must still be refused by the collision guard"
    )


@pytest.mark.asyncio
async def test_run_fork_agent_submission_when_non_admin_forks_agent_with_no_refusal(
    fake_slack_web_client: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Forking is open to every workspace member — no admin re-check remains."""
    from cryptography.fernet import Fernet

    client_fake: Any = fake_slack_web_client
    runtime = _build_runtime_with_db(db_session_factory, fernet_key=Fernet.generate_key().decode())

    await run_new_agent_submission(
        runtime,
        client_fake.client,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        view_id="V1",
        extra={"name": _AGENT_NAME, "model": "claude-sonnet-4-6", "system": None},
    )
    await run_fork_agent_submission(
        runtime,
        client_fake.client,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        view_id="V2",
        extra={"source_name": _AGENT_NAME, "new_name": "forked-agent"},
    )

    texts = _ephemeral_texts(client_fake)
    assert any("Forked" in t for t in texts), "a non-admin's fork submission must succeed"
    assert not any("permission" in t for t in texts), (
        "fork must not post a permission-refusal ephemeral"
    )


@pytest.mark.asyncio
async def test_run_edit_repo_submission_when_non_admin_and_agent_is_workspace_default_refuses_bind(
    fake_slack_web_client: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A repo re-point changes what the whole workspace's shared agent clones,
    so a non-admin is refused against the currently-default agent — and refused
    early enough that no GitHub probe is ever made.

    The runtime's GitHub transport is the never-called handler: an outbound
    probe would raise rather than quietly succeed, which is what pins the gate
    ahead of the probe rather than merely ahead of the write."""
    from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
    from daimon.core.stores.agent_repo_binding import get_binding

    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=_TEAM_ID)
    ma_agent_id = f"agent_{'f' * 24}"
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)

    async with db_session_factory() as session:
        await make_tenant(session, platform="slack", workspace_id=_TEAM_ID, id=tenant_id)
        await session.commit()
    await _seed_guild_account(db_session_factory, tenant_id=tenant_id)
    await _mark_reachable(db_session_factory, tenant_id=tenant_id, agent_name=_AGENT_NAME)

    client_fake: Any = fake_slack_web_client
    # conftest default users.info is non-admin; no override.

    runtime = SlackRuntime(
        settings=_build_edit_repo_settings(app_id=None),
        anthropic=build_fake_anthropic(
            _build_edit_repo_ma_handler(tenant_id=tenant_id, ma_agent_id=ma_agent_id)
        ),
        sessionmaker=db_session_factory,
        billing_config=None,
        http_client=_build_http_client(),
        resolver_cache=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
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
            "repo_url": "https://github.com/example/default-agent.git",
            "pat": "",
            "pat_replace": False,
        },
    )

    async with db_session_factory() as session:
        row = await get_binding(session, tenant_id=tenant_id, agent_id=agent_uuid)

    assert row is None, "no binding may be written for a non-admin on a workspace-default agent"

    texts = _ephemeral_texts(client_fake)
    assert any("workspace-admin" in t for t in texts), (
        "the refusal ephemeral must say the change needs workspace-admin permission"
    )
    assert not any(":white_check_mark:" in t for t in texts), (
        "a refused bind must not post a success confirmation"
    )


@pytest.mark.asyncio
async def test_run_edit_repo_submission_when_admin_and_agent_is_workspace_default_binds_repo(
    fake_slack_web_client: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An admin binding a repo to the workspace's default agent is the
    first-run onboarding step and must keep working — the attachment gate
    short-circuits on live admin status before it looks at shared-ness."""
    from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
    from daimon.core.stores.agent_repo_binding import get_binding

    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=_TEAM_ID)
    ma_agent_id = f"agent_{'f' * 24}"
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)

    async with db_session_factory() as session:
        await make_tenant(session, platform="slack", workspace_id=_TEAM_ID, id=tenant_id)
        await session.commit()
    await _seed_guild_account(db_session_factory, tenant_id=tenant_id)
    await _mark_reachable(db_session_factory, tenant_id=tenant_id, agent_name=_AGENT_NAME)

    client_fake: Any = fake_slack_web_client
    _override_users_info_admin(client_fake.mock)

    runtime = SlackRuntime(
        settings=_build_edit_repo_settings(app_id=None),
        anthropic=build_fake_anthropic(
            _build_edit_repo_ma_handler(tenant_id=tenant_id, ma_agent_id=ma_agent_id)
        ),
        sessionmaker=db_session_factory,
        billing_config=None,
        http_client=_build_http_client(_github_handler(200, {"private": False})),
        resolver_cache=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
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
            "repo_url": "https://github.com/example/default-agent.git",
            "pat": "",
            "pat_replace": False,
        },
    )

    async with db_session_factory() as session:
        row = await get_binding(session, tenant_id=tenant_id, agent_id=agent_uuid)

    assert row is not None, "an admin's bind against the workspace-default agent must be written"
    assert row.repo_url == "example/default-agent", (
        "the binding must carry the submitted repo, not a stale one"
    )

    texts = _ephemeral_texts(client_fake)
    assert any(":white_check_mark:" in t for t in texts), (
        "an admin's bind must post the success confirmation"
    )
    assert not any("workspace-admin" in t for t in texts), (
        "an admin must not see the shared-agent refusal"
    )


def _paste_secrets_ma_handler(*, tenant_id: Any, ma_agent_id: str) -> Any:
    """Serve a single tenant-tagged agent for run_paste_secrets_submission."""
    agent_data = _agent_payload_for_gate_tests(tenant_id=tenant_id, agent_id=ma_agent_id)

    def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if method == "GET" and path == "/v1/agents":
            return httpx.Response(200, json={"data": [agent_data], "has_more": False})
        if method == "GET" and path == "/v1/environments":
            return httpx.Response(200, json={"data": [], "has_more": False})
        return httpx.Response(404, json={"error": f"unhandled {method} {path}"})

    return _handler


@pytest.mark.asyncio
async def test_run_paste_secrets_submission_when_non_admin_and_agent_is_workspace_default_refuses_write(
    fake_slack_web_client: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """put_agent_file is an upsert and the file it writes is mounted
    read-write on every session of the shared agent, so a non-admin is refused
    against the currently-default agent and no file exists afterwards."""
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.stores.agent_files import list_agent_files

    async with db_session_factory() as session:
        tenant = await make_tenant(session, platform="slack", workspace_id=_TEAM_ID)
        tenant_id = tenant.id
        await session.commit()
    await _mark_reachable(db_session_factory, tenant_id=tenant_id, agent_name=_AGENT_NAME)

    client_fake: Any = fake_slack_web_client
    # conftest default users.info is non-admin; no override.

    ma_agent_id = f"agent_{'g' * 24}"
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)
    runtime = _build_runtime_with_db(
        db_session_factory,
        anthropic_handler=_paste_secrets_ma_handler(tenant_id=tenant_id, ma_agent_id=ma_agent_id),
    )

    await run_paste_secrets_submission(
        runtime,
        client_fake.client,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        view_id="V_SUBMIT_TEST",
        agent_name=_AGENT_NAME,
        parent_section="secrets",
        extra={"pairs": [("OPEN_KEY", "open_val")]},
    )

    async with db_session_factory() as session:
        files = await list_agent_files(session, tenant_id=tenant_id, agent_id=agent_uuid)

    assert files == [], "a refused paste must write no agent file at all"

    texts = _ephemeral_texts(client_fake)
    assert any("workspace-admin" in t for t in texts), (
        "the refusal ephemeral must say the change needs workspace-admin permission"
    )
    assert not any(":white_check_mark:" in t for t in texts), (
        "a refused paste must not post a success confirmation"
    )


@pytest.mark.asyncio
async def test_run_paste_secrets_submission_when_admin_and_agent_is_workspace_default_writes_secrets(
    fake_slack_web_client: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An admin populating the workspace-default agent's environment still
    works, and the confirmation still carries the key name only."""
    from daimon.core.ma_identity import derive_agent_uuid
    from daimon.core.stores.agent_files import get_agent_file

    async with db_session_factory() as session:
        tenant = await make_tenant(session, platform="slack", workspace_id=_TEAM_ID)
        tenant_id = tenant.id
        await session.commit()
    await _mark_reachable(db_session_factory, tenant_id=tenant_id, agent_name=_AGENT_NAME)

    client_fake: Any = fake_slack_web_client
    _override_users_info_admin(client_fake.mock)

    ma_agent_id = f"agent_{'g' * 24}"
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)
    runtime = _build_runtime_with_db(
        db_session_factory,
        anthropic_handler=_paste_secrets_ma_handler(tenant_id=tenant_id, ma_agent_id=ma_agent_id),
    )

    secret_value = "admin_written_value"
    await run_paste_secrets_submission(
        runtime,
        client_fake.client,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        view_id="V_SUBMIT_TEST",
        agent_name=_AGENT_NAME,
        parent_section="secrets",
        extra={"pairs": [("SHARED_KEY", secret_value)]},
    )

    async with db_session_factory() as session:
        row = await get_agent_file(
            session, tenant_id=tenant_id, agent_id=agent_uuid, key="SHARED_KEY"
        )

    assert row is not None, "an admin's paste against the workspace-default agent must be written"
    assert row.content == secret_value, "the stored file must carry the submitted value"

    texts = _ephemeral_texts(client_fake)
    assert any("SHARED_KEY" in t for t in texts), (
        "the confirmation must name the key that was written"
    )
    assert not any(secret_value in t for t in texts), (
        "the confirmation must never carry the secret value"
    )


@pytest.mark.asyncio
async def test_run_paste_secrets_submission_refusal_logs_no_key_names(
    fake_slack_web_client: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The gate precedes every log line derived from the submitted pairs, so a
    refused paste leaks neither key names nor values to the operator log."""
    async with db_session_factory() as session:
        tenant = await make_tenant(session, platform="slack", workspace_id=_TEAM_ID)
        tenant_id = tenant.id
        await session.commit()
    await _mark_reachable(db_session_factory, tenant_id=tenant_id, agent_name=_AGENT_NAME)

    client_fake: Any = fake_slack_web_client
    # conftest default users.info is non-admin; no override.

    ma_agent_id = f"agent_{'g' * 24}"
    runtime = _build_runtime_with_db(
        db_session_factory,
        anthropic_handler=_paste_secrets_ma_handler(tenant_id=tenant_id, ma_agent_id=ma_agent_id),
    )

    cap = structlog.testing.LogCapture()
    structlog.configure(processors=[cap])
    try:
        await run_paste_secrets_submission(
            runtime,
            client_fake.client,
            team_id=_TEAM_ID,
            user_id=_USER_ID,
            channel_id=_CHANNEL_ID,
            view_id="V_SUBMIT_TEST",
            agent_name=_AGENT_NAME,
            parent_section="secrets",
            extra={"pairs": [("REFUSED_KEY", "refused_value")]},
        )
    finally:
        structlog.reset_defaults()

    assert all("keys" not in entry for entry in cap.entries), (
        "a refused paste must emit no log entry carrying a keys field"
    )
    assert all("REFUSED_KEY" not in str(entry) for entry in cap.entries), (
        "the submitted key name must never reach the log stream on the refusal path"
    )
    assert all("refused_value" not in str(entry) for entry in cap.entries), (
        "the submitted secret value must never reach the log stream"
    )


# ---------------------------------------------------------------------------
# Task 2 — the three field-conditional paths: reachability-gated for non-admin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_edit_agent_submission_when_non_admin_and_unreachable_proceeds(
    fake_slack_web_client: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Editing model/prompt is open when nobody has scoped this agent."""
    from daimon.core.ma_identity import derive_tenant_uuid

    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=_TEAM_ID)
    ma_agent_id = f"agent_{'h' * 24}"
    calls: list[tuple[str, str]] = []
    agent_payload = _agent_payload_for_gate_tests(tenant_id=tenant_id, agent_id=ma_agent_id)
    handler = _make_recording_ma_handler_with_agents_and_update([agent_payload], calls)
    runtime = _build_runtime_with_db(db_session_factory, anthropic_handler=handler)

    client_fake: Any = fake_slack_web_client
    # conftest default users.info is non-admin; no override.

    await run_edit_agent_submission(
        runtime,
        client_fake.client,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        view_id="V_SUBMIT_TEST",
        agent_name=_AGENT_NAME,
        parent_section="agent",
        extra={"model": None, "system": "You are helpful."},
    )

    assert any(method in {"PATCH", "POST"} for method, path in calls if "/v1/agents/" in path), (
        "an unreachable agent's edit must reach the MA update"
    )

    texts = _ephemeral_texts(client_fake)
    assert any(":white_check_mark:" in t for t in texts), (
        "edit-agent must succeed for a non-admin on an unreachable agent"
    )


@pytest.mark.asyncio
async def test_run_edit_agent_submission_when_non_admin_and_reachable_refused(
    fake_slack_web_client: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A non-admin editing a currently-default agent must be refused before
    any MA request."""
    async with db_session_factory() as session:
        tenant = await make_tenant(session, platform="slack", workspace_id=_TEAM_ID)
        tenant_id = tenant.id
        await session.commit()
    await _mark_reachable(db_session_factory, tenant_id=tenant_id, agent_name=_AGENT_NAME)

    calls: list[tuple[str, str]] = []
    ma_agent_id = f"agent_{'i' * 24}"
    agent_payload = _agent_payload_for_gate_tests(tenant_id=tenant_id, agent_id=ma_agent_id)
    handler = _make_recording_ma_handler_with_agents_and_update([agent_payload], calls)
    runtime = _build_runtime_with_db(db_session_factory, anthropic_handler=handler)

    client_fake: Any = fake_slack_web_client

    await run_edit_agent_submission(
        runtime,
        client_fake.client,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        view_id="V_SUBMIT_TEST",
        agent_name=_AGENT_NAME,
        parent_section="agent",
        extra={"model": None, "system": "Attempted change."},
    )

    writes = [c for c in calls if c[0] != "GET"]
    assert writes == [], "a refused edit must never write to the MA"

    texts = _ephemeral_texts(client_fake)
    assert len(texts) == 1, "exactly one ephemeral should be posted on refusal"
    assert "workspace-admin" in texts[0], (
        "the refusal ephemeral should name why the write was refused"
    )


@pytest.mark.asyncio
async def test_run_add_skill_submission_when_non_admin_and_unreachable_proceeds(
    fake_slack_web_client: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adding a skill is open when nobody has scoped this agent."""
    calls: list[str] = []

    async def fake_kickoff(
        runtime: Any, *, tenant_id: Any, account_id: Any, agent_name: str, repo_url: str
    ) -> SyncReport:
        calls.append(repo_url)
        return SyncReport(synced=1)

    monkeypatch.setattr(submit_mod, "kick_off_skill_sync", fake_kickoff)

    runtime = _build_runtime_with_db(db_session_factory)
    client_fake: Any = fake_slack_web_client
    # conftest default users.info is non-admin; no override.

    await run_add_skill_submission(
        runtime,
        client_fake.client,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        view_id="V_SUBMIT_TEST",
        agent_name=_AGENT_NAME,
        parent_section="skills",
        extra={"repo_url": "https://github.com/example/skills.git", "branch": "main"},
    )

    assert calls == ["https://github.com/example/skills.git"], (
        "an unreachable agent's add-skill must reach kick_off_skill_sync"
    )

    texts = _ephemeral_texts(client_fake)
    assert any(":white_check_mark:" in t for t in texts), (
        "add-skill must succeed for a non-admin on an unreachable agent"
    )


@pytest.mark.asyncio
async def test_run_add_skill_submission_when_non_admin_and_reachable_refused_queues_no_sync(
    fake_slack_web_client: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-admin adding a skill to a currently-default agent must be
    refused before the sync is queued."""

    async def unexpected_kickoff(*args: Any, **kwargs: Any) -> SyncReport:
        raise AssertionError("skill sync must not be kicked off when the write is refused")

    monkeypatch.setattr(submit_mod, "kick_off_skill_sync", unexpected_kickoff)

    async with db_session_factory() as session:
        tenant = await make_tenant(session, platform="slack", workspace_id=_TEAM_ID)
        tenant_id = tenant.id
        await session.commit()
    await _mark_reachable(db_session_factory, tenant_id=tenant_id, agent_name=_AGENT_NAME)

    runtime = _build_runtime_with_db(db_session_factory)
    client_fake: Any = fake_slack_web_client

    await run_add_skill_submission(
        runtime,
        client_fake.client,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        view_id="V_SUBMIT_TEST",
        agent_name=_AGENT_NAME,
        parent_section="skills",
        extra={"repo_url": "https://github.com/example/skills.git", "branch": "main"},
    )

    texts = _ephemeral_texts(client_fake)
    assert len(texts) == 1, "exactly one ephemeral should be posted on refusal, no sync queued"
    assert "workspace-admin" in texts[0], (
        "the refusal ephemeral should name why the write was refused"
    )


@pytest.mark.asyncio
async def test_run_add_mcp_submission_when_non_admin_and_unreachable_proceeds(
    fake_slack_web_client: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Adding an MCP server is open when nobody has scoped this agent."""
    from daimon.core.ma_identity import derive_tenant_uuid

    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=_TEAM_ID)
    ma_agent_id = f"agent_{'j' * 24}"
    calls: list[tuple[str, str]] = []
    agent_payload = _agent_payload_for_gate_tests(tenant_id=tenant_id, agent_id=ma_agent_id)
    handler = _make_recording_ma_handler_with_agents_and_update([agent_payload], calls)
    runtime = _build_runtime_with_db(db_session_factory, anthropic_handler=handler)

    client_fake: Any = fake_slack_web_client
    # conftest default users.info is non-admin; no override.

    await run_add_mcp_submission(
        runtime,
        client_fake.client,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        view_id="V_SUBMIT_TEST",
        agent_name=_AGENT_NAME,
        parent_section="mcps",
        extra={
            "mcp_name": "an-mcp",
            "mcp_url": "https://mcp.example.com",
            "token": None,
            "token_replace": False,
        },
    )

    assert any(method in {"PATCH", "POST"} for method, path in calls if "/v1/agents/" in path), (
        "an unreachable agent's add-mcp must reach the MA update"
    )

    texts = _ephemeral_texts(client_fake)
    assert any(":white_check_mark:" in t for t in texts), (
        "add-mcp must succeed for a non-admin on an unreachable agent"
    )


@pytest.mark.asyncio
async def test_run_add_mcp_submission_when_non_admin_and_reachable_refused(
    fake_slack_web_client: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A non-admin adding an MCP server to a currently-default agent must be
    refused before any MA request."""
    async with db_session_factory() as session:
        tenant = await make_tenant(session, platform="slack", workspace_id=_TEAM_ID)
        tenant_id = tenant.id
        await session.commit()
    await _mark_reachable(db_session_factory, tenant_id=tenant_id, agent_name=_AGENT_NAME)

    ma_agent_id = f"agent_{'k' * 24}"
    calls: list[tuple[str, str]] = []
    agent_payload = _agent_payload_for_gate_tests(tenant_id=tenant_id, agent_id=ma_agent_id)
    handler = _make_recording_ma_handler_with_agents_and_update([agent_payload], calls)
    runtime = _build_runtime_with_db(db_session_factory, anthropic_handler=handler)

    client_fake: Any = fake_slack_web_client

    await run_add_mcp_submission(
        runtime,
        client_fake.client,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        view_id="V_SUBMIT_TEST",
        agent_name=_AGENT_NAME,
        parent_section="mcps",
        extra={
            "mcp_name": "an-mcp",
            "mcp_url": "https://mcp.example.com",
            "token": None,
            "token_replace": False,
        },
    )

    writes = [c for c in calls if c[0] != "GET"]
    assert writes == [], "a refused add-mcp must never write to the MA"

    texts = _ephemeral_texts(client_fake)
    assert len(texts) == 1, "exactly one ephemeral should be posted on refusal"
    assert "workspace-admin" in texts[0], (
        "the refusal ephemeral should name why the write was refused"
    )


@pytest.mark.asyncio
async def test_run_add_mcp_submission_when_admin_and_reachable_proceeds(
    fake_slack_web_client: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """An admin caller is unaffected by reachability on a field-conditional branch."""
    async with db_session_factory() as session:
        tenant = await make_tenant(session, platform="slack", workspace_id=_TEAM_ID)
        tenant_id = tenant.id
        await session.commit()
    await _mark_reachable(db_session_factory, tenant_id=tenant_id, agent_name=_AGENT_NAME)

    ma_agent_id = f"agent_{'l' * 24}"
    calls: list[tuple[str, str]] = []
    agent_payload = _agent_payload_for_gate_tests(tenant_id=tenant_id, agent_id=ma_agent_id)
    handler = _make_recording_ma_handler_with_agents_and_update([agent_payload], calls)
    runtime = _build_runtime_with_db(db_session_factory, anthropic_handler=handler)

    client_fake: Any = fake_slack_web_client
    _override_users_info_admin(client_fake.mock)

    await run_add_mcp_submission(
        runtime,
        client_fake.client,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        view_id="V_SUBMIT_TEST",
        agent_name=_AGENT_NAME,
        parent_section="mcps",
        extra={
            "mcp_name": "an-mcp",
            "mcp_url": "https://mcp.example.com",
            "token": None,
            "token_replace": False,
        },
    )

    assert any(method in {"PATCH", "POST"} for method, path in calls if "/v1/agents/" in path), (
        "an admin's add-mcp must still reach the MA update even on a reachable agent"
    )

    texts = _ephemeral_texts(client_fake)
    assert any(":white_check_mark:" in t for t in texts), (
        "add-mcp must succeed for an admin regardless of reachability"
    )


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
    from daimon.testing.factories import make_tenant

    # We need a Tenant row so put_agent_file's FK resolves. Seed it via a
    # one-shot session from the factory (per-test schema isolation is active).
    async with db_session_factory() as session:
        tenant = await make_tenant(session, platform="slack", workspace_id=_TEAM_ID)
        tenant_id = tenant.id
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
        billing_config=None,
        http_client=_build_http_client(),
        resolver_cache=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
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
# run_edit_repo_submission — GitHub access proof gates every binding write
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


def _build_edit_repo_settings(*, app_id: str | None, fernet_key: str | None = None) -> MagicMock:
    """`fernet_key` defaults to a freshly generated key, not the literal string
    "dummy" — any test whose submit now reaches `load_agent_inline_pat` needs a
    real Fernet key or the fernet build raises."""
    settings: MagicMock = MagicMock()
    settings.crypto.keys = (SecretStr(fernet_key or Fernet.generate_key().decode()),)
    settings.mcp.public_url = None
    settings.mcp.jwt_secret = None
    settings.github = MagicMock()
    settings.github.app_id = app_id
    settings.github.oauth_scopes = ("repo",)
    return settings


def _build_edit_repo_runtime(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: Any,
    ma_agent_id: str,
    github_handler: Callable[[httpx.Request], httpx.Response] | None = None,
    fernet_key: str | None = None,
    app_id: str | None = None,
) -> SlackRuntime:
    """Build a SlackRuntime wired for run_edit_repo_submission tests: real MA
    transport for the agent lookup, real Postgres, and a real httpx client
    over MockTransport for the GitHub probe(s).

    `github_handler` defaults to one that fails the test if a probe is ever
    made — declaring the expected response per test is the point (a
    MagicMock client would instead make every probe return a truthy mock and
    silently pass every check).
    """
    return SlackRuntime(
        settings=_build_edit_repo_settings(app_id=app_id, fernet_key=fernet_key),
        anthropic=build_fake_anthropic(
            _build_edit_repo_ma_handler(tenant_id=tenant_id, ma_agent_id=ma_agent_id)
        ),
        sessionmaker=db_session_factory,
        billing_config=None,
        http_client=_build_http_client(github_handler),
        resolver_cache=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
        turn_deps=MagicMock(),  # pyright: ignore[reportArgumentType]  # stub, turn path not exercised
    )


async def _seed_stored_pat(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    agent_id: Any,
    fernet_key: str,
    plaintext: str,
) -> None:
    """Write a real per-agent credential + overlay binding — the same
    primitives `store_inline_pat` uses — so `load_agent_inline_pat` resolves
    it independently of whatever string the binding's own `ma_secret_ref`
    carries."""
    from daimon.core.github_credentials import build_multifernet, upsert_credential_encrypted
    from daimon.core.stores.agent_github_binding import set_agent_github_binding

    fernet = build_multifernet((fernet_key,))
    await upsert_credential_encrypted(
        sessionmaker=db_session_factory,
        fernet=fernet,
        principal_id=agent_id,
        github_login="(inline-pat)",
        plaintext_token=plaintext,
        scopes=("repo",),
    )
    async with db_session_factory() as session, session.begin():
        await set_agent_github_binding(session, agent_id=agent_id, principal_id=agent_id)


def _ephemeral_texts_for(client_fake: Any) -> list[str]:
    ephemeral_key = ("POST", yarl.URL("https://slack.com/api/chat.postEphemeral"))
    return [
        call.kwargs["json"]["text"] for call in client_fake.mock.requests.get(ephemeral_key, [])
    ]


async def _seed_guild_account(
    db_session_factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: Any,
) -> None:
    """Seed the accounts row `proof_account_id`'s FK requires for a proof
    attributed to the derived guild account (the submitting workspace's
    account for a panel-driven bind)."""
    from daimon.core.defaults.provisioning import derive_guild_account_uuid
    from daimon.core.stores.tenants import get_tenant

    async with db_session_factory() as session:
        tenant_row = await get_tenant(session, tenant_id)
        await make_account(
            session, tenant=tenant_row, id=derive_guild_account_uuid(tenant_id=tenant_id)
        )
        await session.commit()


@pytest.mark.asyncio
async def test_run_edit_repo_submission_blank_pat_binds_anon_when_repo_public(
    fake_slack_web_client: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """First-time blank-token bind against a verified-public repo writes an
    anon: binding with a public proof and reports success.

    Replaces the old premise ("no App-coverage probe on Slack, so an anon:
    binding is written unconditionally") — that gap is exactly what this
    phase closes; the write now requires a real is_public_repo probe.
    """
    from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
    from daimon.core.stores.agent_repo_binding import get_binding

    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=_TEAM_ID)
    ma_agent_id = f"agent_{'e' * 24}"
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)

    async with db_session_factory() as session:
        await make_tenant(session, platform="slack", workspace_id=_TEAM_ID, id=tenant_id)
        await session.commit()
    await _seed_guild_account(db_session_factory, tenant_id=tenant_id)

    client_fake: Any = fake_slack_web_client
    _override_users_info_admin(client_fake.mock)

    runtime = _build_edit_repo_runtime(
        db_session_factory,
        tenant_id=tenant_id,
        ma_agent_id=ma_agent_id,
        github_handler=_github_handler(200, {"private": False}),
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
            "repo_url": "https://github.com/example/verified-public.git",
            "pat": "",
            "pat_replace": False,
        },
    )

    async with db_session_factory() as session:
        row = await get_binding(session, tenant_id=tenant_id, agent_id=agent_uuid)

    assert row is not None, "binding should exist after a verified-public first-time bind"
    assert row.ma_secret_ref == "anon:", "a verified-public no-PAT bind writes an anon: binding"
    assert row.proof_kind == "public", "the binding must record a public proof"
    assert row.proof_at is not None, "proof_at must be recorded"

    texts = _ephemeral_texts_for(client_fake)
    assert any("Saved repo + auth" in t for t in texts), (
        "user should see the plain save confirmation"
    )
    assert not any("App-covered" in t for t in texts), (
        "Slack panel must never advertise App coverage as proof of access"
    )


@pytest.mark.asyncio
async def test_run_edit_repo_submission_blank_pat_no_stored_token_and_repo_private_refuses_leaving_no_proof(
    fake_slack_web_client: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Blank token, no stored credential, and a private repo: the bind must
    be refused, not silently written as anon:."""
    from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
    from daimon.core.stores.agent_repo_binding import get_binding

    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=_TEAM_ID)
    ma_agent_id = f"agent_{'r' * 24}"
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)

    async with db_session_factory() as session:
        await make_tenant(session, platform="slack", workspace_id=_TEAM_ID, id=tenant_id)
        await session.commit()

    client_fake: Any = fake_slack_web_client
    _override_users_info_admin(client_fake.mock)

    runtime = _build_edit_repo_runtime(
        db_session_factory,
        tenant_id=tenant_id,
        ma_agent_id=ma_agent_id,
        github_handler=_github_handler(200, {"private": True}),
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
            "repo_url": "https://github.com/example/private-no-token.git",
            "pat": "",
            "pat_replace": False,
        },
    )

    async with db_session_factory() as session:
        row = await get_binding(session, tenant_id=tenant_id, agent_id=agent_uuid)
    assert row is None, "a private repo with no credential must refuse the bind"

    texts = _ephemeral_texts_for(client_fake)
    assert any("token" in t.lower() for t in texts), (
        "refusal must name the fix: a token is required to bind a private repo"
    )


@pytest.mark.asyncio
async def test_run_edit_repo_submission_blank_pat_no_stored_token_and_repo_missing_refuses_leaving_no_proof(
    fake_slack_web_client: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A 404 from the public-visibility probe (nonexistent or hidden repo)
    refuses the bind identically to an explicitly private repo."""
    from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
    from daimon.core.stores.agent_repo_binding import get_binding

    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=_TEAM_ID)
    ma_agent_id = f"agent_{'s' * 24}"
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)

    async with db_session_factory() as session:
        await make_tenant(session, platform="slack", workspace_id=_TEAM_ID, id=tenant_id)
        await session.commit()

    client_fake: Any = fake_slack_web_client
    _override_users_info_admin(client_fake.mock)

    runtime = _build_edit_repo_runtime(
        db_session_factory,
        tenant_id=tenant_id,
        ma_agent_id=ma_agent_id,
        github_handler=_github_handler(404),
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
            "repo_url": "https://github.com/example/does-not-exist.git",
            "pat": "",
            "pat_replace": False,
        },
    )

    async with db_session_factory() as session:
        row = await get_binding(session, tenant_id=tenant_id, agent_id=agent_uuid)
    assert row is None, "a 404 from the visibility probe must refuse the bind"

    texts = _ephemeral_texts_for(client_fake)
    assert any(":x:" in t for t in texts), "a refusal ephemeral must be posted"


@pytest.mark.asyncio
async def test_run_edit_repo_submission_blank_pat_stored_token_covers_new_repo_binds_with_pat_proof(
    fake_slack_web_client: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Blank token, but the agent already has a stored token that can read
    the newly-typed repo: the binding is written under that token's ref with
    a pat proof."""
    from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
    from daimon.core.stores.agent_repo_binding import get_binding

    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=_TEAM_ID)
    ma_agent_id = f"agent_{'t' * 24}"
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)
    fernet_key = Fernet.generate_key().decode()

    async with db_session_factory() as session:
        await make_tenant(session, platform="slack", workspace_id=_TEAM_ID, id=tenant_id)
        await session.commit()
    await _seed_guild_account(db_session_factory, tenant_id=tenant_id)
    await _seed_stored_pat(
        db_session_factory,
        agent_id=agent_uuid,
        fernet_key=fernet_key,
        plaintext="ghp_stored_covers_new_repo",
    )

    client_fake: Any = fake_slack_web_client
    _override_users_info_admin(client_fake.mock)

    runtime = _build_edit_repo_runtime(
        db_session_factory,
        tenant_id=tenant_id,
        ma_agent_id=ma_agent_id,
        fernet_key=fernet_key,
        github_handler=_github_handler(200),
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
            "repo_url": "https://github.com/example/stored-token-covers.git",
            "pat": "",
            "pat_replace": False,
        },
    )

    async with db_session_factory() as session:
        row = await get_binding(session, tenant_id=tenant_id, agent_id=agent_uuid)

    assert row is not None, "a stored token that covers the repo must write the binding"
    assert row.ma_secret_ref == f"inline-pat:{agent_uuid}", (
        "the stored token's ref must be used for the write"
    )
    assert row.proof_kind == "pat", "proof must record the stored token as the access proof"
    assert row.proof_at is not None, "proof_at must be recorded"


@pytest.mark.asyncio
async def test_run_edit_repo_submission_blank_pat_stored_token_cannot_access_new_repo_refuses_leaving_binding_unchanged(
    fake_slack_web_client: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Blank token, and the agent's stored token cannot read the newly-typed
    repo: the submit is refused and the pre-existing binding's repo_url is
    left untouched — no silent fall-through to a probe of a different repo."""
    from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
    from daimon.core.stores.agent_repo_binding import get_binding, set_binding

    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=_TEAM_ID)
    ma_agent_id = f"agent_{'u' * 24}"
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)
    fernet_key = Fernet.generate_key().decode()

    async with db_session_factory() as session:
        await make_tenant(session, platform="slack", workspace_id=_TEAM_ID, id=tenant_id)
        await session.commit()
    await _seed_stored_pat(
        db_session_factory,
        agent_id=agent_uuid,
        fernet_key=fernet_key,
        plaintext="ghp_stored_cannot_access_new_repo",
    )
    async with db_session_factory() as session:
        await set_binding(
            session,
            tenant_id=tenant_id,
            agent_id=agent_uuid,
            repo_url="https://github.com/example/old-repo.git",
            default_branch="main",
            ma_secret_ref=f"inline-pat:{agent_uuid}",
            proof=None,
        )
        await session.commit()

    client_fake: Any = fake_slack_web_client
    _override_users_info_admin(client_fake.mock)

    runtime = _build_edit_repo_runtime(
        db_session_factory,
        tenant_id=tenant_id,
        ma_agent_id=ma_agent_id,
        fernet_key=fernet_key,
        github_handler=_github_handler(404),
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
            "repo_url": "https://github.com/example/new-repo-denied.git",
            "pat": "",
            "pat_replace": False,
        },
    )

    async with db_session_factory() as session:
        row = await get_binding(session, tenant_id=tenant_id, agent_id=agent_uuid)

    assert row is not None, "the pre-existing binding must survive a refused re-point"
    assert row.repo_url == "example/old-repo", "repo_url must remain the OLD value after refusal"
    assert row.proof_kind is None, "a refused re-point must not record any new proof"

    texts = _ephemeral_texts_for(client_fake)
    assert any("stored" in t.lower() for t in texts), (
        "refusal must name the fix: the stored token cannot read the new repo"
    )


@pytest.mark.asyncio
async def test_run_edit_repo_submission_blank_pat_repoints_vault_credential_ref_preserved_and_records_pat_proof(
    fake_slack_web_client: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Blank token re-pointing an existing binding whose ma_secret_ref is
    neither inline-pat: nor anon: (a vault-issued credential id) with a
    stored token that can read the new repo: ma_secret_ref survives
    verbatim, repo_url moves to the new value, and a fresh pat proof is
    recorded — the keep-secret write and the proof re-establishment happen
    in the same transaction."""
    from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
    from daimon.core.stores.agent_repo_binding import get_binding, set_binding

    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=_TEAM_ID)
    ma_agent_id = f"agent_{'v' * 24}"
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)
    fernet_key = Fernet.generate_key().decode()
    vault_ref = "vault-cred-99"

    async with db_session_factory() as session:
        await make_tenant(session, platform="slack", workspace_id=_TEAM_ID, id=tenant_id)
        await session.commit()
    await _seed_guild_account(db_session_factory, tenant_id=tenant_id)
    await _seed_stored_pat(
        db_session_factory,
        agent_id=agent_uuid,
        fernet_key=fernet_key,
        plaintext="ghp_vault_repoint_scenario",
    )
    async with db_session_factory() as session:
        seeded = await set_binding(
            session,
            tenant_id=tenant_id,
            agent_id=agent_uuid,
            repo_url="https://github.com/example/vault-old.git",
            default_branch="main",
            ma_secret_ref=vault_ref,
            proof=None,
        )
        await session.commit()
    assert seeded.proof_kind is None, "the seeded binding must start with no proof recorded"

    client_fake: Any = fake_slack_web_client
    _override_users_info_admin(client_fake.mock)

    runtime = _build_edit_repo_runtime(
        db_session_factory,
        tenant_id=tenant_id,
        ma_agent_id=ma_agent_id,
        fernet_key=fernet_key,
        github_handler=_github_handler(200),
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
            "repo_url": "https://github.com/example/vault-new.git",
            "pat": "",
            "pat_replace": False,
        },
    )

    async with db_session_factory() as session:
        row = await get_binding(session, tenant_id=tenant_id, agent_id=agent_uuid)

    assert row is not None, "binding should still exist after the re-point"
    assert row.ma_secret_ref == vault_ref, (
        "ma_secret_ref written by another surface must be preserved verbatim, not clobbered"
    )
    assert row.repo_url == "example/vault-new", "repo_url must move to the newly-typed repo"
    assert row.proof_kind == "pat", "proof must be re-established against the NEW repo"
    assert row.proof_at is not None, "proof_at must be recorded"


@pytest.mark.asyncio
async def test_run_edit_repo_submission_pasted_token_cannot_access_repo_refuses_and_stores_no_credential(
    fake_slack_web_client: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A pasted token that cannot read the target repo is refused before
    either the credential is stored or a binding is written."""
    from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
    from daimon.core.stores.agent_repo_binding import get_binding

    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=_TEAM_ID)
    ma_agent_id = f"agent_{'w' * 24}"
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)
    fernet_key = Fernet.generate_key().decode()

    async with db_session_factory() as session:
        await make_tenant(session, platform="slack", workspace_id=_TEAM_ID, id=tenant_id)
        await session.commit()

    client_fake: Any = fake_slack_web_client
    _override_users_info_admin(client_fake.mock)

    runtime = _build_edit_repo_runtime(
        db_session_factory,
        tenant_id=tenant_id,
        ma_agent_id=ma_agent_id,
        fernet_key=fernet_key,
        github_handler=_github_handler(404),
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
            "repo_url": "https://github.com/example/pasted-denied.git",
            "pat": "ghp_junk_denied",
            "pat_replace": True,
        },
    )

    async with db_session_factory() as session:
        row = await get_binding(session, tenant_id=tenant_id, agent_id=agent_uuid)
    assert row is None, "a refused pasted token must write no binding"

    from daimon.adapters.slack.agent_setup.write import load_agent_inline_pat

    stored = await load_agent_inline_pat(runtime, agent_id=agent_uuid)
    assert stored is None, "a refused pasted token must never be stored as a credential"

    texts = _ephemeral_texts_for(client_fake)
    assert any("token" in t.lower() for t in texts), (
        "refusal must name the fix: paste a token that has access"
    )


@pytest.mark.asyncio
async def test_run_edit_repo_submission_when_pat_replace_true_stores_new_inline_pat_and_pat_proof(
    fake_slack_web_client: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """pat_replace=True + a typed PAT that CAN read the repo: stores the
    token, binds the repo, and records a pat proof attributed to the
    submitting workspace's guild account."""
    from daimon.core.defaults.provisioning import derive_guild_account_uuid
    from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
    from daimon.core.stores.agent_repo_binding import get_binding

    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=_TEAM_ID)
    ma_agent_id = f"agent_{'d' * 24}"
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)

    async with db_session_factory() as session:
        await make_tenant(session, platform="slack", workspace_id=_TEAM_ID, id=tenant_id)
        await session.commit()
    await _seed_guild_account(db_session_factory, tenant_id=tenant_id)

    client_fake: Any = fake_slack_web_client
    _override_users_info_admin(client_fake.mock)

    fernet_key = Fernet.generate_key().decode()
    runtime = _build_edit_repo_runtime(
        db_session_factory,
        tenant_id=tenant_id,
        ma_agent_id=ma_agent_id,
        fernet_key=fernet_key,
        github_handler=_github_handler(200),
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
    assert row.proof_kind == "pat", "a pasted-token bind must record a pat proof"
    assert row.proof_account_id == derive_guild_account_uuid(tenant_id=tenant_id), (
        "proof must attribute the submitting workspace's guild account"
    )


@pytest.mark.asyncio
async def test_run_edit_repo_submission_pasted_token_with_no_repo_url_stores_token_and_writes_no_binding(
    fake_slack_web_client: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A pasted token with no repo_url is stored (unchanged behavior) and
    writes no binding — there is no repo to check access against yet."""
    from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
    from daimon.core.stores.agent_repo_binding import get_binding

    tenant_id = derive_tenant_uuid(platform="slack", workspace_id=_TEAM_ID)
    ma_agent_id = f"agent_{'x' * 24}"
    agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=ma_agent_id)
    fernet_key = Fernet.generate_key().decode()

    async with db_session_factory() as session:
        await make_tenant(session, platform="slack", workspace_id=_TEAM_ID, id=tenant_id)
        await session.commit()

    client_fake: Any = fake_slack_web_client
    _override_users_info_admin(client_fake.mock)

    runtime = _build_edit_repo_runtime(
        db_session_factory,
        tenant_id=tenant_id,
        ma_agent_id=ma_agent_id,
        fernet_key=fernet_key,
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
            "repo_url": None,
            "pat": "ghp_no_repo_token",
            "pat_replace": True,
        },
    )

    async with db_session_factory() as session:
        row = await get_binding(session, tenant_id=tenant_id, agent_id=agent_uuid)
    assert row is None, "no repo_url means no binding should be written"

    from daimon.adapters.slack.agent_setup.write import load_agent_inline_pat

    stored = await load_agent_inline_pat(runtime, agent_id=agent_uuid)
    assert stored == "ghp_no_repo_token", "the pasted token must still be stored with no repo bound"
