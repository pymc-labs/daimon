"""Tests for agent_setup/submit.py.

One form reaches this module — the panel's New agent form — so the coverage
is the evaluator's field validation pre-ack and the background create after
it: where the person lands, what a member is told about routing, what comes
back after a name collision, and that nothing here opens a billed session.

The run_* tests drive a real FakeSlackWebClient and a real Postgres schema.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import yarl
from cryptography.fernet import Fernet
from daimon.adapters.slack.agent_setup.state import (
    PanelMetadata,
    decode_panel_metadata,
    encode_panel_metadata,
)
from daimon.adapters.slack.agent_setup.submit import (
    SubmitDecision,
    evaluate_new_agent_submission,
    run_new_agent_submission,
)
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.testing.ma import build_fake_anthropic, make_fake_ma_handler
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# ---------------------------------------------------------------------------
# Helpers for building minimal Slack view_submission payloads
# ---------------------------------------------------------------------------

_TEAM_ID = "T_TEST"
_USER_ID = "U_TEST"
_CHANNEL_ID = "C_TEST"
_ROOT_VIEW_ID = "V_PANEL_ROOT"
_FORM_VIEW_ID = "V_NEW_AGENT_FORM"


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


def _input_value(block_id: str, action_id: str, value: str) -> dict[str, Any]:
    """Build a minimal state.values entry for a plain_text_input."""
    return {block_id: {action_id: {"type": "plain_text_input", "value": value}}}


def _select_value(block_id: str, action_id: str, value: str) -> dict[str, Any]:
    """Build a minimal state.values entry for a static_select's selected_option."""
    return {
        block_id: {
            action_id: {
                "type": "static_select",
                "selected_option": {"text": {"type": "plain_text", "text": value}, "value": value},
            }
        }
    }


def _panel_meta(**overrides: Any) -> PanelMetadata:
    """The metadata the New agent form carries when the panel pushes it."""
    base: dict[str, Any] = {
        "team_id": _TEAM_ID,
        "channel_id": _CHANNEL_ID,
        "view": "new_agent",
        "root_view_id": _ROOT_VIEW_ID,
    }
    base.update(overrides)
    return PanelMetadata(**base)


def _panel_payload(
    *,
    values: dict[str, Any],
    meta: PanelMetadata | None = None,
    user_id: str = _USER_ID,
) -> dict[str, Any]:
    """A view_submission from the panel's own New agent form."""
    return {
        "user": {"id": user_id},
        "view": {
            "callback_id": "agent_setup__new_agent",
            "id": _FORM_VIEW_ID,
            "private_metadata": encode_panel_metadata(meta if meta is not None else _panel_meta()),
            "state": {"values": values},
        },
    }


# ---------------------------------------------------------------------------
# Pure evaluator tests — evaluate_new_agent_submission
# ---------------------------------------------------------------------------


def test_evaluate_new_agent_submission_when_name_invalid_returns_errors_keyed_new_agent_name() -> (
    None
):
    values = _input_value("new_agent__name", "new_agent__name", "bad name!")  # spaces + bang
    payload = _panel_payload(values=values)

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


def test_evaluate_new_agent_returns_update_to_creating_view() -> None:
    values = {
        **_input_value("new_agent__name", "new_agent__name", "my-agent"),
        **_select_value("new_agent__model", "new_agent__model", "claude-sonnet-5"),
    }
    payload = _panel_payload(values=values)

    decision = evaluate_new_agent_submission(payload)

    assert decision.proceed is True, "valid name should proceed"
    assert decision.response_payload.get("response_action") == "update", (
        "the form becomes the creating view rather than closing the panel"
    )
    acked_meta = decode_panel_metadata(decision.response_payload["view"]["private_metadata"])
    assert acked_meta is not None and acked_meta.view == "creating", (
        "the acked view says what is happening and keeps the panel's state"
    )
    assert decision.panel_meta is not None and decision.panel_meta.root_view_id == _ROOT_VIEW_ID, (
        "the background run needs the root view id to refresh the list behind the form"
    )
    assert decision.extra.get("name") == "my-agent", "name should be carried to extra"
    assert decision.extra.get("model") == "claude-sonnet-5", (
        "the selected option's value must be carried to extra"
    )


def test_evaluate_new_agent_submission_when_model_invalid_returns_errors_keyed_new_agent_model() -> (
    None
):
    """A stale client can still submit a retired/unknown model id via the select."""
    values = {
        **_input_value("new_agent__name", "new_agent__name", "valid-name"),
        **_select_value("new_agent__model", "new_agent__model", "gpt-4-turbo"),
    }
    payload = _panel_payload(values=values)

    decision = evaluate_new_agent_submission(payload)

    assert decision.proceed is False, "unknown model should not proceed"
    errors = decision.response_payload.get("errors", {})
    assert "new_agent__model" in errors, (
        "error must be keyed to new_agent__model (the input block_id)"
    )


def test_evaluate_new_agent_submission_when_model_missing_returns_errors_keyed_new_agent_model() -> (
    None
):
    """The model select always carries an initial_option in production, but the
    evaluator must not silently fall back to a default if a value is somehow absent."""
    values = _input_value("new_agent__name", "new_agent__name", "valid-name")
    payload = _panel_payload(values=values)

    decision = evaluate_new_agent_submission(payload)

    assert decision.proceed is False, "a missing model selection must not proceed"
    errors = decision.response_payload.get("errors", {})
    assert "new_agent__model" in errors


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


def _ephemeral_texts(client_fake: Any) -> list[str]:
    ephemeral_key = ("POST", yarl.URL("https://slack.com/api/chat.postEphemeral"))
    return [
        call.kwargs["json"]["text"] for call in client_fake.mock.requests.get(ephemeral_key, [])
    ]


# ---------------------------------------------------------------------------
# The background create
# ---------------------------------------------------------------------------


def _views(client_fake: Any, method: str) -> list[dict[str, Any]]:
    key = ("POST", yarl.URL(f"https://slack.com/api/{method}"))
    return [dict(call.kwargs["json"]) for call in client_fake.mock.requests.get(key, [])]


@pytest.mark.asyncio
async def test_run_new_agent_when_created_updates_same_view_to_details_and_refreshes_root_page(
    fake_slack_web_client: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Creation lands on the new agent's Details, with the list behind it fresh.

    Two updates and no ephemeral: the view the form became shows what was
    created, and the root view is re-rendered so the new row is there when the
    person goes back. Creation stays open to every member — the conftest
    users.info default is a non-admin and nothing here refuses it.
    """
    client_fake: Any = fake_slack_web_client
    runtime = _build_runtime_with_db(db_session_factory, fernet_key=Fernet.generate_key().decode())

    await run_new_agent_submission(
        runtime,
        client_fake.client,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        view_id=_FORM_VIEW_ID,
        meta=_panel_meta(),
        name="churn-explorer",
        purpose="Explain churn",
        model="claude-sonnet-4-6",
    )

    updates = _views(client_fake, "views.update")
    assert [call["view_id"] for call in updates] == [_FORM_VIEW_ID, _ROOT_VIEW_ID], (
        "the form's own view becomes Details, then the root list is refreshed"
    )
    details_meta = decode_panel_metadata(updates[0]["view"]["private_metadata"])
    assert details_meta is not None, "the Details view carries typed panel metadata"
    assert (details_meta.view, details_meta.agent_name) == ("details", "churn-explorer"), (
        "the created agent is the one shown"
    )
    assert not _ephemeral_texts(client_fake), (
        "the outcome is the view itself; no success ephemeral beside it"
    )


@pytest.mark.asyncio
async def test_run_new_agent_when_member_details_carries_admin_routing_sentence(
    fake_slack_web_client: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A new agent answers nowhere, and a member is told who can change that.

    "Created" must not read as "available by mention", and the next step for a
    member is asking an admin rather than a control they do not have.
    """
    client_fake: Any = fake_slack_web_client
    runtime = _build_runtime_with_db(db_session_factory, fernet_key=Fernet.generate_key().decode())

    await run_new_agent_submission(
        runtime,
        client_fake.client,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        view_id=_FORM_VIEW_ID,
        meta=_panel_meta(),
        name="unrouted-agent",
        purpose=None,
        model="claude-sonnet-4-6",
    )

    details = json.dumps(_views(client_fake, "views.update")[0]["view"])
    assert "Not answering in any channel yet" in details, (
        "a just-created agent says it is not reachable yet"
    )
    assert "An admin can" in details, "a member is given the admin handoff, not a control"


@pytest.mark.asyncio
async def test_run_new_agent_when_name_collides_restores_form_with_inputs_and_banner(
    fake_slack_web_client: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A collision brings the form back with what was typed and why it failed.

    The form is the only place the person can fix the name, and re-typing a
    purpose they already wrote is the kind of loss this panel exists to avoid.
    """
    client_fake: Any = fake_slack_web_client
    runtime = _build_runtime_with_db(db_session_factory, fernet_key=Fernet.generate_key().decode())

    common: dict[str, Any] = {
        "team_id": _TEAM_ID,
        "user_id": _USER_ID,
        "channel_id": _CHANNEL_ID,
        "meta": _panel_meta(),
        "name": "collide-agent",
        "purpose": "Explain churn",
        "model": "claude-sonnet-4-6",
    }
    await run_new_agent_submission(runtime, client_fake.client, view_id="V1", **common)
    before = len(_views(client_fake, "views.update"))
    await run_new_agent_submission(runtime, client_fake.client, view_id="V2", **common)

    restored = _views(client_fake, "views.update")[before:]
    assert len(restored) == 1, "a refused create re-renders the form and nothing else"
    assert restored[0]["view_id"] == "V2", "the form comes back where it was submitted from"
    rendered = json.dumps(restored[0]["view"])
    assert "already has an agent named" in rendered, "the banner says why it failed"
    assert "Explain churn" in rendered, "what was typed comes back with the form"
    restored_meta = decode_panel_metadata(restored[0]["view"]["private_metadata"])
    assert restored_meta is not None and restored_meta.root_view_id == _ROOT_VIEW_ID, (
        "the restored form still knows the root view for a later retry"
    )


@pytest.mark.asyncio
async def test_run_new_agent_never_consults_admission(
    fake_slack_web_client: Any,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Creating and inspecting stay available when billed turns cannot be.

    Nothing in this path opens a session, so nothing about the workspace's
    balance can stop someone creating an agent and reading its Details.
    """
    client_fake: Any = fake_slack_web_client
    paths: list[str] = []
    base = make_fake_ma_handler()

    def recording_handler(request: httpx.Request) -> httpx.Response:
        paths.append(f"{request.method} {request.url.path}")
        return base(request)

    runtime = _build_runtime_with_db(
        db_session_factory,
        fernet_key=Fernet.generate_key().decode(),
        anthropic_handler=recording_handler,
    )

    await run_new_agent_submission(
        runtime,
        client_fake.client,
        team_id=_TEAM_ID,
        user_id=_USER_ID,
        channel_id=_CHANNEL_ID,
        view_id=_FORM_VIEW_ID,
        meta=_panel_meta(),
        name="unbilled-agent",
        purpose=None,
        model="claude-sonnet-4-6",
    )

    assert paths, "the create really did reach the Managed Agents API"
    assert not any("/v1/sessions" in path for path in paths), (
        "creation opens no session, so nothing is admitted or billed"
    )
