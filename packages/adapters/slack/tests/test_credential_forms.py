"""Tests for the Slack credential-request forms (`credential_forms.py`).

Behavioral assertions:
  - build_credential_modal states every fixed fact of the request as context
    lines and collects exactly ONE input per kind (a file for env_file, a
    value or token otherwise), keeps every title inside Slack's 24-character
    cap, and carries token/channel/message_ts through private_metadata under
    the kind's callback_id.
  - evaluate_credential_submission rejects an empty or oversized value with a
    response_action errors payload keyed to the input block, rejects an
    env_file submission that is not exactly one file within the size cap, and
    never copies the secret anywhere but the decision's own field.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from daimon.adapters.slack.credential_requests import (
    CRED_CALLBACK_PREFIX,
    build_credential_modal,
    evaluate_credential_submission,
)
from daimon.core.credential_requests import (
    ENV_FILE_TARGET,
    build_skill_repo_target,
)
from daimon.core.env_file import MAX_ENV_FILE_BYTES

from .credential_helpers import _CHANNEL_ID, _MESSAGE_TS, _TEAM_ID, _USER_ID

# ---------------------------------------------------------------------------
# build_credential_modal
# ---------------------------------------------------------------------------


_FORM_TARGETS: dict[str, str] = {
    "env": "OPENAI_API_KEY",
    "env_file": ENV_FILE_TARGET,
    "mcp": "my-server",
    "repo": build_skill_repo_target("https://github.com/owner/repo", "main", ""),
    "skill_repo": build_skill_repo_target("https://github.com/owner/skills", "main", "skills"),
}
_ALL_KINDS = ["env", "env_file", "mcp", "repo", "skill_repo"]


def _modal(
    kind: str,
    *,
    target: str | None = None,
    mcp_server_url: str | None = "https://mcp.example.com",
) -> dict[str, Any]:
    return build_credential_modal(
        kind=kind,  # type: ignore[arg-type]
        token="tok_test",
        channel_id=_CHANNEL_ID,
        message_ts=_MESSAGE_TS,
        target=target if target is not None else _FORM_TARGETS[kind],
        agent_name="tester",
        mcp_server_url=mcp_server_url,
    )


def _blocks_of(view: dict[str, Any], block_type: str) -> list[dict[str, Any]]:
    return [b for b in view["blocks"] if b["type"] == block_type]


@pytest.mark.parametrize("kind", _ALL_KINDS)
def test_every_form_states_its_facts_and_collects_one_input(kind: str) -> None:
    view = _modal(kind)
    inputs = _blocks_of(view, "input")
    contexts = _blocks_of(view, "context")
    assert view["callback_id"] == f"{CRED_CALLBACK_PREFIX}{kind}", (
        "the callback id routes the submission back to this kind"
    )
    assert len(inputs) == 1, "a private form collects exactly one thing"
    assert not inputs[0].get("optional", False), "the one input a form has is required"
    assert len(contexts) + len(inputs) == len(view["blocks"]), (
        "a form is fixed facts plus one input, nothing else"
    )
    assert "tester" in json.dumps(contexts), "the facts name the agent the request is for"


@pytest.mark.parametrize("kind", _ALL_KINDS)
def test_every_form_title_fits_slacks_cap(kind: str) -> None:
    title = _modal(kind)["title"]["text"]
    assert 0 < len(title) <= 24, "Slack rejects a view whose title exceeds 24 characters"


def test_long_key_name_title_is_truncated_rather_than_rejected() -> None:
    title = _modal("env", target="A_VERY_LONG_KEY_NAME_THAT_OVERFLOWS")["title"]["text"]
    assert title == "A_VERY_LONG_KEY_NAME_THA", "the key name is truncated to the cap, not dropped"


def test_kinds_without_a_short_target_take_their_fixed_title() -> None:
    assert _modal("env_file")["title"]["text"] == "Keys from a file", (
        "the .env sentinel target is no title"
    )
    assert _modal("repo")["title"]["text"] == "Your GitHub token", "a repo URL is no title"
    assert _modal("mcp", target="linear")["title"]["text"] == "linear token", (
        "the server name names the token being asked for"
    )


def test_env_form_collects_the_value_itself() -> None:
    element = _blocks_of(_modal("env"), "input")[0]["element"]
    assert element["type"] == "plain_text_input", "a key value is typed, not uploaded"
    assert element["multiline"] is True, "long keys must not need a single-line field"
    assert element["max_length"] == 3000, "Slack's own maximum for a plain-text input"


def test_env_file_form_collects_exactly_one_uploaded_file() -> None:
    element = _blocks_of(_modal("env_file"), "input")[0]["element"]
    assert element["type"] == "file_input", "the .env form takes an upload, not a pasted value"
    assert element["max_files"] == 1, "a whole-file import reads one file"
    assert element["filetypes"] == ["env", "txt"], "Slack filters the picker to .env-shaped files"


def test_env_file_form_says_the_file_itself_is_not_kept() -> None:
    facts = json.dumps(_blocks_of(_modal("env_file"), "context"))
    assert "one KEY=VALUE per line" in facts, "the form states the format it can read"
    assert "not a retained copy of your uploaded file" in facts, (
        "the person is told what is stored: the keys, not their file"
    )


def test_repo_form_has_no_branch_input_and_states_the_branch_instead() -> None:
    view = _modal("repo", target=build_skill_repo_target("https://github.com/o/r", "release", ""))
    inputs = _blocks_of(view, "input")
    assert len(inputs) == 1, "the repo form collects the token only"
    assert inputs[0]["label"]["text"] == "Token", "the one field is the token"
    assert "Branch" not in json.dumps(inputs), (
        "a branch field would let the form retarget the request the card described"
    )
    assert "release" in json.dumps(_blocks_of(view, "context")), (
        "the branch from the packed target is stated as a fact"
    )


def test_skill_repo_form_says_the_working_repo_is_untouched() -> None:
    facts = json.dumps(_blocks_of(_modal("skill_repo"), "context"))
    assert "working repo does not change" in facts, (
        "the skill repo is the one thing this import binds"
    )


def test_mcp_form_states_the_server_url_it_is_connecting() -> None:
    facts = json.dumps(_blocks_of(_modal("mcp"), "context"))
    assert "https://mcp.example.com" in facts, "the person sees which endpoint the token is for"


def test_modal_metadata_carries_token_channel_and_message_ts() -> None:
    view = _modal("env")
    meta = json.loads(view["private_metadata"])
    assert meta["token"] == "tok_test"
    assert meta["channel_id"] == _CHANNEL_ID
    assert meta["message_ts"] == _MESSAGE_TS


def test_modal_metadata_never_carries_the_target_secret_field() -> None:
    """The modal is built before any secret exists — nothing but routing
    handles may appear in private_metadata, ever."""
    view = _modal("env")
    meta = json.loads(view["private_metadata"])
    assert set(meta) <= {"token", "channel_id", "message_ts"}


# ---------------------------------------------------------------------------
# evaluate_credential_submission
# ---------------------------------------------------------------------------


def _submission(kind: str, values: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "view_submission",
        "team": {"id": _TEAM_ID},
        "user": {"id": _USER_ID},
        "view": {
            "callback_id": f"{CRED_CALLBACK_PREFIX}{kind}",
            "private_metadata": json.dumps(
                {"token": "tok_test", "channel_id": _CHANNEL_ID, "message_ts": _MESSAGE_TS},
                separators=(",", ":"),
            ),
            "state": {"values": values},
        },
    }


def _value_input(value: str) -> dict[str, Any]:
    return {
        "credential__value": {"credential__value": {"type": "plain_text_input", "value": value}}
    }


def test_empty_value_is_rejected_with_field_error() -> None:
    decision = evaluate_credential_submission(_submission("env", _value_input("   ")))
    assert decision.proceed is False
    assert decision.response_payload is not None
    assert decision.response_payload["response_action"] == "errors"
    assert "credential__value" in decision.response_payload["errors"]


def test_oversized_value_is_rejected_with_field_error() -> None:
    decision = evaluate_credential_submission(
        _submission("env", _value_input("é" * 3000))  # 6000 bytes, 3000 chars
    )
    assert decision.proceed is False
    assert decision.response_payload is not None
    assert "credential__value" in decision.response_payload["errors"]


def test_valid_value_proceeds_and_carries_routing_fields() -> None:
    decision = evaluate_credential_submission(_submission("env", _value_input("s3cr3t")))
    assert decision.proceed is True
    assert decision.response_payload is None
    assert decision.kind == "env"
    assert decision.value == "s3cr3t"
    assert decision.token == "tok_test"
    assert decision.channel_id == _CHANNEL_ID
    assert decision.message_ts == _MESSAGE_TS


def _file_input(files: list[dict[str, Any]]) -> dict[str, Any]:
    return {"credential__file": {"credential__file": {"type": "file_input", "files": files}}}


def _env_file_error(files: list[dict[str, Any]]) -> str:
    decision = evaluate_credential_submission(_submission("env_file", _file_input(files)))
    assert decision.proceed is False, "a file submission that cannot be read must not proceed"
    assert decision.response_payload is not None, "a refusal needs an ack payload"
    assert decision.response_payload["response_action"] == "errors", (
        "response_action errors keeps the form open instead of closing it"
    )
    errors: dict[str, str] = decision.response_payload["errors"]
    assert set(errors) == {"credential__file"}, "the error names the file field, nothing else"
    return errors["credential__file"]


def test_env_file_submission_without_a_file_is_refused_on_the_file_field() -> None:
    message = _env_file_error([])
    assert ".env" in message, "the message says what to attach"


def test_env_file_submission_with_two_files_is_refused_on_the_file_field() -> None:
    message = _env_file_error(
        [
            {"id": "F_ONE", "name": "a.env", "size": 10},
            {"id": "F_TWO", "name": "b.env", "size": 10},
        ]
    )
    assert "one file" in message, "the message says one file at a time"
    assert "F_ONE" not in message and "F_TWO" not in message, (
        "a field error names the field, never the submitted content"
    )


def test_env_file_submission_over_the_size_cap_is_refused_before_any_download() -> None:
    message = _env_file_error([{"id": "F_BIG", "name": ".env", "size": MAX_ENV_FILE_BYTES + 1}])
    assert "too big" in message and "KB" in message, "the message states the cap"
    assert "F_BIG" not in message, "a field error names the field, never the submitted content"


def test_env_file_submission_with_one_file_proceeds_with_its_id() -> None:
    decision = evaluate_credential_submission(
        _submission("env_file", _file_input([{"id": "F_ENV", "name": ".env", "size": 128}]))
    )
    assert decision.proceed is True, "one file within the cap is what the form asked for"
    assert decision.response_payload is None, "an accepted submission acks empty and closes"
    assert decision.file_id == "F_ENV", "the decision carries the handle the runner fetches with"
    assert decision.value == "", "a file submission carries a handle, never a value"
