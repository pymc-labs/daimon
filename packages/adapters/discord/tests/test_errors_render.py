"""Known errors use plain copy without provider bodies or internal identifiers."""

from __future__ import annotations

import uuid
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
from daimon.core.setup_conversations import get_setup_responder
from daimon.core.stores.access_policy import AccessPolicyUnreadable
from daimon.core.stores.direct_messages import DirectMessageBusy
from mux.testing.drivers.anthropic.ma import build_fake_anthropic
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


def test_known_local_errors_never_publish_exception_bodies() -> None:
    body = "sesn_private agent_private access_token=private SELECT secret"
    for error, expected in [
        (ChannelBudgetError(body), "That spending budget isn't valid."),
        (InvalidScheduleError(body), "That schedule isn't valid."),
        (NotebookRateLimitError(body), "The notebook publishing limit has been reached."),
        (DirectMessageBusy(body), "A reply is still running."),
        (HandoffRefusedInSetupThread(body), "This setup conversation can't change agents."),
        (AgentNameCollision(body), "This server already has an agent with that name."),
    ]:
        result = render_error(error, request_id=TEST_RID)
        assert result.startswith(expected)
        assert "private" not in result
        assert "SELECT" not in result
        assert result.endswith(f"\n\n{REF}")


_REFUSED = (
    "Daimon's AI service couldn't accept the request.\n\n"
    "Try again, or start a new thread. If it keeps happening, tell the team running Daimon."
)
_TOO_LARGE = (
    "That was too much for Daimon's AI service to take in at once.\n\n"
    "Shorten it, or start a new thread."
)
_KEY_REJECTED = (
    "The AI provider rejected Daimon's request.\n\n"
    "The team running Daimon needs to check its key for this provider."
)
_CREDENTIAL_GUARD = (
    "Daimon stopped this request to keep a credential safe.\n\nTell the team running Daimon."
)
_MODEL_GONE = (
    "The model this agent uses isn't offered any more.\n\n"
    "Switch the agent's model, or ask its owner or an admin to."
)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (429, "Daimon's AI service is busy.\n\nTry again in a minute."),
        (529, "Daimon's AI service is busy.\n\nTry again in a minute."),
        (500, "Daimon couldn't reach its AI service.\n\nTry again in a minute."),
        (503, "Daimon couldn't reach its AI service.\n\nTry again in a minute."),
        (400, _REFUSED),
        (401, _KEY_REJECTED),
        (403, _KEY_REJECTED),
        (404, _REFUSED),
        (413, _TOO_LARGE),
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
        "This setup conversation's Daimon agent is missing. "
        "Ask the team running Daimon to restore it, then open a new setup conversation."
        f"\n\n{REF}"
    )


def test_an_unreadable_access_policy_keeps_its_words_and_the_ref() -> None:
    """A class whose every message is written for people subclasses UserFacingError."""
    error = AccessPolicyUnreadable(tenant_id=uuid.uuid4())
    assert render_error(TurnError(kind="upstream", cause=error), request_id=TEST_RID) == (
        "the access settings here can't be read, so Daimon can't reply; "
        f"ask an admin to fix them\n\n{REF}"
    )


def test_a_missing_skill_names_it_instead_of_the_generic_refusal() -> None:
    """Findings #6/#11: the turn names the gone skill and who must remove it."""
    from daimon.core.skills.unresolved import skill_unavailable
    from daimon.testing.ma_models import ma_agent

    exc = TurnError(
        kind="upstream",
        cause=skill_unavailable(
            skill_id="skill_01PBRj",
            agent=ma_agent(id="ag_qa", name="qa"),
            channel_skills=(),
            channel_skill_name=None,
        ),
    )

    text = render_error(exc, request_id="")

    assert '"skill_01PBRj"' in text and "qa's owner or an admin" in text
    assert "remove_skill" not in text
    assert "couldn't accept the request" not in text


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            ("rate_limited", True, None),
            "Daimon's AI service is busy.\n\nTry again in a minute.",
        ),
        (("overloaded", True, "503"), "Daimon's AI service is busy.\n\nTry again in a minute."),
        (
            ("transient_network", True, None),
            "Daimon couldn't reach its AI service.\n\nTry again in a minute.",
        ),
        (
            ("upstream", True, None),
            "Daimon couldn't reach its AI service.\n\nTry again in a minute.",
        ),
        (
            ("permission", False, "gcs_token_echo"),
            _CREDENTIAL_GUARD,
        ),
        (
            ("upstream", False, "malformed_interaction"),
            _REFUSED,
        ),
        (("upstream", False, "413"), _TOO_LARGE),
        (("auth", False, "401"), _KEY_REJECTED),
        (("auth", False, "authentication_error"), _KEY_REJECTED),
        (("permission", False, "mcp_secret_outside_binding"), _CREDENTIAL_GUARD),
        (("permission", False, "gcs_downscoping_failed"), _CREDENTIAL_GUARD),
        (("permission", False, None), _REFUSED),
        (("upstream", False, "snapshot_too_large"), _REFUSED),
        (("not_found", False, "404"), _REFUSED),
    ],
)
def test_neutral_provider_failures_map_to_their_cause(
    error: tuple[str, bool, str | None], expected: str
) -> None:
    from mux.errors import ProviderError

    category, retryable, code = error
    failure = ProviderError(category, retryable=retryable, native_code=code)  # type: ignore[arg-type]
    result = render_error(failure, request_id=TEST_RID)
    assert result == f"{expected}\n\n{REF}"
    # Never the internal code (e.g. the r12 gcs_token_echo) on the card.
    assert code is None or code not in result
    wrapped = TurnError(kind="upstream", cause=failure)
    assert render_error(wrapped, request_id=TEST_RID) == f"{expected}\n\n{REF}"


def test_a_not_found_naming_the_model_says_to_switch_it() -> None:
    from mux.errors import ProviderError

    failure = ProviderError("not_found", retryable=False, native_code="404", native_param="model")
    assert render_error(failure, request_id=TEST_RID) == f"{_MODEL_GONE}\n\n{REF}"


def test_a_deleted_agent_is_named_with_where_to_pick_another() -> None:
    from daimon.adapters.discord.errors import setup_out_of_date_notice

    assert setup_out_of_date_notice(kind="agent", name="research") == (
        "This channel's agent `research` can't be found.\n\n"
        "Ask an admin to pick another in `/agent-setup`."
    )
    assert setup_out_of_date_notice(kind="environment", name="py") == SETUP_OUT_OF_DATE_NOTICE


def test_a_pinned_refusal_names_only_channels_everyone_can_see() -> None:
    """Posted where anyone reads it, so a private or unknown channel is never named."""
    from daimon.adapters.discord.bot import public_channel_ids

    def channel(visible: bool) -> MagicMock:
        found = MagicMock()
        found.permissions_for.return_value = MagicMock(view_channel=visible)
        return found

    channels = {1: channel(True), 2: channel(False)}
    guild = MagicMock()
    guild.get_channel.side_effect = channels.get
    assert public_channel_ids(guild, ("1", "2", "3", "C9")) == ("1",)
    assert public_channel_ids(None, ("1",)) == ()
