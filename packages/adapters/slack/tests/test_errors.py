"""Tests for errors.py — render_error, build_error_view, surface_command_error."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anthropic
import httpx
from cryptography.fernet import InvalidToken
from daimon.adapters.slack.errors import (
    build_error_view,
    generate_request_id,
    render_error,
    surface_command_error,
)
from daimon.core.errors import DaimonError, SpecError, StoreError
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.exc import DBAPIError, OperationalError
from yarl import URL

TEST_RID = "01JTZXTEST000000000000000"


def _requests_to(mock: Any, method: str) -> list[Any]:
    return [
        req
        for (_, url), reqs in mock.requests.items()
        if url == URL(f"https://slack.com/api/{method}")
        for req in reqs
    ]


class TestRenderError:
    def test_spec_error(self) -> None:
        result = render_error(SpecError("invalid field 'foo'"), request_id=TEST_RID)
        assert result.startswith("⚠️"), "spec errors carry the warning emoji"
        assert "*Spec validation failed*" in result, "label uses Slack single-star bold"
        assert "invalid field" in result, "detail is shown to the user"
        assert f"`rid: {TEST_RID}`" in result, "rid is the handle into the logs"

    def test_store_error(self) -> None:
        result = render_error(StoreError("agent not found"), request_id=TEST_RID)
        assert "*Store error*" in result
        assert "agent not found" in result
        assert TEST_RID in result

    def test_daimon_error_generic(self) -> None:
        result = render_error(DaimonError("something broke"), request_id=TEST_RID)
        assert "*Error*" in result
        assert "something broke" in result
        assert TEST_RID in result

    def test_daimon_error_text_is_mrkdwn_escaped(self) -> None:
        result = render_error(DaimonError("use <@U123> & <#C1>"), request_id=TEST_RID)
        assert "<@U123>" not in result, "exception text must not become a live Slack mention"
        assert "&lt;@U123&gt; &amp; &lt;#C1&gt;" in result

    def test_api_connection_error(self) -> None:
        exc = anthropic.APIConnectionError(
            request=httpx.Request("GET", "https://api.anthropic.com"),
        )
        result = render_error(exc, request_id=TEST_RID)
        assert result.startswith("\U0001f50c")
        assert "*Connection Error*" in result
        assert "Could not connect" in result
        assert TEST_RID in result

    def test_api_status_error_includes_status_code(self) -> None:
        exc = anthropic.APIStatusError(
            message="rate limited",
            response=httpx.Response(
                status_code=429,
                request=httpx.Request("POST", "https://api.anthropic.com"),
            ),
            body=None,
        )
        result = render_error(exc, request_id=TEST_RID)
        assert result.startswith("❌")
        assert "*API Error (429)*" in result
        assert "rate limited" in result
        assert TEST_RID in result

    def test_slack_api_error_names_slack_error_code(self) -> None:
        response = MagicMock()
        response.status_code = 200
        response.__getitem__ = MagicMock(return_value="channel_not_found")
        response.get = MagicMock(return_value="channel_not_found")
        exc = SlackApiError("The request to the Slack API failed.", response)
        result = render_error(exc, request_id=TEST_RID)
        assert "*Slack Error*" in result
        assert "channel_not_found" in result
        assert TEST_RID in result

    def test_sqlalchemy_error_never_leaks_statement_or_parameters(self) -> None:
        exc = DBAPIError(
            "SELECT secret FROM users WHERE token = %(token)s",
            {"token": "xoxb-super-secret"},
            Exception("connection lost"),
        )
        result = render_error(exc, request_id=TEST_RID)
        assert "*Database error*" in result
        assert "DBAPIError" in result, "type name is enough to triage"
        assert "SELECT" not in result, "statement must stay in the logs"
        assert "xoxb-super-secret" not in result, "bound parameters must stay in the logs"
        assert TEST_RID in result

    def test_operational_error_also_scrubbed(self) -> None:
        exc = OperationalError("stmt", {"p": "v"}, Exception("boom"))
        result = render_error(exc, request_id=TEST_RID)
        assert "OperationalError" in result
        assert "stmt" not in result

    def test_invalid_token_does_not_leak_ciphertext_detail(self) -> None:
        result = render_error(InvalidToken("gAAAA-ciphertext"), request_id=TEST_RID)
        assert "gAAAA" not in result, "Fernet detail stays in the logs"
        assert "*Credential error*" in result
        assert TEST_RID in result

    def test_value_error(self) -> None:
        result = render_error(ValueError("bad thing"), request_id=TEST_RID)
        assert "*Invalid input*" in result
        assert "bad thing" in result

    def test_unexpected_error_fallback(self) -> None:
        result = render_error(RuntimeError("weird"), request_id=TEST_RID)
        assert "*Unexpected error*" in result
        assert "weird" in result
        assert TEST_RID in result


class TestGenerateRequestId:
    def test_is_26_char_ulid(self) -> None:
        rid = generate_request_id()
        assert len(rid) == 26
        assert rid.isalnum()

    def test_unique(self) -> None:
        assert generate_request_id() != generate_request_id()


class TestBuildErrorView:
    def test_modal_keeps_title_and_carries_rendered_error(self) -> None:
        view = build_error_view(DaimonError("nope"), title="Routines", request_id=TEST_RID)
        assert view["type"] == "modal"
        assert view["title"] == {"type": "plain_text", "text": "Routines"}
        text = view["blocks"][0]["text"]["text"]
        assert "nope" in text
        assert TEST_RID in text

    def test_modal_has_close_button(self) -> None:
        view = build_error_view(DaimonError("nope"), title="Billing", request_id=TEST_RID)
        assert view["close"] == {"type": "plain_text", "text": "Close"}


class TestSurfaceCommandError:
    async def test_replaces_loading_modal_when_view_id_known(
        self, fake_slack_web_client: Any
    ) -> None:
        await surface_command_error(
            fake_slack_web_client.client,
            DaimonError("nope"),
            request_id=TEST_RID,
            title="Routines",
            view_id="V_TEST",
            channel_id="C_TEST",
            user_id="U_TEST",
        )
        updates = _requests_to(fake_slack_web_client.mock, "views.update")
        assert len(updates) == 1, "the Loading… placeholder must be replaced"
        body = updates[0].kwargs["json"]
        assert body["view_id"] == "V_TEST"
        assert TEST_RID in body["view"]["blocks"][0]["text"]["text"]
        assert not _requests_to(fake_slack_web_client.mock, "chat.postEphemeral")

    async def test_posts_ephemeral_when_no_modal_was_opened(
        self, fake_slack_web_client: Any
    ) -> None:
        await surface_command_error(
            fake_slack_web_client.client,
            DaimonError("nope"),
            request_id=TEST_RID,
            title="Routines",
            view_id="",
            channel_id="C_TEST",
            user_id="U_TEST",
        )
        assert not _requests_to(fake_slack_web_client.mock, "views.update")
        ephemerals = _requests_to(fake_slack_web_client.mock, "chat.postEphemeral")
        assert len(ephemerals) == 1
        body = ephemerals[0].kwargs["json"]
        assert body["channel"] == "C_TEST"
        assert body["user"] == "U_TEST"
        assert TEST_RID in body["text"]

    async def test_secondary_slack_failure_is_swallowed(self) -> None:
        client = MagicMock(spec=AsyncWebClient)
        client.views_update = AsyncMock(side_effect=SlackApiError("down", MagicMock()))
        await surface_command_error(
            client,
            DaimonError("nope"),
            request_id=TEST_RID,
            title="Routines",
            view_id="V_TEST",
            channel_id="C_TEST",
            user_id="U_TEST",
        )
