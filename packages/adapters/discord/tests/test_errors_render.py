"""Known errors use plain copy without provider bodies or internal identifiers."""

from __future__ import annotations

from unittest.mock import MagicMock

import anthropic
import discord
import httpx
from daimon.adapters.discord.errors import generate_request_id, render_error
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
from daimon.core.notebooks.publish import NotebookRateLimitError
from daimon.core.stores.direct_messages import DirectMessageBusy
from sqlalchemy.exc import DBAPIError

TEST_RID = "01JTZXTEST000000000000000"


def test_known_local_errors_never_publish_exception_bodies() -> None:
    body = "sesn_private agent_private access_token=private SELECT secret"
    for error, expected in [
        (SpecError(body), "Daimon couldn't read this setup."),
        (ChannelBudgetError(body), "That spending budget isn't valid."),
        (InvalidScheduleError(body), "That schedule isn't valid."),
        (InvalidChannelAdminIds(body), "That admin selection isn't valid."),
        (NotebookRateLimitError(body), "The notebook publishing limit has been reached."),
        (DirectMessageBusy(body), "A reply is still running."),
        (HandoffRefusedInSetupThread(body), "This setup conversation can't change agents."),
        (AgentNameCollision(body), "This workspace already has an agent with that name."),
        (StoreError(body), "Daimon couldn't load or save this change."),
        (DaimonError(body), "Something went wrong while handling your request."),
        (ValueError(body), "Daimon couldn't use that input."),
        (RuntimeError(body), "Something went wrong while handling your request."),
        (
            DBAPIError("SELECT secret", {"token": "private"}, Exception(body)),
            "Daimon couldn't load or save this change.",
        ),
    ]:
        result = render_error(error, request_id=TEST_RID)
        assert result.startswith(expected)
        assert "private" not in result
        assert "SELECT" not in result
        assert TEST_RID not in result
        assert "rid:" not in result


def test_anthropic_failures_use_plain_copy_without_json_or_identifiers() -> None:
    for status, expected in [
        (503, "Claude is overloaded right now. Try again in a minute."),
        (529, "Claude is overloaded right now. Try again in a minute."),
        (429, "Too many requests right now. Try again in a minute."),
        (400, "Claude couldn't accept this request. Try sending it again."),
        (401, "Daimon couldn't connect to Claude. Ask an admin to check the connection."),
        (403, "Daimon couldn't connect to Claude. Ask an admin to check the connection."),
        (500, "Claude is unavailable right now. Try again in a minute."),
    ]:
        error = anthropic.APIStatusError(
            message=f"Error code: {status} - "
            + "{'request_id': 'req_private', 'message': 'Overloaded'}",
            response=httpx.Response(
                status, request=httpx.Request("POST", "https://api.anthropic.com")
            ),
            body={"request_id": "req_private"},
        )
        assert render_error(error, request_id=TEST_RID) == expected
        assert (
            render_error(TurnError(kind="upstream", cause=error), request_id=TEST_RID) == expected
        )


def test_connection_and_generic_api_errors_do_not_publish_bodies() -> None:
    request = httpx.Request("POST", "https://api.anthropic.com")
    assert render_error(anthropic.APIConnectionError(request=request), request_id=TEST_RID) == (
        "Daimon couldn't reach Claude. Try again in a minute."
    )
    assert render_error(
        anthropic.APIError("req_private", request, body=None), request_id=TEST_RID
    ) == ("Daimon couldn't get a reply from Claude. Try again in a minute.")


def test_discord_existing_thread_error_is_plain_copy() -> None:
    response = MagicMock(status=400, reason="Bad Request")
    error = discord.HTTPException(
        response, {"code": 160004, "message": "A thread has already been created for this message"}
    )
    assert render_error(error, request_id=TEST_RID) == (
        "A conversation already exists for this message. Continue in its thread."
    )


def test_discord_permission_error_is_actionable_without_http_detail() -> None:
    error = discord.HTTPException(MagicMock(status=403, reason="Forbidden"), "private")
    assert render_error(error, request_id=TEST_RID) == (
        "Daimon doesn't have permission to post here. Ask a server admin for help."
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
        assert render_error(UserFacingError(copy), request_id=TEST_RID) == copy
