"""Agent-setup view_submission handler (pure evaluator + background run).

One form reaches this module: the New agent form the read-only panel pushes.
Every other change to an agent happens in the setup conversation, where the
chat tool owns the write and its authorization.

Two responsibilities:

1. ``evaluate_new_agent_submission`` (PURE, synchronous):
   Validates the view_submission payload within the 3-second Socket Mode ack
   deadline. Returns a ``SubmitDecision`` carrying the ``response_action``
   payload and whether the background run should proceed. No I/O.

2. ``run_new_agent_submission`` (async, background):
   Runs AFTER the ack: creates the agent and updates the open view to its
   Details, or restores the form with the reason it could not.

Pattern mirrors ``privacy_panel/submit.py`` — same Decision dataclass shape,
same evaluate-then-spawn discipline, same boundary catch tuple.

Creation is open to every workspace member: a brand-new agent is unrouted and
unshared, so there is nothing an admin has approved for it to put at risk.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any

import anthropic
import structlog
from daimon.adapters.slack.admin import resolve_is_admin
from daimon.adapters.slack.agent_setup.actions import load_agents_view
from daimon.adapters.slack.agent_setup.panel_views import (
    build_creating_view,
    build_details_view,
    build_new_agent_form,
)
from daimon.adapters.slack.agent_setup.read import (
    coding_tools_available,
    load_panel_details,
    load_panel_roster,
    resolve_attributions,
)
from daimon.adapters.slack.agent_setup.state import (
    PanelMetadata,
    decode_panel_metadata,
)
from daimon.adapters.slack.agent_setup.write import (
    create_blank_agent,
)
from daimon.adapters.slack.runtime import SlackRuntime
from daimon.core.constants import DEFAULT_AGENT_MODEL
from daimon.core.defaults.provisioning import derive_guild_account_uuid
from daimon.core.errors import DaimonError
from daimon.core.ma_identity import derive_tenant_uuid
from daimon.core.models_catalog import list_model_choices
from daimon.core.observability import capture_exception_with_scope
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient
from sqlalchemy.exc import SQLAlchemyError

log = structlog.get_logger()

# Agent name format: 1-64 chars, letters/digits/hyphens/underscores
_AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# ---------------------------------------------------------------------------
# Decision dataclass (mirrors privacy_panel/submit.py DeleteDecision)
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class SubmitDecision:
    """Result of ``evaluate_new_agent_submission``.

    response_payload: dict to pass as SocketModeResponse(payload=...) to ack.
    proceed:          True when validation passed and the background run should fire.
    team_id:          Workspace ID from the view's metadata (background run routing).
    user_id:          Submitting Slack user ID (background run admin re-check).
    extra:            Fields the background run needs, keyed by name.
    panel_meta:       The form's decoded panel metadata, or None when the view
                      carried metadata this deployment can no longer read.
    """

    response_payload: dict[str, Any]
    proceed: bool
    team_id: str
    user_id: str
    extra: dict[str, Any]
    panel_meta: PanelMetadata | None = None


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _get_view(payload: dict[str, Any]) -> dict[str, Any]:
    return payload.get("view") or {}


def _get_values(payload: dict[str, Any]) -> dict[str, Any]:
    view = _get_view(payload)
    view_state: dict[str, Any] = view.get("state") or {}
    return view_state.get("values") or {}


def _get_value(values: dict[str, Any], block_id: str, action_id: str) -> str:
    """Extract and strip a plain_text_input value from state.values."""
    block: dict[str, Any] = values.get(block_id) or {}
    element: dict[str, Any] = block.get(action_id) or {}
    return str(element.get("value") or "").strip()


def _get_selected_option_value(values: dict[str, Any], block_id: str, action_id: str) -> str:
    """Extract a static_select's selected option value from state.values."""
    block: dict[str, Any] = values.get(block_id) or {}
    element: dict[str, Any] = block.get(action_id) or {}
    selected: dict[str, Any] = element.get("selected_option") or {}
    return str(selected.get("value") or "").strip()


def _get_user_id(payload: dict[str, Any]) -> str:
    user: dict[str, Any] = payload.get("user") or {}
    return str(user.get("id") or "")


# ---------------------------------------------------------------------------
# Pure evaluators
# ---------------------------------------------------------------------------


def evaluate_new_agent_submission(payload: dict[str, Any]) -> SubmitDecision:
    """Pure: validate the new-agent form submission.

    Checks name format and model membership pre-ack, then acks with
    ``response_action: update`` so the form becomes the "creating…" view and
    the person stays inside the panel while the create runs. The
    name-collision check is deferred to ``run_new_agent_submission``, which
    restores this form with a banner when it fires — it needs an MA read and
    the ack budget is three seconds.

    No I/O.
    """
    view = _get_view(payload)
    meta = decode_panel_metadata(str(view.get("private_metadata") or ""))
    values = _get_values(payload)

    name = _get_value(values, "new_agent__name", "new_agent__name")
    # A real static_select over the model catalog, not free text — read the
    # selected option's value. A stale client (an already-open form submitted
    # after a catalog change) can still send an id outside the allow-list, so
    # this stays validated exactly like the old free-text field was.
    model = _get_selected_option_value(values, "new_agent__model", "new_agent__model")
    purpose = _get_value(values, "new_agent__prompt", "new_agent__prompt")

    if meta is None:
        # A form from before this panel shipped, or metadata Slack truncated:
        # there is no view to update and no root to refresh, so say so instead
        # of creating an agent nobody can be shown.
        return _new_agent_error(
            payload,
            meta=None,
            block_id="new_agent__name",
            text="This form is out of date. Close it and run /agent-setup again.",
        )

    if not _AGENT_NAME_RE.match(name):
        return _new_agent_error(
            payload,
            meta=meta,
            block_id="new_agent__name",
            text="Name must be 1–64 characters: letters, digits, hyphens, underscores.",
        )

    if not model or model not in _allowed_model_ids():
        return _new_agent_error(
            payload,
            meta=meta,
            block_id="new_agent__model",
            text=f'Unknown model "{model}". Choose one from the list.',
        )

    return SubmitDecision(
        response_payload={
            "response_action": "update",
            "view": build_creating_view(
                agent_name=name,
                meta=meta.with_view("creating", agent_name=name),
            ),
        },
        proceed=True,
        team_id=meta.team_id,
        user_id=_get_user_id(payload),
        extra={"name": name, "purpose": purpose or None, "model": model},
        panel_meta=meta,
    )


def _new_agent_error(
    payload: dict[str, Any],
    *,
    meta: PanelMetadata | None,
    block_id: str,
    text: str,
) -> SubmitDecision:
    """A field error on the new-agent form: the form stays open, nothing runs."""
    return SubmitDecision(
        response_payload={"response_action": "errors", "errors": {block_id: text}},
        proceed=False,
        team_id=meta.team_id if meta is not None else "",
        user_id=_get_user_id(payload),
        extra={},
        panel_meta=meta,
    )


# ---------------------------------------------------------------------------
# Model-ID allow-list helper (deferred import to avoid circular imports)
# ---------------------------------------------------------------------------


def _allowed_model_ids() -> frozenset[str]:
    from daimon.core.constants import ALLOWED_MODEL_IDS

    return frozenset(ALLOWED_MODEL_IDS)


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
    meta: PanelMetadata,
    name: str,
    purpose: str | None,
    model: str,
) -> None:
    """Post-ack: create the agent, then show its Details in the same view.

    Creation is open to every workspace member: a new agent has no propagation
    row and no config tier pointing at it, so there is nothing an admin has
    approved to protect, and no turn is admitted or billed. The name-collision
    guard lives in ``create_blank_agent`` (one indexed MA read); when it fires
    the form comes back carrying what was typed and the reason, rather than an
    ephemeral beside a modal that has already closed.

    On success the view the form became shows Details for the new agent —
    which says, honestly, that it does not answer anywhere yet — and the root
    Agents view behind it is refreshed so the new row is there on the way back.
    """
    try:
        tenant_id = derive_tenant_uuid(platform="slack", workspace_id=team_id)
        account_id = derive_guild_account_uuid(tenant_id=tenant_id)

        try:
            outcome = await create_blank_agent(
                runtime,
                tenant_id=tenant_id,
                name=name,
                system=purpose,
                model=model or DEFAULT_AGENT_MODEL,
                account_id=account_id,
            )
            if outcome.anthropic_id is None:
                raise DaimonError(
                    "Agent creation did not return an identity. Reopen setup and retry."
                )
        except (DaimonError, anthropic.APIError, SQLAlchemyError) as exc:
            log.error("slack.agent_setup.new_agent_failed", team_id=team_id, exc_info=exc)
            _capture(exc)
            await web_client.views_update(  # pyright: ignore[reportUnknownMemberType]
                view_id=view_id,
                view=build_new_agent_form(
                    meta=meta.with_view("new_agent", root_view_id=meta.root_view_id),
                    model_choices=list_model_choices(default=DEFAULT_AGENT_MODEL),
                    initial_name=name,
                    initial_purpose=purpose,
                    initial_model=model,
                    error=str(exc),
                ),
            )
            return

        log.info("slack.agent_setup.new_agent.created", team_id=team_id, agent_name=name)

        is_admin = await resolve_is_admin(web_client, user_id=user_id)
        async with runtime.sessionmaker() as session:
            roster = await load_panel_roster(
                session,
                runtime.anthropic,
                tenant_id=tenant_id,
                channel_id=channel_id or None,
                thread_id=None,
                default=runtime.deployment_default,
            )
            details = await load_panel_details(
                session,
                runtime.anthropic,
                runtime,
                tenant_id=tenant_id,
                roster=roster,
                agent_name=name,
                channel_id=channel_id,
                thread_id=None,
                is_admin=is_admin,
            )
            attributions = await resolve_attributions(
                session,
                tenant_id=tenant_id,
                account_ids=[
                    row.created_by_account_id for row in roster.rows if row.created_by_account_id
                ],
            )

        if details is not None:
            await web_client.views_update(  # pyright: ignore[reportUnknownMemberType]
                view_id=view_id,
                view=build_details_view(
                    details,
                    meta=meta.with_view("details", agent_name=name),
                    is_admin=is_admin,
                    coding_tools_available=coding_tools_available(runtime),
                    channel_id=channel_id,
                    attribution=attributions.get(details.created_by_account_id)
                    if details.created_by_account_id
                    else None,
                ),
            )

        if meta.root_view_id:
            # The root is a separate view in Slack's stack: it keeps the page
            # the reader left, now with the new agent in it.
            await web_client.views_update(  # pyright: ignore[reportUnknownMemberType]
                view_id=meta.root_view_id,
                view=await load_agents_view(
                    runtime,
                    tenant_id=tenant_id,
                    meta=meta.with_view("agents").with_page(meta.page),
                    is_admin=is_admin,
                ),
            )
    except (DaimonError, anthropic.APIError, SlackApiError, SQLAlchemyError) as exc:
        log.error("slack.agent_setup.new_agent_render_failed", team_id=team_id, exc_info=exc)
        _capture(exc)


# ---------------------------------------------------------------------------
# Shared private helpers
# ---------------------------------------------------------------------------


def _capture(exc: Exception) -> None:
    """Capture exception to Sentry (via observability module)."""
    capture_exception_with_scope(exc)
