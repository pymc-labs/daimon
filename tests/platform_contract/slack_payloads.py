"""Slack payloads built by the adapter's own builders.

Builders run with the same fakes the adapter tests use (`test_panel_views`,
`test_here`, and a transport-level `aioresponses` fake for the lifecycle). An entry
marked RECONSTRUCTED is assembled from real parts by hand because the real call
needs live state; there are none such today.

Not covered, because the payload only exists inside a handler with live Slack or
database state: credential-request and OAuth cards posted from handlers,
provider action blocks, budget notices, output-delivery posts, GitHub waiting and
new-repo views, avatar views, `/memory` and `/github` replies.
"""

from __future__ import annotations

import asyncio
import copy
import functools
import inspect
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from aioresponses import aioresponses
from daimon.core.credential_requests import CredentialRequestKind
from slack_sdk.web.async_client import AsyncWebClient

from .loaders import SLACK_TESTS, load_test_module, run_async
from .registry import entry


def _panel_tests() -> Any:
    return load_test_module(SLACK_TESTS / "agent_setup" / "test_panel_views.py")


def _here_tests() -> Any:
    return load_test_module(SLACK_TESTS / "test_here.py")


# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------


@entry("feedback_modal", "slack", "view")
def feedback_modal() -> Any:
    from daimon.adapters.slack.feedback import build_feedback_modal

    return build_feedback_modal(channel_id="C0BRY43TL91", message_ts="1.2", thread_ts="1.0")


@entry("feedback_modal_shared", "slack", "view")
def feedback_modal_shared() -> Any:
    from daimon.adapters.slack.feedback import build_feedback_modal

    return build_feedback_modal(
        channel_id="C0BRY43TL91", message_ts="1.2", thread_ts="1.0", shared=True
    )


@entry("support_modal", "slack", "view")
def support_modal() -> Any:
    from daimon.adapters.slack.support_escalation import build_support_modal

    return build_support_modal(
        channel_id="C1", message_ts="1.2", thread_ts="1.0", remaining=3, sealed=True
    )


def _credential_modal(kind: CredentialRequestKind) -> Any:
    from daimon.adapters.slack.credential_requests import build_credential_modal
    from daimon.core.credential_requests import ENV_FILE_TARGET, build_skill_repo_target

    targets = {
        "env": "OPENAI_API_KEY",
        "env_file": ENV_FILE_TARGET,
        "mcp": "my-server",
        "repo": build_skill_repo_target("https://github.com/owner/repo", "main", ""),
        "skill_repo": build_skill_repo_target("https://github.com/owner/skills", "main", "skills"),
    }
    return build_credential_modal(
        kind=kind,
        token="tok_test",
        channel_id="C1",
        message_ts="1.2",
        target=targets[kind],
        agent_name="research-bot",
        mcp_server_url="https://mcp.example.com",
    )


_CREDENTIAL_KINDS: tuple[CredentialRequestKind, ...] = (
    "env",
    "env_file",
    "mcp",
    "repo",
    "skill_repo",
)
for _kind in _CREDENTIAL_KINDS:
    entry(f"credential_modal_{_kind}", "slack", "view")(lambda kind=_kind: _credential_modal(kind))


@entry("notice_modal", "slack", "view")
def notice_modal() -> Any:
    from daimon.adapters.slack.click_replies import notice_modal

    return notice_modal(title="Checking…", text="One moment.")


@entry("generic_error_view", "slack", "view")
def generic_error_view() -> Any:
    from daimon.adapters.slack.errors import build_error_view

    return build_error_view(
        RuntimeError("boom"), title="Billing", request_id="01ARZ3NDEKTSV4RRFFQ69G5FAV"
    )


# ---------------------------------------------------------------------------
# Privacy panel
# ---------------------------------------------------------------------------


@entry("privacy_main", "slack", "view")
def privacy_main() -> Any:
    from daimon.adapters.slack.privacy_panel.views import build_privacy_main_container

    return build_privacy_main_container(
        is_slack_connected=True,
        slack_connect_url="https://example.com/c",
        policy_url="https://example.com/p",
        display_name="research-bot",
    )


@entry("privacy_main_unconnected", "slack", "view")
def privacy_main_unconnected() -> Any:
    from daimon.adapters.slack.privacy_panel.views import build_privacy_main_container

    return build_privacy_main_container(
        is_slack_connected=False,
        slack_connect_url="https://example.com/c",
        policy_url="https://example.com/p",
    )


@entry("privacy_loading", "slack", "view")
def privacy_loading() -> Any:
    from daimon.adapters.slack.privacy_panel.views import build_loading_view

    return build_loading_view()


def _purge_preview() -> Any:
    from daimon.core.privacy import PurgePreview, PurgePreviewRow

    names = [
        "linked_principals",
        "principal_links",
        "routines",
        "user_configs",
        "account",
        "user_skills",
        "github_user_links",
        "mcp_tokens",
        "slack_user_tokens",
        "slack_turn_contexts",
        "credential_requests",
        "wizard_sessions",
        "message_feedback",
        "support_escalations",
    ]
    rows = {
        name: PurgePreviewRow(count=1 if name == "account" else 2, example=None) for name in names
    }
    return PurgePreview(**rows)


@entry("privacy_delete_modal", "slack", "view")
def privacy_delete_modal() -> Any:
    from daimon.adapters.slack.privacy_panel.views import build_delete_modal

    return build_delete_modal(
        _purge_preview(),
        account_id=uuid.uuid4(),
        user_name="alice",
        view_id="V_MAIN_PRIV_01",
        display_name="research-bot",
    )


@entry("privacy_post_delete", "slack", "view")
def privacy_post_delete() -> Any:
    from daimon.adapters.slack.privacy_panel.views import build_post_delete_view
    from daimon.core.ma import SessionDeletionReport
    from daimon.core.purge import AccountPurgeResult, PurgeReport

    result = AccountPurgeResult(
        db=PurgeReport(accounts=1),
        sessions=SessionDeletionReport(failed=3, upstream_error=False),
    )
    return build_post_delete_view(result, display_name="research-bot")


@entry("privacy_export_result", "slack", "view")
def privacy_export_result() -> Any:
    from daimon.adapters.slack.privacy_panel.views import build_export_result_view

    return build_export_result_view(summary="3 routines, 2 skills", display_name="research-bot")


# ---------------------------------------------------------------------------
# Billing panel
# ---------------------------------------------------------------------------


def _billing_state(*, admin: bool) -> Any:
    from daimon.core.billing_panel import BillingPanelState, MemberRow

    members = (
        MemberRow(
            platform_user_id="U_TOP",
            display_name="User U_TOP",
            cost_usd=8.0,
            turn_count=15,
            is_caller=False,
        ),
        MemberRow(
            platform_user_id="U_ADMIN",
            display_name="User DMIN",
            cost_usd=1.5,
            turn_count=3,
            is_caller=True,
        ),
    )
    return BillingPanelState(
        is_admin=admin,
        caller_user_id="U_ADMIN",
        caller_spend=1.5,
        caller_turns=3,
        caller_cap=None if admin else Decimal("5.00"),
        guild_balance_usd=Decimal("50.00"),
        guild_spend=10.0,
        guild_turns=20,
        guild_distinct_members=2,
        member_rows=members if admin else (),
        over_cap_count=0,
    )


def _billing_view(*, admin: bool) -> Any:
    from daimon.adapters.slack.billing_panel.views import build_billing_view

    return build_billing_view(
        _billing_state(admin=admin),
        now=datetime(2025, 1, 15, tzinfo=UTC),
        since=datetime(2025, 1, 1, tzinfo=UTC),
        channel_id="C1",
    )


entry("billing_view_admin", "slack", "view")(lambda: _billing_view(admin=True))
entry("billing_view_member", "slack", "view")(lambda: _billing_view(admin=False))


@entry("billing_redeem_modal", "slack", "view")
def billing_redeem_modal() -> Any:
    from daimon.adapters.slack.billing_panel.redeem import build_redeem_modal

    return build_redeem_modal(
        root_view_id="V1", channel_id="C1", error="That code was not recognised."
    )


@entry("billing_expiry_view", "slack", "view")
def billing_expiry_view() -> Any:
    from daimon.adapters.slack.billing_panel.views import build_expiry_view
    from daimon.core.promo_credit import ActiveTimedCredit

    credit = ActiveTimedCredit(
        remaining_usd=Decimal("20.00"), ends_at=datetime(2026, 12, 1, tzinfo=UTC)
    )
    return build_expiry_view([credit])


# ---------------------------------------------------------------------------
# Routines panel
# ---------------------------------------------------------------------------


def _routines_state() -> Any:
    from daimon.adapters.slack.routines_panel.state import RoutineEntry, RoutinesPanelState
    from daimon.core.stores.domain import RoutineRow

    now = datetime.now(UTC)
    row = RoutineRow(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        created_by_user_id=None,
        agent_id="agent-x",
        agent_name="Alpha",
        cron_expr="0 * * * *",
        timezone="UTC",
        trigger_message="My routine",
        enabled=True,
        next_fire_at=None,
        last_fired_at=None,
        last_error=None,
        last_result_tail=None,
        created_at=now,
        updated_at=now,
    )
    routine = RoutineEntry(routine=row, agent_name="Alpha", glyph="⏳", label="My routine")
    return RoutinesPanelState(rows=[routine], over_cap_count=0, agent_name_map={})


@entry("routines_content", "slack", "view")
def routines_content() -> Any:
    from daimon.adapters.slack.routines_panel.views import build_content_view

    return build_content_view(_routines_state(), channel_id="C1")


@entry("routines_loading", "slack", "view")
def routines_loading() -> Any:
    from daimon.adapters.slack.routines_panel.views import build_loading_view

    return build_loading_view(channel_id="C1")


@entry("routines_new_routine_modal", "slack", "view")
def routines_new_routine_modal() -> Any:
    from daimon.adapters.slack.routines_panel.views import build_create_routine_modal

    return build_create_routine_modal(team_id="T1", channel_id="C1", agent_names=["Alpha", "Beta"])


@entry("routines_delete_confirm", "slack", "view")
def routines_delete_confirm() -> Any:
    from daimon.adapters.slack.routines_panel.views import build_delete_confirm_modal

    parameters = inspect.signature(build_delete_confirm_modal).parameters
    wanted = {
        "team_id": "T1",
        "channel_id": "C1",
        "routine_id": str(uuid.uuid4()),
        "root_view_id": "V1",
        "label": "My routine",
    }
    return build_delete_confirm_modal(
        **{key: value for key, value in wanted.items() if key in parameters}
    )


# ---------------------------------------------------------------------------
# Agent setup panel
# ---------------------------------------------------------------------------


@entry("agent_setup_agents_view", "slack", "view")
def agent_setup_agents_view() -> Any:
    from daimon.adapters.slack.agent_setup.panel_views import build_agents_view

    tests = _panel_tests()
    answering = tests._roster_agent("zebra-bot")
    roster = tests._roster(
        answering,
        tests._roster_agent("alpha-bot"),
        tests._roster_agent("beta-bot"),
        answering=answering,
    )
    return build_agents_view(
        roster,
        page=tests._page(roster),
        meta=tests._meta(),
        is_admin=True,
        attributions={},
        channel_id=tests._CHANNEL_ID,
    )


@entry("agent_setup_details_view", "slack", "view")
def agent_setup_details_view() -> Any:
    from daimon.adapters.slack.agent_setup.panel_views import build_details_view

    tests = _panel_tests()
    details = tests._details(
        name="research-bot",
        binding=tests._binding(),
        skills=[{"type": "custom", "skill_id": "s1", "version": "1"}],
    )
    return build_details_view(
        details,
        meta=tests._meta(),
        is_admin=True,
        coding_tools_available=True,
        channel_id=tests._CHANNEL_ID,
        attribution=None,
        avatar_url="https://example.test/avatars/token/hash.png",
    )


@entry("agent_setup_details_view_long_name", "slack", "view")
def agent_setup_details_view_long_name() -> Any:
    from daimon.adapters.slack.agent_setup.panel_views import build_details_view

    tests = _panel_tests()
    return build_details_view(
        tests._details(name="a-very-long-agent-name-that-overflows"),
        meta=tests._meta(),
        is_admin=True,
        coding_tools_available=True,
        channel_id=tests._CHANNEL_ID,
        attribution=None,
    )


@entry("agent_setup_routing_view", "slack", "view")
def agent_setup_routing_view() -> Any:
    from daimon.adapters.slack.agent_setup.panel_views import build_routing_view
    from daimon.adapters.slack.agent_setup.state import PANEL_PAGE_SIZE
    from daimon.core.roster import paginate
    from daimon.core.scope import ChannelConfigRow, TenantConfigRow

    tests = _panel_tests()
    channel = ChannelConfigRow(
        tenant_id=tests._TENANT_ID,
        channel_id=tests._CHANNEL_ID,
        agent_name="research-bot",
        agent_name_set_by_account_id=tests._MAKER,
        agent_name_set_at=tests._MOMENT,
    )
    answering_map = tests._answering_map(
        channels=[channel],
        tenant=TenantConfigRow(tenant_id=tests._TENANT_ID, agent_name="daimon"),
        deployment_default="daimon",
    )
    return build_routing_view(
        answering_map,
        page=paginate(answering_map.channel_overrides, page=0, page_size=PANEL_PAGE_SIZE),
        meta=tests._meta(),
        is_admin=True,
        attributions={tests._MAKER: "<@U1>"},
        setup_links=[],
        channel_id=tests._CHANNEL_ID,
        unrouted_agent_name=None,
    )


def _model_choices() -> Any:
    from daimon.core.models_catalog import list_model_choices

    return list_model_choices(default="claude-sonnet-4-6")


@entry("agent_setup_new_agent_modal", "slack", "view")
def agent_setup_new_agent_modal() -> Any:
    from daimon.adapters.slack.agent_setup.panel_views import build_new_agent_form

    return build_new_agent_form(meta=_panel_tests()._meta(), model_choices=_model_choices())


@entry("agent_setup_new_agent_modal_error", "slack", "view")
def agent_setup_new_agent_modal_error() -> Any:
    from daimon.adapters.slack.agent_setup.panel_views import build_new_agent_form

    return build_new_agent_form(
        meta=_panel_tests()._meta(),
        model_choices=_model_choices(),
        initial_name="churn explorer",
        initial_purpose="Explore churn",
        error="Name taken.",
    )


@entry("agent_setup_github_home", "slack", "view")
def agent_setup_github_home() -> Any:
    from daimon.adapters.slack.agent_setup.panel_views import build_github_home_view

    return build_github_home_view(
        _panel_tests()._meta(),
        agent_counts=(("helper", 1), ("other", 0)),
        can_choose_agent=True,
    )


@entry("agent_setup_operator_token_form", "slack", "view")
def agent_setup_operator_token_form() -> Any:
    from daimon.adapters.slack.agent_setup.panel_views import build_operator_token_form

    return build_operator_token_form(meta=_panel_tests()._meta())


@entry("agent_setup_add_skill_form", "slack", "view")
def agent_setup_add_skill_form() -> Any:
    from daimon.adapters.slack.agent_setup.panel_views import build_add_skill_form

    return build_add_skill_form(meta=_panel_tests()._meta())


@entry("agent_setup_channel_skills_form", "slack", "view")
def agent_setup_channel_skills_form() -> Any:
    from daimon.adapters.slack.agent_setup.panel_views import build_channel_skills_form

    return build_channel_skills_form(meta=_panel_tests()._meta(), rows=[])


@entry("agent_setup_creating_view", "slack", "view")
def agent_setup_creating_view() -> Any:
    from daimon.adapters.slack.agent_setup.panel_views import build_creating_view

    return build_creating_view(agent_name="research-bot", meta=_panel_tests()._meta())


@entry("agent_setup_error_view", "slack", "view")
def agent_setup_error_view() -> Any:
    from daimon.adapters.slack.agent_setup.panel_views import build_error_view

    return build_error_view(request_id="01ARZ3NDEKTSV4RRFFQ69G5FAV")


# ---------------------------------------------------------------------------
# Message blocks: help, /here, hand-over, cards
# ---------------------------------------------------------------------------


@entry("help_blocks", "slack", "blocks")
def help_blocks() -> Any:
    from daimon.adapters.slack.help import build_help_blocks

    return build_help_blocks(display_name="research-bot")


@entry("here_attachment_channel", "slack", "message")
def here_attachment_channel() -> Any:
    from daimon.adapters.slack.here import build_here_attachment

    attachment = build_here_attachment(_here_tests()._card("channel"))
    return {"attachments": [attachment]}


@entry("here_attachment_no_view", "slack", "message")
def here_attachment_no_view() -> Any:
    from daimon.adapters.slack.here import build_here_attachment

    attachment = build_here_attachment(_here_tests()._card("no_view"))
    return {"attachments": [attachment]}


@entry("thread_hand_over_blocks", "slack", "blocks")
def thread_hand_over_blocks() -> Any:
    from daimon.adapters.slack.thread_handoff import build_hand_over_blocks

    return build_hand_over_blocks(
        text="This thread is with Atlas.\nStart a new thread to change agent.",
        agent_id="agent-1",
        agent_name="research-bot",
    )


@entry("github_card_with_buttons", "slack", "blocks")
def github_card_with_buttons() -> Any:
    """The card is built by the real `github_card_blocks`; the two buttons are hand-built.

    RECONSTRUCTED buttons: the handlers in `github_requests.py` (link and cancel) and
    `github_new_repo.py` assemble their button dicts inline, so there is no builder to call.
    """
    from daimon.adapters.slack.agent_setup.github_card_ui import github_card_blocks

    buttons = [
        {
            "type": "button",
            "action_id": "github_personal__open",
            "url": "https://github.com/login/oauth/authorize?state=x",
            "text": {"type": "plain_text", "text": "Get a new link"},
        },
        {
            "type": "button",
            "action_id": "github_request__decision",
            "value": "request-id:cancel",
            "text": {"type": "plain_text", "text": "Cancel request"},
        },
    ]
    return github_card_blocks("GitHub is connected.\nrepo acme/widgets", buttons=buttons)


@entry("coding_tools_ephemeral", "slack", "blocks")
def coding_tools_ephemeral() -> Any:
    from daimon.adapters.slack.agent_setup.coding_tools import render_coding_tools_message

    _, blocks = render_coding_tools_message(
        agent_name="research-bot",
        public_url="https://mcp.example.com/mcp",
        jwt="eyJ.fake.jwt",
        jti=uuid.uuid4(),
        channel_id="C1",
    )
    return blocks


@entry("vote_buttons_only", "slack", "blocks")
def vote_buttons_only() -> Any:
    from daimon.adapters.slack.feedback import build_feedback_actions_block

    return [build_feedback_actions_block()]


@entry("feedback_details_prompt_ephemeral", "slack", "blocks")
def feedback_details_prompt_ephemeral() -> Any:
    from daimon.adapters.slack.feedback import (
        _AnswerPlace,  # pyright: ignore[reportPrivateUsage]
        _details_prompt_blocks,  # pyright: ignore[reportPrivateUsage]
    )

    return _details_prompt_blocks(_AnswerPlace(channel_id="C1", message_ts="1.2", thread_ts="1.0"))


def _confirmation_attachments(card_state: str) -> Any:
    from daimon.adapters.slack.tool_confirmation import (
        _attachment,  # pyright: ignore[reportPrivateUsage]
    )
    from daimon.core.confirmation import ConfirmationPrompt
    from daimon.core.posted_controls.confirmation import build_confirmation_card

    prompt = ConfirmationPrompt(
        title="Confirm this action?",
        requester_platform_user_id="U_TEST",
        expires_at=datetime(2026, 10, 10, tzinfo=UTC),
    )
    token = "pending-card" if card_state == "pending" else None
    card = build_confirmation_card(prompt, state=card_state, token=token)  # pyright: ignore[reportArgumentType]
    return {"attachments": _attachment(card, prompt)}


for _state in ("pending", "approved", "denied", "expired"):
    entry(f"confirmation_card_{_state}", "slack", "message")(
        lambda state=_state: _confirmation_attachments(state)
    )


# ---------------------------------------------------------------------------
# Turn card
# ---------------------------------------------------------------------------


def _card_state(phase: str, **fields: Any) -> Any:
    from daimon.core.turn.card_state import CardState, TurnPhase

    return CardState(
        phase=getattr(TurnPhase, phase), agent_name="Atlas", started_at=100.0, **fields
    )


@entry("turn_card_working_with_cancel", "slack", "blocks")
def turn_card_working_with_cancel() -> Any:
    from daimon.adapters.slack.blockkit import to_blocks

    state = _card_state(
        "TOOL_RUNNING",
        plan_lines=("☑️ Load the data", "⏳ Fit the model", "\u2007\u2007\u2007Write it up"),
        tool_lines=("+3 earlier", "☑️ Read a file", "⏳ Running a command"),
        text_preview="Preview text.",
    )
    return to_blocks(state, now=120.0, cancel_key="turnkey123")


@entry("turn_card_done", "slack", "blocks")
def turn_card_done() -> Any:
    from daimon.adapters.slack.blockkit import to_blocks

    return to_blocks(_card_state("DONE", cost_str="$0.04"), now=142.0)


@entry("turn_card_error", "slack", "blocks")
def turn_card_error() -> Any:
    from daimon.adapters.slack.blockkit import to_blocks

    return to_blocks(_card_state("ERROR", error_reason="rate limited"), now=112.0)


@entry("turn_interrupted", "slack", "blocks")
def turn_interrupted() -> Any:
    from daimon.adapters.slack.blockkit import to_interrupted_blocks

    return to_interrupted_blocks()


# ---------------------------------------------------------------------------
# Real lifecycle: what the adapter actually sends to chat.postMessage / chat.update
# ---------------------------------------------------------------------------

_ANSWER = "## Answer\n\nHere is **the answer** with a list:\n- one\n- two"
_ANSWER_WITH_TABLE = "Intro\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"


async def _drive_lifecycle(answer: str, *, render_tables: bool) -> list[tuple[str, dict[str, Any]]]:
    """Run a turn through `SlackTurnLifecycle` against a transport-level fake.

    Returns the (method, JSON body) of every `chat.postMessage` / `chat.update` call, in order.
    """
    from daimon.adapters.slack.lifecycle import SlackTurnLifecycle
    from daimon.core.turn.state import TextBlock, ToolUseBlock, TurnState, UsageTotals

    ok = {"ok": True, "ts": "1000000000.000001", "channel": "C_TEST"}
    with aioresponses() as mock:
        for method in ("chat.postMessage", "chat.update"):
            mock.post(f"https://slack.com/api/{method}", payload=ok, repeat=True)  # pyright: ignore[reportUnknownMemberType]
        lifecycle = SlackTurnLifecycle(
            client=AsyncWebClient(token="xoxb-test"),
            channel="C_TEST",
            thread_ts="1700000000.000000",
            cancel=asyncio.Event(),
            author_id="U_AUTHOR",
            requester_is_admin=True,
            agent_name="Atlas",
            model_id="claude-sonnet-4-6",
            register=lambda ts, event, author_id: None,
            deregister=lambda ts: None,
            clock=lambda: 100.0,
            request_id=lambda: "01TESTREQUESTID0000000000",
            render_tables=render_tables,
            ask_human=True,
        )
        await lifecycle.post_initial()
        tool = ToolUseBlock(
            kind="tool_use", id="tu_1", type="agent.tool_use", name="bash", input={}
        )
        await lifecycle.on_render(TurnState(content=[tool]))
        usage = UsageTotals(
            input_tokens=1200,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=300,
            output_tokens=320,
        )
        await lifecycle.on_terminal_success(
            TurnState(content=[TextBlock(kind="text", text=answer)], usage_totals=usage)
        )
        calls: list[tuple[str, dict[str, Any]]] = [
            (url.name, call.kwargs["json"])  # pyright: ignore[reportUnknownMemberType]
            for (_, url), recorded in mock.requests.items()  # pyright: ignore[reportUnknownMemberType]
            for call in recorded
        ]
    return calls


# Every call a lifecycle run sends, grouped by method (aioresponses records requests per
# method and URL, so this is not the order they were sent). `_lifecycle_calls` fails when a
# run sends a different set of calls, so a new or dropped call cannot go unvalidated.
_LIFECYCLE_RUNS: dict[str, tuple[str, bool, tuple[str, ...]]] = {
    "answer": (_ANSWER, False, ("chat.postMessage", "chat.update")),
    "table_answer": (
        _ANSWER_WITH_TABLE,
        True,
        ("chat.postMessage", "chat.postMessage", "chat.update"),
    ),
}


@functools.cache
def _lifecycle_calls(run: str) -> tuple[tuple[str, dict[str, Any]], ...]:
    answer, render_tables, expected = _LIFECYCLE_RUNS[run]
    calls = tuple(run_async(_drive_lifecycle(answer, render_tables=render_tables)))
    sent = tuple(method for method, _ in calls)
    if sent != expected:
        raise AssertionError(f"lifecycle run {run!r} sent {sent}, catalogue expects {expected}")
    return calls


def _register_lifecycle_calls() -> None:
    for run, (_, _, methods) in _LIFECYCLE_RUNS.items():
        for index, method in enumerate(methods):
            name = f"lifecycle_{run}_{index + 1}_{method.removeprefix('chat.')}"
            entry(name, "slack", "blocks")(
                lambda run=run, index=index: copy.deepcopy(
                    _lifecycle_calls(run)[index][1]["blocks"]
                )
            )


_register_lifecycle_calls()
