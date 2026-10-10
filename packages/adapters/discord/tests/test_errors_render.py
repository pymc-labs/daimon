"""Known errors use plain copy without provider bodies or internal identifiers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import anthropic
import discord
import httpx
import pytest
from daimon.adapters.discord.errors import (
    NOT_SET_UP_NOTICE,
    SETUP_OUT_OF_DATE_NOTICE,
    error_lines,
    generate_request_id,
    render_error,
)
from daimon.core.channel_admins import InvalidChannelAdminIds
from daimon.core.channel_budget import ChannelBudgetError
from daimon.core.continuity.handoff import HandoffRefusedInSetupThread
from daimon.core.cron import InvalidScheduleError
from daimon.core.errors import (
    AgentNameCollision,
    DaimonError,
    SpecError,
    StoreError,
    TurnError,
    UserFacingError,
)
from daimon.core.github_repo_auth import resolve_clone_token
from daimon.core.notebooks.publish import NotebookRateLimitError
from daimon.core.setup_conversations import get_setup_responder
from daimon.core.stores.access_policy import AccessPolicyUnreadable
from daimon.core.stores.direct_messages import DirectMessageBusy
from daimon.core.stores.domain import AgentRepoBindingRow
from daimon.testing.ma import build_fake_anthropic
from sqlalchemy.exc import DBAPIError
from structlog.testing import capture_logs

TEST_RID = "01JTZXTEST0000000000000000"


REF = "-# Ref 000000"
OUR_SIDE = "Something went wrong on our side.\n\nTry again. If it keeps happening, tell an admin."


def _status_error(
    status: int, payload: dict[str, object] | None = None
) -> anthropic.APIStatusError:
    return anthropic.APIStatusError(
        message=f"Error code: {status} - "
        + "{'request_id': 'req_private', 'message': 'Overloaded'}",
        response=httpx.Response(
            status,
            request=httpx.Request("POST", "https://api.anthropic.com"),
            json=payload or {"request_id": "req_private"},
        ),
        body={"request_id": "req_private"},
    )


def test_known_local_errors_never_publish_exception_bodies() -> None:
    body = "sesn_private agent_private access_token=private SELECT secret"
    for error, expected in [
        (ChannelBudgetError(body), "That spending budget isn't valid."),
        (InvalidScheduleError(body), "That schedule isn't valid."),
        (InvalidChannelAdminIds(body), "That admin selection isn't valid."),
        (NotebookRateLimitError(body), "The notebook publishing limit has been reached."),
        (DirectMessageBusy(body), "A reply is still running."),
        (HandoffRefusedInSetupThread(body), "This setup conversation can't change agents."),
        (AgentNameCollision(body), "This workspace already has an agent with that name."),
    ]:
        result = render_error(error, request_id=TEST_RID)
        assert result.startswith(expected)
        assert "private" not in result
        assert "SELECT" not in result
        assert result.endswith(f"\n\n{REF}"), "fixed guidance keeps the ref line"


def test_everything_else_is_our_side_with_a_ref_and_no_body() -> None:
    body = "sesn_private agent_private access_token=private SELECT secret"
    for error in [
        SpecError(body),
        StoreError(body),
        DaimonError(body),
        ValueError(body),
        RuntimeError(body),
        DBAPIError("SELECT secret", {"token": "private"}, Exception(body)),
    ]:
        assert render_error(error, request_id=TEST_RID) == f"{OUR_SIDE}\n\n{REF}"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (429, "Daimon's AI service is busy.\n\nTry again in a minute."),
        (529, "Daimon's AI service is busy.\n\nTry again in a minute."),
        (500, "Daimon couldn't reach its AI service.\n\nTry again in a minute."),
        (503, "Daimon couldn't reach its AI service.\n\nTry again in a minute."),
        (400, "Daimon's AI service couldn't accept the request.\n\nAsk an admin to check it."),
        (401, "Daimon's AI service couldn't accept the request.\n\nAsk an admin to check it."),
        (403, "Daimon's AI service couldn't accept the request.\n\nAsk an admin to check it."),
        (404, "Daimon's AI service couldn't accept the request.\n\nAsk an admin to check it."),
    ],
)
def test_anthropic_failures_map_to_their_cause(status: int, expected: str) -> None:
    error = _status_error(status)
    assert render_error(error, request_id=TEST_RID) == f"{expected}\n\n{REF}"
    wrapped = TurnError(kind="upstream", cause=error)
    assert render_error(wrapped, request_id=TEST_RID) == f"{expected}\n\n{REF}"
    assert "req_private" not in render_error(error, request_id=TEST_RID)


def test_the_spend_limit_is_not_busy() -> None:
    error = _status_error(
        429,
        {
            "type": "error",
            "error": {
                "type": "rate_limit_error",
                "message": "private",
                "details": {"error_code": "enforced_spend_limit_reached"},
            },
        },
    )
    assert render_error(error, request_id=TEST_RID) == (
        "Daimon has reached its usage limit.\n\n"
        "Ask the team running it to check the limit.\n\n"
        f"{REF}"
    )


def test_connection_and_generic_api_errors_could_not_reach() -> None:
    request = httpx.Request("POST", "https://api.anthropic.com")
    expected = f"Daimon couldn't reach its AI service.\n\nTry again in a minute.\n\n{REF}"
    assert render_error(anthropic.APIConnectionError(request=request), request_id=TEST_RID) == (
        expected
    )
    assert render_error(anthropic.APITimeoutError(request=request), request_id=TEST_RID) == (
        expected
    )
    assert (
        render_error(anthropic.APIError("req_private", request, body=None), request_id=TEST_RID)
        == expected
    )


def test_other_discord_rejections_say_discord_did_not_accept() -> None:
    error = discord.HTTPException(MagicMock(status=400, reason="Bad Request"), "private")
    assert render_error(error, request_id=TEST_RID) == (
        f"Discord didn't accept that.\n\nTry again.\n\n{REF}"
    )


def test_the_ref_is_the_last_six_and_the_full_id_is_logged() -> None:
    rid = generate_request_id()
    with capture_logs() as logs:
        rendered = render_error(RuntimeError("private detail"), request_id=rid)
    assert rendered.endswith(f"\n\n-# Ref {rid[-6:]}")
    assert rid not in rendered
    assert "private" not in rendered
    [entry] = [e for e in logs if e["event"] == "error.rendered"]
    assert entry["rid"] == rid
    assert entry["ref"] == rid[-6:]
    assert isinstance(entry["exc_info"], RuntimeError)


def test_lines_are_separated_by_a_blank_line() -> None:
    for error in [RuntimeError("x"), _status_error(529)]:
        rendered = render_error(error, request_id=TEST_RID)
        assert "\n\n" in rendered
        assert "\n" not in rendered.replace("\n\n", ""), rendered


def test_without_a_request_id_there_is_no_ref() -> None:
    assert render_error(RuntimeError("x"), request_id="") == OUR_SIDE


def test_error_lines_carry_no_ref() -> None:
    assert error_lines(_status_error(529)) == (
        "Daimon's AI service is busy.",
        "Try again in a minute.",
    )
    assert error_lines(UserFacingError("Do this.")) == ("Do this.",)


def test_the_setup_notices_use_the_approved_words() -> None:
    assert NOT_SET_UP_NOTICE == (
        "Daimon isn't set up in this channel yet.\n\nAsk an admin to run `/agent-setup`."
    )
    assert SETUP_OUT_OF_DATE_NOTICE == (
        "This channel's setup is out of date.\n\nAsk an admin to check `/agent-setup`."
    )


def test_discord_existing_thread_error_is_plain_copy() -> None:
    response = MagicMock(status=400, reason="Bad Request")
    error = discord.HTTPException(
        response, {"code": 160004, "message": "A thread has already been created for this message"}
    )
    assert render_error(error, request_id=TEST_RID) == (
        f"A conversation already exists for this message. Continue in its thread.\n\n{REF}"
    )


def test_discord_permission_error_is_actionable_without_http_detail() -> None:
    error = discord.HTTPException(MagicMock(status=403, reason="Forbidden"), "private")
    assert render_error(error, request_id=TEST_RID) == (
        f"Daimon doesn't have permission to post here. Ask a server admin for help.\n\n{REF}"
    )


class TestGenerateRequestId:
    def test_generate_request_id_is_ulid(self) -> None:
        rid = generate_request_id()
        assert isinstance(rid, str), "should return a string"
        assert len(rid) == 26, "ULID should be 26 characters"

    def test_generate_request_id_unique(self) -> None:
        rid1 = generate_request_id()
        rid2 = generate_request_id()
        assert rid1 != rid2, "consecutive ULIDs should be unique"


def test_audited_user_guidance_keeps_its_next_step_without_retry_later() -> None:
    for copy in (
        "This turn's setup context expired. Mention me again to continue.",
        "This server is not registered. Ask a server admin to finish setup.",
        "The agent was created but is not listed yet. Reopen `/agent-setup` to see it.",
    ):
        assert render_error(UserFacingError(copy), request_id=TEST_RID) == f"{copy}\n\n{REF}"


@pytest.mark.parametrize("status", [400, 404])
async def test_missing_setup_responder_keeps_shared_helper_guidance(status: int) -> None:
    client = build_fake_anthropic(
        lambda req: httpx.Response(
            status,
            json={"type": "error", "error": {"type": "not_found_error", "message": "private"}},
        )
    )
    with pytest.raises(UserFacingError) as caught:
        await get_setup_responder(client, tenant_id=uuid.uuid4(), ma_agent_id="ag_private")
    assert render_error(caught.value, request_id=TEST_RID) == (
        "This setup conversation's Daimon responder is missing. "
        "Ask the operator to restore it, then open a new setup conversation."
        f"\n\n{REF}"
    )


async def test_a_converted_core_refusal_keeps_its_words_and_the_ref() -> None:
    """The clone refusal is authored copy, so it reaches chat as written."""
    now = datetime.now(UTC)
    binding = AgentRepoBindingRow(
        tenant_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        repo_url="acme/widgets",
        default_branch="main",
        ma_secret_ref="",
        created_at=now,
        updated_at=now,
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(UserFacingError) as caught:
            await resolve_clone_token(
                client,
                binding=binding,
                per_agent_pat=None,
                fallback_pat=None,
                app_id=None,
                app_private_key=None,
                now=int(now.timestamp()),
            )
    wrapped = TurnError(kind="upstream", cause=caught.value)
    assert render_error(wrapped, request_id=TEST_RID) == (
        "No credential is authorized to clone acme/widgets. Re-bind this repo "
        "with a GitHub token that can read it, using request_repo_binding."
        f"\n\n{REF}"
    )


def test_an_unreadable_access_policy_keeps_its_words_and_the_ref() -> None:
    """A class whose every message is written for people subclasses UserFacingError."""
    error = AccessPolicyUnreadable(tenant_id=uuid.uuid4())
    assert render_error(TurnError(kind="upstream", cause=error), request_id=TEST_RID) == (
        "this workspace's access settings can't be read, so no turn was started; "
        f"ask an admin to fix them\n\n{REF}"
    )
