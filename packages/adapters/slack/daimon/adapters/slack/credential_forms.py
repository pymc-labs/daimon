"""Slack credential-request forms — the modal each kind opens, and its ack.

The pure half of the credential-request surface: the per-kind private form
(`build_credential_modal`), the pre-ack evaluation of its submission
(`evaluate_credential_submission`), and the card copy a refusal echoes into
the ephemeral beside it. No I/O and no runtime — the click handler in
`credential_requests.py` and the runners in `credential_submissions.py` both
build on what is here.

Secret hygiene (the same structural guarantees the Discord modals and the
panel's paste form document): the submitted value exists only in the modal's
input state and the decision object's own field — it never enters a log
record (env logs the key name; mcp/repo log a masked tail), an `action_id`,
`private_metadata`, or any non-ephemeral message.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any, Final, cast

from daimon.adapters.slack.modal_limits import MAX_TITLE_CHARS
from daimon.core.constants import MAX_SECRET_VALUE_BYTES
from daimon.core.credential_requests import (
    CredentialRequestKind,
    split_skill_repo_target,
)
from daimon.core.env_file import (
    MAX_ENV_FILE_BYTES,
)
from daimon.core.github_repo_auth import normalize_owner_repo
from daimon.core.posted_controls import (
    CardKind,
    CardState,
    RefusalReason,
    card_for_request,
    card_text,
    expired_message,
)
from daimon.core.stores.domain import CredentialRequestRow

__all__ = [
    "CRED_CALLBACK_PREFIX",
    "CredentialSubmissionDecision",
    "build_credential_modal",
    "evaluate_credential_submission",
    "expired_refusal",
    "refusal_text",
]

CRED_CALLBACK_PREFIX: Final[str] = "credential_request__"

_VALUE_BLOCK = "credential__value"
_FILE_BLOCK = "credential__file"

#: The two kinds whose `target` packs `repo_url@branch#path`.
_REPO_KINDS: Final[frozenset[str]] = frozenset({"repo", "skill_repo"})

#: Title for a kind whose `target` is a URL or a sentinel rather than a name.
_TITLE_FALLBACK: Final[dict[CredentialRequestKind, str]] = {
    "env": "Add a key",
    "env_file": "Keys from a file",
    "mcp": "Your MCP token",
    "repo": "Your GitHub token",
    "skill_repo": "Your GitHub token",
}


def expired_refusal(row: CredentialRequestRow) -> str:
    """The expired copy for `row`: what happened, and how to ask again.

    Word-for-word the text the card itself now shows, because both come from
    `expired_message` — a late clicker's ephemeral and the card beside it must
    not say different things.
    """
    repo_display: str | None = None
    if row.kind in _REPO_KINDS:
        repo_url, _branch, _path = split_skill_repo_target(row.target)
        repo_display = normalize_owner_repo(repo_url)
    return expired_message(
        kind=cast("CardKind", row.kind),
        agent_name=row.target_name or "the agent",
        responder_name=row.responder_name or "Daimon",
        target=row.target,
        repo=repo_display,
    )


def refusal_text(
    row: CredentialRequestRow,
    *,
    state: CardState,
    refusal: RefusalReason | None = None,
) -> str:
    """The ephemeral copy for a refused submission: the card's own words.

    Built by the same `card_for_request` call the edit beside it makes, for
    the reason `expired_refusal` is: the ephemeral and the card it sits next
    to must not describe one refusal two different ways.
    """
    return card_text(card_for_request(row, state=state, refusal=refusal))


def _modal_title(kind: CredentialRequestKind, target: str) -> str:
    """The form's title, built to fit Slack's 24-character `view.title` cap.

    The key name is the most useful title the `env` kind can have, and the
    server name the most useful one for `mcp`; the other kinds' `target` is a
    packed URL or the `.env` sentinel, so they take the fixed fallback.
    """
    name = target.strip()
    if kind == "env" and name:
        return name[:MAX_TITLE_CHARS]
    if kind == "mcp" and name:
        suffix = " token"
        return f"{name[: MAX_TITLE_CHARS - len(suffix)]}{suffix}"
    return _TITLE_FALLBACK[kind]


def _form_facts(
    kind: CredentialRequestKind, *, agent_name: str, target: str, mcp_server_url: str | None
) -> tuple[str, ...]:
    """The fixed facts shown above the input — one fact per context line.

    Everything the request already decided (which agent, which key, which
    repo and branch, which server) is stated here as text, never as an
    editable field: the form collects the one thing the request does not
    already know.
    """
    if kind == "env":
        return (
            f"for *{agent_name}*",
            f"anyone who talks to {agent_name} can use it",
            "the value is not shown in chat",
        )
    if kind == "env_file":
        return (
            f"for *{agent_name}*",
            "one KEY=VALUE per line",
            "Daimon stores the keys, not a retained copy of your uploaded file",
        )
    if kind == "mcp":
        return (
            f"for *{agent_name}* → {mcp_server_url or target}",
            f"anyone who talks to {agent_name} can use this connection",
        )
    repo_url, branch, _path = split_skill_repo_target(target)
    skill_repo_only = (
        ("skill repo only — the working repo does not change",) if kind == "skill_repo" else ()
    )
    return (
        f"for *{agent_name}* → <{repo_url}|{normalize_owner_repo(repo_url)}>, branch `{branch}`",
        *skill_repo_only,
        f"the token stays yours; {agent_name} uses it whenever it needs GitHub",
    )


def _context_block(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _form_input(kind: CredentialRequestKind) -> dict[str, Any]:
    """The one input block this kind collects."""
    if kind == "env_file":
        return {
            "type": "input",
            "block_id": _FILE_BLOCK,
            "label": {"type": "plain_text", "text": ".env file"},
            "element": {
                "type": "file_input",
                "action_id": _FILE_BLOCK,
                "filetypes": ["env", "txt"],
                "max_files": 1,
            },
        }
    if kind == "env":
        return {
            "type": "input",
            "block_id": _VALUE_BLOCK,
            "label": {"type": "plain_text", "text": "Value"},
            "element": {
                "type": "plain_text_input",
                "action_id": _VALUE_BLOCK,
                "multiline": True,
                "max_length": 3000,
                "placeholder": {"type": "plain_text", "text": "paste the value"},
            },
        }
    element: dict[str, Any] = {
        "type": "plain_text_input",
        "action_id": _VALUE_BLOCK,
        "max_length": 255,
    }
    block: dict[str, Any] = {
        "type": "input",
        "block_id": _VALUE_BLOCK,
        "label": {"type": "plain_text", "text": "Token"},
        "element": element,
    }
    if kind != "mcp":
        element["placeholder"] = {"type": "plain_text", "text": "github_pat_…"}
        block["hint"] = {
            "type": "plain_text",
            "text": "a fine-grained token with read access to the repo",
        }
    return block


def build_credential_modal(
    *,
    kind: CredentialRequestKind,
    token: str,
    channel_id: str,
    message_ts: str,
    target: str,
    agent_name: str,
    mcp_server_url: str | None = None,
) -> dict[str, Any]:
    """The per-kind private form opened from a live request's button click.

    Every routing fact — the agent, the key or server name, the repo and the
    branch — is already fixed by the request row keyed by ``token``, so each
    form states those as context lines and collects exactly ONE input: the
    value, the token, or the `.env` file. Nothing restated here is editable,
    which is what keeps a submitted form unable to retarget the request.

    ``private_metadata`` carries only routing handles (token, channel, the
    button message's ts) — never a secret, and never the target.
    """
    facts = _form_facts(kind, agent_name=agent_name, target=target, mcp_server_url=mcp_server_url)
    blocks: list[dict[str, Any]] = [_context_block(fact) for fact in facts]
    blocks.append(_form_input(kind))
    return {
        "type": "modal",
        "callback_id": f"{CRED_CALLBACK_PREFIX}{kind}",
        "private_metadata": json.dumps(
            {"token": token, "channel_id": channel_id, "message_ts": message_ts},
            separators=(",", ":"),
        ),
        "title": {"type": "plain_text", "text": _modal_title(kind, target)},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }


@dataclasses.dataclass(frozen=True)
class CredentialSubmissionDecision:
    """Outcome of the pure pre-ack evaluation of a credential view_submission.

    ``response_payload`` is the ack body (``response_action: errors``) when
    the submission is rejected, or None for an empty ack that closes the
    form. ``value`` is the secret and is carried in memory only — it must
    never be logged. ``file_id`` is the uploaded `.env` file's Slack id on the
    ``env_file`` kind and None everywhere else: a file id is a handle, not a
    value, and the bytes are fetched afterwards with the workspace's own bot
    token.

    No branch field: the repo kinds' branch rides in the request row's packed
    `target`, so a submitted form cannot change which branch was asked for.
    """

    proceed: bool
    response_payload: dict[str, Any] | None
    kind: CredentialRequestKind
    value: str
    token: str
    channel_id: str
    message_ts: str
    file_id: str | None = None


def _input_value(values: dict[str, Any], block_id: str) -> str:
    block: dict[str, Any] = values.get(block_id) or {}
    element: dict[str, Any] = block.get(block_id) or {}
    return str(element.get("value") or "")


def _uploaded_files(values: dict[str, Any], block_id: str) -> list[dict[str, Any]]:
    """The `file_input` element's submitted file objects (id, name, size)."""
    block: dict[str, Any] = values.get(block_id) or {}
    element: dict[str, Any] = block.get(block_id) or {}
    files: list[dict[str, Any]] = element.get("files") or []
    return files


def evaluate_credential_submission(payload: dict[str, Any]) -> CredentialSubmissionDecision:
    """Pure (no I/O) evaluation of a credential_request__* view_submission.

    Rejects an empty or whitespace-only secret with a field error so the
    person can retype rather than lose the form, and enforces the same byte
    cap the panel's paste form does — Slack's ``max_length`` is a character
    cap and multi-byte input can clear it while overflowing the store.

    The ``env_file`` kind carries a file rather than a value: exactly one is
    required, and the size Slack reports in the submission is checked here so
    an obviously oversized upload is refused with the form still open. That
    size is advisory — it comes from the submitting client — so the bytes are
    measured again after the download, before anything is parsed.
    """
    view: dict[str, Any] = payload.get("view") or {}
    callback_id = str(view.get("callback_id") or "")
    kind_str = callback_id.removeprefix(CRED_CALLBACK_PREFIX)
    kind: CredentialRequestKind = kind_str  # type: ignore[assignment]  # validated by the dispatch prefix match
    meta: dict[str, Any]
    try:
        meta = json.loads(str(view.get("private_metadata") or "") or "{}")
    except json.JSONDecodeError:
        meta = {}
    state: dict[str, Any] = view.get("state") or {}
    values: dict[str, Any] = state.get("values") or {}
    token = str(meta.get("token") or "")
    channel_id = str(meta.get("channel_id") or "")
    message_ts = str(meta.get("message_ts") or "")

    def _decision(
        *,
        proceed: bool,
        errors: dict[str, str] | None = None,
        value: str = "",
        file_id: str | None = None,
    ) -> CredentialSubmissionDecision:
        return CredentialSubmissionDecision(
            proceed=proceed,
            response_payload=(
                {"response_action": "errors", "errors": errors} if errors is not None else None
            ),
            kind=kind,
            value=value,
            token=token,
            channel_id=channel_id,
            message_ts=message_ts,
            file_id=file_id,
        )

    if kind == "env_file":
        files = _uploaded_files(values, _FILE_BLOCK)
        file_id = str(files[0].get("id") or "") if len(files) == 1 else ""
        if not file_id:
            return _decision(
                proceed=False,
                errors={
                    _FILE_BLOCK: (
                        "Attach one .env file." if len(files) < 2 else "Attach one file at a time."
                    )
                },
            )
        # The submitting client reports this; it is a cheap way to refuse a
        # huge upload without downloading it, not the boundary that protects
        # the parser. `decode_env_bytes` re-measures the real bytes.
        if int(files[0].get("size") or 0) > MAX_ENV_FILE_BYTES:
            return _decision(
                proceed=False,
                errors={_FILE_BLOCK: f"That file is too big. Max {MAX_ENV_FILE_BYTES // 1024} KB."},
            )
        return _decision(proceed=True, file_id=file_id)

    raw_value = _input_value(values, _VALUE_BLOCK)
    if not raw_value.strip():
        return _decision(
            proceed=False,
            errors={_VALUE_BLOCK: "Value cannot be empty — try again."},
        )
    if len(raw_value.encode()) > MAX_SECRET_VALUE_BYTES:
        return _decision(
            proceed=False,
            errors={_VALUE_BLOCK: f"Value is too large. Max {MAX_SECRET_VALUE_BYTES} bytes."},
        )
    return _decision(proceed=True, value=raw_value)
