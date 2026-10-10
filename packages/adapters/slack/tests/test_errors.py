"""Tests for errors.py — render_error, build_error_view, surface_command_error."""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anthropic
import httpx
import pytest
from cryptography.fernet import InvalidToken
from daimon.adapters.slack.errors import (
    NOT_SET_UP_NOTICE,
    SETUP_OUT_OF_DATE_NOTICE,
    build_error_view,
    generate_request_id,
    render_error,
    render_error_payload,
    surface_command_error,
)
from daimon.core.errors import DaimonError, SpecError, StoreError, TurnError, UserFacingError
from daimon.core.turn.errors import SessionAgentMismatch
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.exc import DBAPIError, OperationalError
from structlog.testing import capture_logs
from yarl import URL

TEST_RID = "01JTZXTEST0000000000000000"


def _requests_to(mock: Any, method: str) -> list[Any]:
    return [
        req
        for (_, url), reqs in mock.requests.items()
        if url == URL(f"https://slack.com/api/{method}")
        for req in reqs
    ]


REF = "_Ref 000000_"
OUR_SIDE = "Something went wrong on our side.\n\nTry again. If it keeps happening, tell an admin."
BUSY = "Daimon's AI service is busy.\n\nTry again in a minute."
UNREACHABLE = "Daimon couldn't reach its AI service.\n\nTry again in a minute."
REFUSED = "Daimon's AI service couldn't accept the request.\n\nAsk an admin to check it."


def _status_error(
    status: int, payload: dict[str, object] | None = None
) -> anthropic.APIStatusError:
    return anthropic.APIStatusError(
        message="private provider message",
        response=httpx.Response(
            status_code=status,
            request=httpx.Request("POST", "https://api.anthropic.com"),
            json=payload or {"request_id": "req_private"},
        ),
        body=None,
    )


def _slack_error(code: str) -> SlackApiError:
    response = MagicMock()
    response.status_code = 200
    response.__getitem__ = MagicMock(return_value=code)
    response.get = MagicMock(return_value=code)
    return SlackApiError("The request to the Slack API failed.", response)


class TestRenderError:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (429, BUSY),
            (529, BUSY),
            (500, UNREACHABLE),
            (502, UNREACHABLE),
            (400, REFUSED),
            (401, REFUSED),
            (404, REFUSED),
        ],
    )
    def test_anthropic_status_errors_map_to_their_cause(self, status: int, expected: str) -> None:
        result = render_error(_status_error(status), request_id=TEST_RID)
        assert result == f"{expected}\n\n{REF}"
        assert "private" not in result
        wrapped = TurnError(kind="upstream", cause=_status_error(status))
        assert render_error(wrapped, request_id=TEST_RID) == result

    def test_the_spend_limit_is_the_usage_limit_not_busy(self) -> None:
        exc = _status_error(
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
        assert render_error(exc, request_id=TEST_RID) == (
            "Daimon has reached its usage limit.\n\n"
            f"Ask the team running it to check the limit.\n\n{REF}"
        )

    def test_api_connection_error(self) -> None:
        exc = anthropic.APIConnectionError(
            request=httpx.Request("GET", "https://api.anthropic.com"),
        )
        assert render_error(exc, request_id=TEST_RID) == f"{UNREACHABLE}\n\n{REF}"

    def test_missing_scope_asks_for_permissions(self) -> None:
        result = render_error(_slack_error("missing_scope"), request_id=TEST_RID)
        assert result == (
            "Daimon doesn't have permission to do that in Slack.\n\n"
            "Ask a workspace admin to check Daimon's permissions and reinstall it.\n\n"
            f"{REF}"
        )

    def test_other_slack_errors_say_slack_did_not_accept(self) -> None:
        result = render_error(_slack_error("channel_not_found"), request_id=TEST_RID)
        assert result == f"Slack didn't accept that.\n\nTry again.\n\n{REF}"
        assert "channel_not_found" not in result

    def test_invalid_token_cannot_connect(self) -> None:
        result = render_error(InvalidToken("gAAAA-ciphertext"), request_id=TEST_RID)
        assert "gAAAA" not in result, "Fernet detail stays in the logs"
        assert result == (
            "Daimon can't connect to this Slack workspace.\n\n"
            f"Tell the team running Daimon.\n\n{REF}"
        )

    def test_everything_else_is_our_side_without_its_text(self) -> None:
        for exc in [
            SpecError("invalid field 'foo'"),
            StoreError("agent not found"),
            DaimonError("use <@U123> & <#C1>"),
            ValueError("bad thing"),
            RuntimeError("weird"),
            DBAPIError(
                "SELECT secret FROM users WHERE token = %(token)s",
                {"token": "xoxb-super-secret"},
                Exception("connection lost"),
            ),
            OperationalError("stmt", {"p": "v"}, Exception("boom")),
        ]:
            assert render_error(exc, request_id=TEST_RID) == f"{OUR_SIDE}\n\n{REF}"

    def test_user_facing_guidance_is_shown_escaped_without_a_ref(self) -> None:
        result = render_error(UserFacingError("Ask <@U123> & retry."), request_id=TEST_RID)
        assert result == "Ask &lt;@U123&gt; &amp; retry."

    def test_session_agent_mismatch_keeps_its_wording(self) -> None:
        exc = SessionAgentMismatch(
            mapping_id=uuid.uuid4(),
            session_id="sesn_private",
            source_agent_id="ag_a",
            destination_agent_id="ag_b",
        )
        assert render_error(exc, request_id=TEST_RID) == (
            "This conversation's session belongs to another responder. "
            "Your existing work is preserved. Continuing with this responder currently "
            "requires a new conversation."
        )

    def test_lines_are_separated_by_a_blank_line(self) -> None:
        for exc in [RuntimeError("x"), _status_error(529), _slack_error("missing_scope")]:
            rendered = render_error(exc, request_id=TEST_RID)
            assert "\n" not in rendered.replace("\n\n", ""), rendered

    def test_the_ref_is_the_last_six_and_the_full_id_is_logged(self) -> None:
        rid = generate_request_id()
        with capture_logs() as logs:
            rendered = render_error(RuntimeError("private detail"), request_id=rid)
        assert rendered.endswith(f"\n\n_Ref {rid[-6:]}_")
        assert rid not in rendered
        [entry] = [e for e in logs if e["event"] == "error.rendered"]
        assert entry["rid"] == rid
        assert entry["ref"] == rid[-6:]
        assert isinstance(entry["exc_info"], RuntimeError)

    def test_payload_draws_the_ref_in_a_small_context_block(self) -> None:
        payload = render_error_payload(_status_error(529), request_id=TEST_RID)
        assert payload["text"] == f"{BUSY}\n\n{REF}"
        assert payload["blocks"] == [
            {"type": "section", "text": {"type": "mrkdwn", "text": BUSY}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "Ref 000000"}]},
        ]

    def test_setup_notices_use_the_approved_words(self) -> None:
        assert NOT_SET_UP_NOTICE == (
            "Daimon isn't set up in this channel yet.\n\nAsk an admin to run `/agent-setup`."
        )
        assert SETUP_OUT_OF_DATE_NOTICE == (
            "This channel's setup is out of date.\n\nAsk an admin to check `/agent-setup`."
        )


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
        assert view["blocks"][0]["text"]["text"] == OUR_SIDE
        assert "nope" not in str(view)
        assert view["blocks"][1] == {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "Ref 000000"}],
        }

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
        assert body["view"]["blocks"][0]["text"]["text"] == OUR_SIDE
        assert body["view"]["blocks"][1]["elements"][0]["text"] == "Ref 000000"
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
        assert body["text"] == f"{OUR_SIDE}\n\n{REF}"
        assert body["blocks"][1]["elements"][0]["text"] == "Ref 000000"

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
