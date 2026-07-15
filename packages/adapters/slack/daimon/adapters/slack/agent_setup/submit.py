"""Agent-setup view_submission handler (pure evaluators + background runs).

Two responsibilities per form:

1. ``evaluate_*_submission`` (PURE, synchronous):
   Validates the view_submission payload within the 3-second Socket Mode ack
   deadline. Returns a ``SubmitDecision`` carrying the ``response_action``
   payload and whether the background run should proceed. No I/O.

2. ``run_*_submission`` (async, background):
   Runs AFTER the ack. Re-checks ``is_admin`` server-side (fail-closed),
   calls the appropriate write.py path, then ``views_update`` the parent or
   posts an ephemeral for slow-failure reports.

Pattern mirrors ``privacy_panel/submit.py`` exactly — same Decision dataclass
shape, same evaluate-then-spawn discipline, same boundary catch tuple.

Threat register:
- T-83-14 (EoP): every run_* re-checks resolve_is_admin before any write.
- T-83-15 (Info-Disc): secret VALUES are validated then passed to the
  write layer only; they never appear in response_action payloads, error
  strings, block_ids, action_ids, or log lines (D-09).
- T-83-16 (Tampering): blank PAT/token = keep stored token; never overwrites.
- T-83-17 (DoS): _SECRET_CAP + _MAX_SECRET_VALUE_BYTES enforced pre-ack.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any

import anthropic
import structlog
from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.agent_setup.state import decode_private_metadata
from daimon.adapters.slack.agent_setup.write import (
    call_reconcile_for_panel,
    create_blank_agent,
    fork_agent,
    kick_off_skill_sync,
    store_inline_pat,
)
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.defaults.ma_index import find_agent_by_daimon_tag
from daimon.core.defaults.mcp_merge import get_reserved_mcp_rejection
from daimon.core.defaults.provisioning import derive_guild_account_uuid
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_agent_uuid, derive_tenant_uuid
from daimon.core.observability import capture_exception_with_scope
from daimon.core.specs import AgentSpec
from daimon.core.stores.agent_files import put_agent_file
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.exc import SQLAlchemyError

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Secret-paste validation constants (D-09, T-83-17)
# Port of packages/adapters/discord/daimon/adapters/discord/agent_setup/credentials.py:40-42
# ---------------------------------------------------------------------------

_SECRET_CAP = 20
_MAX_SECRET_VALUE_BYTES = 4096
_POSIX_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Agent name format: 1-64 chars, letters/digits/hyphens/underscores
_AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# URL shape (basic — reachability is post-ack via ephemeral)
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Decision dataclass (mirrors privacy_panel/submit.py DeleteDecision)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class SubmitDecision:
    """Result of evaluate_*_submission.

    response_payload: dict to pass as SocketModeResponse(payload=...) to ack.
    proceed:          True when validation passed and the background run should fire.
    team_id:          Workspace ID from private_metadata (background run routing).
    user_id:          Submitting Slack user ID (background run admin re-check).
    agent_name:       Selected agent name from private_metadata (or None for new-agent).
    parent_section:   L2 section to return to after a successful form submission.
    extra:            Form-specific fields needed by the background run, keyed by name.
                      NEVER includes raw secret values — only key names or booleans.
    """

    response_payload: dict[str, Any]
    proceed: bool
    team_id: str
    user_id: str
    agent_name: str | None
    parent_section: str | None
    extra: dict[str, Any]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _get_view(payload: dict[str, Any]) -> dict[str, Any]:
    return payload.get("view") or {}


def _get_meta(payload: dict[str, Any]) -> dict[str, Any]:
    view = _get_view(payload)
    raw_meta: str = view.get("private_metadata") or ""
    return decode_private_metadata(raw_meta)


def _get_values(payload: dict[str, Any]) -> dict[str, Any]:
    view = _get_view(payload)
    view_state: dict[str, Any] = view.get("state") or {}
    return view_state.get("values") or {}


def _get_value(values: dict[str, Any], block_id: str, action_id: str) -> str:
    """Extract and strip a plain_text_input value from state.values."""
    block: dict[str, Any] = values.get(block_id) or {}
    element: dict[str, Any] = block.get(action_id) or {}
    return str(element.get("value") or "").strip()


def _get_user_id(payload: dict[str, Any]) -> str:
    user: dict[str, Any] = payload.get("user") or {}
    return str(user.get("id") or "")


def _error_decision(
    block_id: str,
    error_text: str,
    *,
    meta: dict[str, Any],
    payload: dict[str, Any],
) -> SubmitDecision:
    """Return a decision with response_action: errors keyed by block_id."""
    return SubmitDecision(
        response_payload={
            "response_action": "errors",
            "errors": {block_id: error_text},
        },
        proceed=False,
        team_id=str(meta.get("team_id") or ""),
        user_id=_get_user_id(payload),
        agent_name=str(meta.get("agent_name") or meta.get("selected_agent_name") or ""),
        parent_section=str(meta.get("active_section") or ""),
        extra={},
    )


def _success_decision(
    *,
    meta: dict[str, Any],
    payload: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> SubmitDecision:
    """Return a proceed=True decision. response_action: clear pops to L1."""
    return SubmitDecision(
        response_payload={"response_action": "clear"},
        proceed=True,
        team_id=str(meta.get("team_id") or ""),
        user_id=_get_user_id(payload),
        agent_name=str(meta.get("agent_name") or meta.get("selected_agent_name") or ""),
        parent_section=str(meta.get("active_section") or ""),
        extra=extra or {},
    )


# ---------------------------------------------------------------------------
# Pure evaluators
# ---------------------------------------------------------------------------


def evaluate_new_agent_submission(payload: dict[str, Any]) -> SubmitDecision:
    """Pure: validate the new-agent form submission.

    Checks name format (regex) and model syntax pre-ack. Returns:
      - proceed=False + response_action=errors on format failure
      - proceed=True + response_action=clear on success (name-collision
        check is deferred to run_* to stay within the 3s budget; run_*
        reports via ephemeral on collision)

    No I/O.
    """
    meta = _get_meta(payload)
    values = _get_values(payload)

    name = _get_value(values, "new_agent__name", "new_agent__name")
    model = _get_value(values, "new_agent__model", "new_agent__model")
    system = _get_value(values, "new_agent__prompt", "new_agent__prompt")

    if not _AGENT_NAME_RE.match(name):
        return _error_decision(
            "new_agent__name",
            "Name must be 1–64 characters: letters, digits, hyphens, underscores.",
            meta=meta,
            payload=payload,
        )

    if model and model not in _allowed_model_ids():
        return _error_decision(
            "new_agent__model",
            f'Unknown model "{model}". Leave blank to use the default.',
            meta=meta,
            payload=payload,
        )

    return _success_decision(
        meta=meta,
        payload=payload,
        extra={
            "name": name,
            "model": model or "claude-sonnet-4-6",
            "system": system or None,
        },
    )


def evaluate_fork_agent_submission(payload: dict[str, Any]) -> SubmitDecision:
    """Pure: validate the fork-agent form submission.

    Checks new name format only. Source-agent existence and name collision
    are deferred to run_* (fast DB read but on the safe side of the budget).

    No I/O.
    """
    meta = _get_meta(payload)
    values = _get_values(payload)

    new_name = _get_value(values, "fork_agent__name", "fork_agent__name")
    source_name = str(meta.get("agent_name") or meta.get("selected_agent_name") or "")

    if not _AGENT_NAME_RE.match(new_name):
        return _error_decision(
            "fork_agent__name",
            "Name must be 1–64 characters: letters, digits, hyphens, underscores.",
            meta=meta,
            payload=payload,
        )

    return _success_decision(
        meta=meta,
        payload=payload,
        extra={"new_name": new_name, "source_name": source_name},
    )


def evaluate_edit_agent_submission(payload: dict[str, Any]) -> SubmitDecision:
    """Pure: validate the edit-agent form submission.

    Only model ID syntax is checked pre-ack (name is read-only; rename forbidden).
    System prompt is accepted as-is.

    No I/O.
    """
    meta = _get_meta(payload)
    values = _get_values(payload)

    model = _get_value(values, "edit_agent__model", "edit_agent__model")
    system = _get_value(values, "edit_agent__prompt", "edit_agent__prompt")

    if model and model not in _allowed_model_ids():
        return _error_decision(
            "edit_agent__model",
            f'Unknown model "{model}". Leave blank to use the default.',
            meta=meta,
            payload=payload,
        )

    return _success_decision(
        meta=meta,
        payload=payload,
        extra={
            "model": model or None,
            "system": system or None,
        },
    )


def evaluate_edit_repo_submission(payload: dict[str, Any]) -> SubmitDecision:
    """Pure: validate the edit-repo form submission.

    repo_url: optional; if provided, must look like a URL. Empty = keep current.
    pat:      optional (write-only, D-08). Empty = keep stored token (blank NEVER
              overwrites). Decision about keep-vs-replace is carried to run_* via
              extra["pat_replace"] flag.

    No I/O.
    """
    meta = _get_meta(payload)
    values = _get_values(payload)

    url = _get_value(values, "edit_repo__url", "edit_repo__url")
    pat = _get_value(values, "edit_repo__pat", "edit_repo__pat")

    if url and not _URL_RE.match(url) and "/" not in url:
        # Accept bare owner/repo form too; reject clearly-wrong patterns
        return _error_decision(
            "edit_repo__url",
            "Enter a GitHub repo URL (https://github.com/org/repo) or owner/repo.",
            meta=meta,
            payload=payload,
        )

    # D-08 / T-83-16: blank PAT = keep stored token; this flag is safe to log.
    pat_replace = bool(pat)

    return _success_decision(
        meta=meta,
        payload=payload,
        extra={
            "repo_url": url or None,
            # CRITICAL (D-08): pat value is only in extra so run_* can use it;
            # it MUST NOT appear in logs or any response_action field.
            "pat": pat or None,
            "pat_replace": pat_replace,
        },
    )


def evaluate_add_skill_submission(payload: dict[str, Any]) -> SubmitDecision:
    """Pure: validate the add-skill form submission.

    repo_url: required, basic URL format check. Reachability is slow I/O →
    accepted pre-ack; run_* reports via ephemeral on failure.

    No I/O.
    """
    meta = _get_meta(payload)
    values = _get_values(payload)

    repo_url = _get_value(values, "add_skill__repo_url", "add_skill__repo_url")
    branch = _get_value(values, "add_skill__branch", "add_skill__branch")

    if not repo_url:
        return _error_decision(
            "add_skill__repo_url",
            "A skill repo URL is required.",
            meta=meta,
            payload=payload,
        )

    if not _URL_RE.match(repo_url) and "/" not in repo_url:
        return _error_decision(
            "add_skill__repo_url",
            "Enter a GitHub repo URL (https://github.com/org/repo) or owner/repo.",
            meta=meta,
            payload=payload,
        )

    return _success_decision(
        meta=meta,
        payload=payload,
        extra={
            "repo_url": repo_url,
            "branch": branch or "main",
        },
    )


def evaluate_add_mcp_submission(payload: dict[str, Any]) -> SubmitDecision:
    """Pure: validate the add-MCP form submission.

    name: required; checked against reserved MCP server names.
    url:  required; basic URL check + deployment-endpoint check.
    token: optional (write-only). Empty = keep stored token (blank never overwrites).

    No I/O.
    """
    meta = _get_meta(payload)
    values = _get_values(payload)

    name = _get_value(values, "add_mcp__name", "add_mcp__name")
    url = _get_value(values, "add_mcp__url", "add_mcp__url")
    token = _get_value(values, "add_mcp__token", "add_mcp__token")

    if not name:
        return _error_decision(
            "add_mcp__name",
            "Server name is required.",
            meta=meta,
            payload=payload,
        )

    if not url:
        return _error_decision(
            "add_mcp__url",
            "Endpoint URL is required.",
            meta=meta,
            payload=payload,
        )

    if not _URL_RE.match(url):
        return _error_decision(
            "add_mcp__url",
            "Enter a valid HTTPS endpoint URL.",
            meta=meta,
            payload=payload,
        )

    # Reserved-name + deployment-endpoint rejection (port of modals_mcp.py copy).
    # public_url is not known at pure-eval time so we pass None (name check fires regardless).
    rejection = get_reserved_mcp_rejection(server_name=name, url=url, public_url=None)
    if rejection is not None:
        # Map the rejection to the appropriate block_id
        if "name" in rejection.lower() or "reserved" in rejection.lower():
            return _error_decision("add_mcp__name", rejection, meta=meta, payload=payload)
        return _error_decision("add_mcp__url", rejection, meta=meta, payload=payload)

    token_replace = bool(token)

    return _success_decision(
        meta=meta,
        payload=payload,
        extra={
            "mcp_name": name,
            "mcp_url": url,
            # CRITICAL (D-08): token value only in extra; never in logs or response.
            "token": token or None,
            "token_replace": token_replace,
        },
    )


def evaluate_paste_secrets_submission(payload: dict[str, Any]) -> SubmitDecision:
    """Pure: validate the paste-secrets form submission (D-09, T-83-15, T-83-17).

    Parses KEY=VALUE lines from paste_secrets__content. Validates:
    - Key names against _POSIX_KEY_RE
    - Total count against _SECRET_CAP (20)
    - Each value's byte length against _MAX_SECRET_VALUE_BYTES (4096)

    CRITICAL (D-09): the validated values are carried to run_* for the
    write ONLY via the extra dict. They MUST NOT appear in:
    - any response_action error string
    - any block_id or action_id
    - any log line

    The extra dict carries parsed pairs as a list[tuple[str, str]] ONLY;
    the caller (run_*) logs key names only, never values.

    No I/O.
    """
    meta = _get_meta(payload)
    values = _get_values(payload)

    content = _get_value(values, "paste_secrets__content", "paste_secrets__content")

    if not content:
        return _error_decision(
            "paste_secrets__content",
            "Paste at least one KEY=VALUE line.",
            meta=meta,
            payload=payload,
        )

    parsed: list[tuple[str, str]] = []
    bad_keys: list[str] = []

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue  # skip blank lines and comments
        if "=" not in line:
            return _error_decision(
                "paste_secrets__content",
                'Each line must be KEY=VALUE. Found a line without "=".',
                meta=meta,
                payload=payload,
            )
        key, _, value = line.partition("=")
        key = key.strip()
        # Value includes everything after the first "=" (values may contain "=").

        if not _POSIX_KEY_RE.match(key):
            bad_keys.append(key)
            continue

        # Byte-size cap per value (T-83-17)
        if len(value.encode()) > _MAX_SECRET_VALUE_BYTES:
            return _error_decision(
                "paste_secrets__content",
                # CRITICAL (D-09): only the KEY name appears; value length is safe.
                f"Value for key {key!r} exceeds the {_MAX_SECRET_VALUE_BYTES}-byte limit.",
                meta=meta,
                payload=payload,
            )

        parsed.append((key, value))

    if bad_keys:
        # Report only key NAMES — never values (D-09).
        bad_keys_str = ", ".join(bad_keys)
        return _error_decision(
            "paste_secrets__content",
            f"Invalid key name(s): {bad_keys_str}. "
            "Keys must start with a letter or underscore and contain only "
            "letters, digits, and underscores.",
            meta=meta,
            payload=payload,
        )

    if not parsed:
        return _error_decision(
            "paste_secrets__content",
            "No valid KEY=VALUE lines found.",
            meta=meta,
            payload=payload,
        )

    # Count cap (T-83-17) — checked after individual validation
    if len(parsed) > _SECRET_CAP:
        return _error_decision(
            "paste_secrets__content",
            f"Too many secrets: {len(parsed)} provided, maximum is {_SECRET_CAP}.",
            meta=meta,
            payload=payload,
        )

    return _success_decision(
        meta=meta,
        payload=payload,
        # CRITICAL (D-09): pairs carries values in memory only for write use.
        # run_paste_secrets logs only key names. Never log extra["pairs"].
        extra={"pairs": parsed},
    )


# ---------------------------------------------------------------------------
# Model-ID allow-list helper (deferred import to avoid circular imports)
# ---------------------------------------------------------------------------


def _allowed_model_ids() -> frozenset[str]:
    from daimon.core.constants import ALLOWED_MODEL_IDS

    return frozenset(ALLOWED_MODEL_IDS)


# ---------------------------------------------------------------------------
# Background run helpers
# ---------------------------------------------------------------------------


def _dev_allow_all_admin(runtime: SlackRuntime) -> bool:
    """Read the testing-only admin-gate override from settings (default False)."""
    slack = runtime.settings.slack
    return slack is not None and slack.dev_allow_all_admin


async def _refuse_non_admin(
    web_client: AsyncWebClient,
    *,
    team_id: str,
    channel_id: str,
    user_id: str,
    dev_allow_all: bool = False,
) -> bool:
    """Re-check is_admin server-side; send ephemeral and return True if non-admin.

    T-83-14: every mutating run_* calls this first. Returns True = caller
    should return early (refused). Returns False = admin confirmed, proceed.
    """
    is_admin = await resolve_is_admin(web_client, user_id=user_id, dev_allow_all=dev_allow_all)
    if not is_admin:
        await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id,
            user=user_id,
            text=":x: You no longer have permission to change agent setup.",
        )
        return True
    return False


# ---------------------------------------------------------------------------
# Background runs (post-ack I/O)
# ---------------------------------------------------------------------------


async def run_new_agent_submission(
    runtime: SlackRuntime,
    web_client: AsyncWebClient,
    *,
    team_id: str,
    user_id: str,
    channel_id: str,
    view_id: str,
    extra: dict[str, Any],
) -> None:
    """Post-ack: create a blank agent then refresh the L1 modal.

    Re-checks is_admin before any write (T-83-14). Name-collision guard
    lives in create_blank_agent (fast indexed MA read).
    """
    try:
        refused = await _refuse_non_admin(
            web_client,
            team_id=team_id,
            channel_id=channel_id,
            user_id=user_id,
            dev_allow_all=_dev_allow_all_admin(runtime),
        )
        if refused:
            return

        tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
        account_id = derive_guild_account_uuid(tenant_id=tenant_id)

        await create_blank_agent(
            runtime,
            tenant_id=tenant_id,
            name=str(extra.get("name") or ""),
            system=str(extra["system"]) if extra.get("system") else None,
            model=str(extra.get("model") or "claude-sonnet-4-6"),
            account_id=account_id,
        )

        log.info(
            "slack.agent_setup.new_agent.created",
            team_id=team_id,
            agent_name=extra.get("name"),
        )
        await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id,
            user=user_id,
            text=f":white_check_mark: Created agent `{extra.get('name')}`.",
        )
    except (DaimonError, anthropic.APIError, SlackApiError, SQLAlchemyError) as exc:
        log.error("slack.agent_setup.new_agent_failed", team_id=team_id, exc_info=exc)
        _capture(exc)
        await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id,
            user=user_id,
            text=f":x: Failed to create agent: {type(exc).__name__}",
        )


async def run_fork_agent_submission(
    runtime: SlackRuntime,
    web_client: AsyncWebClient,
    *,
    team_id: str,
    user_id: str,
    channel_id: str,
    view_id: str,
    extra: dict[str, Any],
) -> None:
    """Post-ack: fork an existing agent then refresh the L1 modal.

    Re-checks is_admin before any write (T-83-14).
    """
    try:
        refused = await _refuse_non_admin(
            web_client,
            team_id=team_id,
            channel_id=channel_id,
            user_id=user_id,
            dev_allow_all=_dev_allow_all_admin(runtime),
        )
        if refused:
            return

        tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
        account_id = derive_guild_account_uuid(tenant_id=tenant_id)

        await fork_agent(
            runtime,
            tenant_id=tenant_id,
            source_name=str(extra.get("source_name") or ""),
            new_name=str(extra.get("new_name") or ""),
            account_id=account_id,
        )

        log.info(
            "slack.agent_setup.fork_agent.created",
            team_id=team_id,
            source_name=extra.get("source_name"),
            new_name=extra.get("new_name"),
        )
        await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id,
            user=user_id,
            text=(
                f":white_check_mark: Forked `{extra.get('source_name')}` "
                f"→ `{extra.get('new_name')}`."
            ),
        )
    except (DaimonError, anthropic.APIError, SlackApiError, SQLAlchemyError) as exc:
        log.error("slack.agent_setup.fork_agent_failed", team_id=team_id, exc_info=exc)
        _capture(exc)
        await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id,
            user=user_id,
            text=f":x: Failed to fork agent: {type(exc).__name__}",
        )


async def run_edit_agent_submission(
    runtime: SlackRuntime,
    web_client: AsyncWebClient,
    *,
    team_id: str,
    user_id: str,
    channel_id: str,
    view_id: str,
    agent_name: str,
    parent_section: str,
    extra: dict[str, Any],
) -> None:
    """Post-ack: reconcile updated model/system for the agent.

    Re-checks is_admin before any write (T-83-14). Name field is read-only
    (rename-forbidden invariant, Structural Guarantee #6).
    """
    try:
        refused = await _refuse_non_admin(
            web_client,
            team_id=team_id,
            channel_id=channel_id,
            user_id=user_id,
            dev_allow_all=_dev_allow_all_admin(runtime),
        )
        if refused:
            return

        tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
        account_id = derive_guild_account_uuid(tenant_id=tenant_id)

        ma_agent = await find_agent_by_daimon_tag(
            runtime.anthropic, tenant_id=tenant_id, name=agent_name
        )
        if ma_agent is None:
            await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                channel=channel_id,
                user=user_id,
                text=f":x: Agent *{agent_name}* not found — it may have been deleted.",
            )
            return

        # apply_agent_modal is rename-forbidden by design.
        from daimon.adapters.slack.agent_setup.state import apply_agent_modal

        updates = apply_agent_modal(
            model_id=str(extra["model"]) if extra.get("model") else None,
            system_prompt=str(extra["system"]) if extra.get("system") else None,
        )

        current_params = ma_agent.model_dump(mode="json")
        spec = AgentSpec.model_validate(
            {
                "name": agent_name,
                "model": updates.get("model") or current_params.get("model", "claude-sonnet-4-6"),
                "system": updates.get("system") or current_params.get("system"),
            }
        )
        await call_reconcile_for_panel(
            runtime,
            tenant_id=tenant_id,
            spec=spec,
            guild_account_id=account_id,
        )

        log.info(
            "slack.agent_setup.edit_agent.reconciled",
            team_id=team_id,
            agent_name=agent_name,
        )
        await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id,
            user=user_id,
            text=f":white_check_mark: Updated `{agent_name}`. Takes effect on the next session.",
        )
    except (DaimonError, anthropic.APIError, SlackApiError, SQLAlchemyError) as exc:
        log.error(
            "slack.agent_setup.edit_agent_failed",
            team_id=team_id,
            agent_name=agent_name,
            exc_info=exc,
        )
        _capture(exc)
        await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id,
            user=user_id,
            text=f":x: Failed to update agent: {type(exc).__name__}",
        )


async def run_edit_repo_submission(
    runtime: SlackRuntime,
    web_client: AsyncWebClient,
    *,
    team_id: str,
    user_id: str,
    channel_id: str,
    view_id: str,
    agent_name: str,
    parent_section: str,
    extra: dict[str, Any],
) -> None:
    """Post-ack: update repo binding and/or inline PAT for the agent.

    Re-checks is_admin before any write (T-83-14).
    D-08 / T-83-16: blank PAT field = keep stored token (never overwrites).
    """
    try:
        refused = await _refuse_non_admin(
            web_client,
            team_id=team_id,
            channel_id=channel_id,
            user_id=user_id,
            dev_allow_all=_dev_allow_all_admin(runtime),
        )
        if refused:
            return

        tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
        account_id = derive_guild_account_uuid(tenant_id=tenant_id)

        ma_agent = await find_agent_by_daimon_tag(
            runtime.anthropic, tenant_id=tenant_id, name=agent_name
        )
        if ma_agent is None:
            await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                channel=channel_id,
                user=user_id,
                text=f":x: Agent *{agent_name}* not found — it may have been deleted.",
            )
            return

        agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(ma_agent.id))

        pat: str | None = extra.get("pat")  # type: ignore[assignment]
        pat_replace: bool = bool(extra.get("pat_replace"))

        if pat_replace and pat:
            # D-08: only replace when the user explicitly typed a new value.
            await store_inline_pat(
                runtime,
                account_id=account_id,
                agent_id=agent_uuid,
                plaintext_pat=pat,
            )
            from daimon.adapters.slack.agent_setup.write import mask_tail

            log.info(
                "slack.agent_setup.edit_repo.pat_replaced",
                team_id=team_id,
                agent_name=agent_name,
                masked=mask_tail(pat),
            )

        repo_url: str | None = extra.get("repo_url")  # type: ignore[assignment]
        if repo_url:
            if pat_replace:
                # A new PAT was typed — replace-the-secret path.
                from daimon.core.stores.agent_repo_binding import (
                    set_binding as set_agent_repo_binding,
                )

                async with runtime.sessionmaker.begin() as session:
                    await set_agent_repo_binding(
                        session,
                        tenant_id=tenant_id,
                        agent_id=agent_uuid,
                        repo_url=repo_url,
                        default_branch="main",
                        ma_secret_ref=f"inline-pat:{agent_uuid}",
                    )
            else:
                # Blank PAT — keep the existing token (T-83-16). Preserve
                # ma_secret_ref instead of clobbering it (PAT-CLOBBER).
                from daimon.core.errors import StoreError
                from daimon.core.stores.agent_repo_binding import (
                    set_binding as set_agent_repo_binding,
                )
                from daimon.core.stores.agent_repo_binding import (
                    update_repo_and_branch_keep_secret,
                )

                try:
                    async with runtime.sessionmaker.begin() as session:
                        await update_repo_and_branch_keep_secret(
                            session,
                            tenant_id=tenant_id,
                            agent_id=agent_uuid,
                            repo_url=repo_url,
                            default_branch="main",
                        )
                except StoreError:
                    # First-time bind with no prior secret to preserve —
                    # genuinely anon: (no PAT was ever provided). Write the
                    # anon: binding.
                    #
                    # No App-coverage probe here (unlike the discord panel): the
                    # Slack app.py create_session call does not thread
                    # session_factory, so the repo-clone path never runs on
                    # Slack today — advertising "App-covered" would be
                    # misleading. Wiring Slack repo clone (session_factory +
                    # fernet + env-mount) is a tracked follow-up.
                    async with runtime.sessionmaker.begin() as session:
                        await set_agent_repo_binding(
                            session,
                            tenant_id=tenant_id,
                            agent_id=agent_uuid,
                            repo_url=repo_url,
                            default_branch="main",
                            ma_secret_ref="anon:",
                        )
            log.info(
                "slack.agent_setup.edit_repo.bound",
                team_id=team_id,
                agent_name=agent_name,
                repo_url=repo_url,
            )

        await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id,
            user=user_id,
            text=f":white_check_mark: Saved repo + auth for `{agent_name}`.",
        )
    except (DaimonError, anthropic.APIError, SlackApiError, SQLAlchemyError) as exc:
        log.error(
            "slack.agent_setup.edit_repo_failed",
            team_id=team_id,
            agent_name=agent_name,
            exc_info=exc,
        )
        _capture(exc)
        await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id,
            user=user_id,
            text=f":x: Failed to update repo: {type(exc).__name__}",
        )


async def run_add_skill_submission(
    runtime: SlackRuntime,
    web_client: AsyncWebClient,
    *,
    team_id: str,
    user_id: str,
    channel_id: str,
    view_id: str,
    agent_name: str,
    parent_section: str,
    extra: dict[str, Any],
) -> None:
    """Post-ack: kick off skill sync for the given repo URL.

    Re-checks is_admin before any write (T-83-14). Repo reachability is slow
    I/O → accepted pre-ack; failures reported via ephemeral.
    """
    try:
        refused = await _refuse_non_admin(
            web_client,
            team_id=team_id,
            channel_id=channel_id,
            user_id=user_id,
            dev_allow_all=_dev_allow_all_admin(runtime),
        )
        if refused:
            return

        tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
        account_id = derive_guild_account_uuid(tenant_id=tenant_id)

        repo_url = str(extra.get("repo_url") or "")

        log.info(
            "slack.agent_setup.add_skill.syncing",
            team_id=team_id,
            agent_name=agent_name,
            repo_url=repo_url,
        )

        try:
            report = await kick_off_skill_sync(
                runtime,
                tenant_id=tenant_id,
                account_id=account_id,
                agent_name=agent_name,
                repo_url=repo_url,
            )
        except Exception as sync_err:
            log.error(
                "slack.agent_setup.add_skill.sync_failed",
                team_id=team_id,
                agent_name=agent_name,
                repo_url=repo_url,
                exc_info=sync_err,
            )
            await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                channel=channel_id,
                user=user_id,
                text=f":x: Skill sync failed for *{agent_name}*: `{type(sync_err).__name__}`",
            )
            return

        n_ok = report.synced + report.updated
        failures = [f"{name}: {reason}" for name, reason in report.failed_uploads] + [
            f"{repo}: {reason}" for repo, reason in report.skipped_repos
        ]
        if not failures:
            toast = f":white_check_mark: Synced {n_ok} skill(s) from {repo_url}."
        elif n_ok > 0:
            toast = f":warning: Synced {n_ok} skill(s), {len(failures)} failed: " + "; ".join(
                failures
            )
        else:
            toast = ":x: Sync failed: " + "; ".join(failures)

        await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id,
            user=user_id,
            text=toast,
        )
    except (DaimonError, anthropic.APIError, SlackApiError, SQLAlchemyError) as exc:
        log.error(
            "slack.agent_setup.add_skill_failed",
            team_id=team_id,
            agent_name=agent_name,
            exc_info=exc,
        )
        _capture(exc)
        await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id,
            user=user_id,
            text=f":x: Failed to add skill: {type(exc).__name__}",
        )


async def run_add_mcp_submission(
    runtime: SlackRuntime,
    web_client: AsyncWebClient,
    *,
    team_id: str,
    user_id: str,
    channel_id: str,
    view_id: str,
    agent_name: str,
    parent_section: str,
    extra: dict[str, Any],
) -> None:
    """Post-ack: add an MCP server entry to the agent and reconcile.

    Re-checks is_admin before any write (T-83-14).
    token: optional (write-only, D-08). Empty = keep stored credential.
    """
    try:
        refused = await _refuse_non_admin(
            web_client,
            team_id=team_id,
            channel_id=channel_id,
            user_id=user_id,
            dev_allow_all=_dev_allow_all_admin(runtime),
        )
        if refused:
            return

        tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
        account_id = derive_guild_account_uuid(tenant_id=tenant_id)

        mcp_name = str(extra.get("mcp_name") or "")
        mcp_url = str(extra.get("mcp_url") or "")
        token: str | None = extra.get("token")  # type: ignore[assignment]

        ma_agent = await find_agent_by_daimon_tag(
            runtime.anthropic, tenant_id=tenant_id, name=agent_name
        )
        if ma_agent is None:
            await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                channel=channel_id,
                user=user_id,
                text=f":x: Agent *{agent_name}* not found — it may have been deleted.",
            )
            return

        # Check public_url post-ack (now we have the actual deployment URL).
        public_url = (
            str(runtime.settings.mcp.public_url)
            if runtime.settings.mcp.public_url is not None
            else None
        )
        post_rejection = get_reserved_mcp_rejection(
            server_name=mcp_name, url=mcp_url, public_url=public_url
        )
        if post_rejection is not None:
            await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                channel=channel_id,
                user=user_id,
                text=f":x: {post_rejection}",
            )
            return

        current_params = ma_agent.model_dump(mode="json")  # pyright: ignore[reportUnknownMemberType]

        existing_mcp_servers: list[dict[str, Any]] = list(current_params.get("mcp_servers") or [])
        # BetaManagedAgentsURLMCPServerParams is a TypedDict — build the dict directly.
        existing_mcp_servers.append({"name": mcp_name, "type": "url", "url": mcp_url})

        spec = AgentSpec.model_validate(
            {
                "name": agent_name,
                "model": current_params.get("model", "claude-sonnet-4-6"),
                "system": current_params.get("system"),
                "mcp_servers": existing_mcp_servers,
            }
        )
        await call_reconcile_for_panel(
            runtime,
            tenant_id=tenant_id,
            spec=spec,
            guild_account_id=account_id,
        )

        # Write vault credential if token provided (D-08: blank = keep).
        if token:
            mcp = runtime.settings.mcp
            if mcp.public_url is not None and mcp.jwt_secret is not None:
                import datetime as dt

                from daimon.core.mcp_vault import add_external_mcp_credential
                from daimon.core.session_context import SessionContext

                agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(ma_agent.id))
                jwt_secret = mcp.jwt_secret.get_secret_value().encode()
                await add_external_mcp_credential(
                    runtime.anthropic,
                    account_id=account_id,
                    agent_id=agent_uuid,
                    jwt_secret=jwt_secret,
                    public_url=str(mcp.public_url),
                    mcp_server_url=mcp_url,
                    token=token,
                    now=dt.datetime.now(dt.UTC),
                    session_context=SessionContext(is_admin=True),
                )

        log.info(
            "slack.agent_setup.add_mcp.added",
            team_id=team_id,
            agent_name=agent_name,
            mcp_name=mcp_name,
            mcp_url=mcp_url,
            # CRITICAL (D-09): token value NEVER logged.
        )
        await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id,
            user=user_id,
            text=f":white_check_mark: Added MCP server `{mcp_name}` to `{agent_name}`.",
        )
    except (DaimonError, anthropic.APIError, SlackApiError, SQLAlchemyError) as exc:
        log.error(
            "slack.agent_setup.add_mcp_failed",
            team_id=team_id,
            agent_name=agent_name,
            exc_info=exc,
        )
        _capture(exc)
        await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id,
            user=user_id,
            text=f":x: Failed to add MCP server: {type(exc).__name__}",
        )


async def run_paste_secrets_submission(
    runtime: SlackRuntime,
    web_client: AsyncWebClient,
    *,
    team_id: str,
    user_id: str,
    channel_id: str,
    view_id: str,
    agent_name: str,
    parent_section: str,
    extra: dict[str, Any],
) -> None:
    """Post-ack: write validated secrets to agent_files store.

    Re-checks is_admin before any write (T-83-14).
    CRITICAL (D-09 / T-83-15): only key NAMES appear in logs and ephemeral.
    Secret values are consumed here and never propagated further.
    """
    try:
        refused = await _refuse_non_admin(
            web_client,
            team_id=team_id,
            channel_id=channel_id,
            user_id=user_id,
            dev_allow_all=_dev_allow_all_admin(runtime),
        )
        if refused:
            return

        tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)

        ma_agent = await find_agent_by_daimon_tag(
            runtime.anthropic, tenant_id=tenant_id, name=agent_name
        )
        if ma_agent is None:
            await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
                channel=channel_id,
                user=user_id,
                text=f":x: Agent *{agent_name}* not found — it may have been deleted.",
            )
            return

        agent_uuid = derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=str(ma_agent.id))

        pairs: list[tuple[str, str]] = list(extra.get("pairs") or [])
        if not pairs:
            return

        # Write each secret. Log ONLY the key name (D-09 / T-83-15).
        key_names_written: list[str] = []
        async with runtime.sessionmaker.begin() as session:
            for key, value in pairs:
                await put_agent_file(
                    session,
                    tenant_id=tenant_id,
                    agent_id=agent_uuid,
                    key=key,
                    content=value,
                )
                key_names_written.append(key)

        # Operator log: key NAMES only — never values (T-83-15).
        log.info(
            "slack.agent_setup.paste_secrets.written",
            team_id=team_id,
            agent_name=agent_name,
            keys=key_names_written,
            count=len(key_names_written),
        )

        n = len(key_names_written)
        if n == 1:
            confirm_text = (
                f":white_check_mark: Added `{key_names_written[0]}`. "
                "Takes effect on the next session."
            )
        else:
            confirm_text = (
                f":white_check_mark: Added {n} secrets. Takes effect on the next session."
            )
        await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id,
            user=user_id,
            text=confirm_text,
        )
    except (DaimonError, anthropic.APIError, SlackApiError, SQLAlchemyError) as exc:
        log.error(
            "slack.agent_setup.paste_secrets_failed",
            team_id=team_id,
            agent_name=agent_name,
            # CRITICAL (D-09): never include secret values or keys from extra in error logs.
            exc_info=exc,
        )
        _capture(exc)
        await web_client.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=channel_id,
            user=user_id,
            text=":x: Failed to write secrets. Check operator logs.",
        )


# ---------------------------------------------------------------------------
# Shared private helpers
# ---------------------------------------------------------------------------


def _capture(exc: Exception) -> None:
    """Capture exception to Sentry (via observability module)."""
    capture_exception_with_scope(exc)
