"""Every Adaptive Card the Teams adapter and the MCP server send is one Teams renders.

Teams draws cards up to v1.5 and refuses a message over about 28 KB. Each builder gets its
largest accepted inputs, 4-byte emoji wherever text is free (httpx sends raw UTF-8), and ten
times any limit where an input is unbounded, so the builder must bound it itself.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, get_args

import httpx
import pytest
from daimon.adapters.teams import card, memory, privacy_card, routines_card, setup_card
from daimon.adapters.teams.billing_panel import checkout_card
from daimon.adapters.teams.billing_panel import panel_card as billing_card
from daimon.adapters.teams.credential_requests import credential_form, oauthdialog
from daimon.adapters.teams.help import COMMAND_HELP, help_card
from daimon.adapters.teams.privacy_panel import NAME_MISMATCH
from daimon.adapters.teams.setup_panel import GONE
from daimon.adapters.teams.tool_confirmation import confirmation_adaptive_card
from daimon.core.agent_detail_lists import DetailListName
from daimon.core.agent_details import (
    AgentDetails,
    KeyEntry,
    McpServerEntry,
    RepoBinding,
    SkillEntry,
)
from daimon.core.answering_map import AnsweringMap, ChannelAnswer, SetupThreadRef, TenantAnswer
from daimon.core.billing_panel import BillingPanelState, MemberRow
from daimon.core.confirmation import prompt_for_tool_call
from daimon.core.continuity.messages import ConfigurationChange
from daimon.core.github_repo_auth import RepoAccess
from daimon.core.headless_runner import LAST_RESULT_TAIL_MAX
from daimon.core.ma import SessionDeletionReport
from daimon.core.mcp_auth import coding_tool_config
from daimon.core.message_split import split_fenced
from daimon.core.models_catalog import ModelChoice
from daimon.core.posted_controls import CardState
from daimon.core.posted_controls.confirmation import ConfirmationCardState, build_confirmation_card
from daimon.core.posted_controls.teams_card import (
    ADAPTIVE_CARD_TYPE,
    build_adaptive_card,
    card_for_request,
)
from daimon.core.privacy import PurgePreview, PurgePreviewRow
from daimon.core.purge import AccountPurgeResult, PurgeReport
from daimon.core.roster import Roster, RosterAgent, paginate
from daimon.core.routines import PANEL_CAP
from daimon.core.scope import AnsweringPlace
from daimon.core.setup_conversations import build_setup_opener
from daimon.core.stores.domain import CredentialRequestRow, RoutineRow
from daimon.core.tool_safety import ToolCall
from daimon.core.turn.notices import TerminationNotice
from daimon.core.turn.termination import TerminationReason
from jsonschema import Draft6Validator
from jsonschema.exceptions import best_match
from microsoft_teams.api import (
    Account,
    Attachment,
    ConversationAccount,
    MessageActivityInput,
    TaskModuleResponse,
)
from microsoft_teams.cards import AdaptiveCard

# Vendored for an offline test from
# https://raw.githubusercontent.com/microsoft/AdaptiveCards/main/schemas/1.5.0/adaptive-card.json
SCHEMA = Path(__file__).parent / "data/adaptive-card-v1.5.schema.json"
# Teams' own element, outside the Adaptive Cards schema:
# https://learn.microsoft.com/microsoftteams/platform/task-modules-and-cards/cards/cards-format#codeblock-in-adaptive-cards
CODE_BLOCK = {
    "type": "object",
    "properties": {
        "type": {"enum": ["CodeBlock"]},
        "codeSnippet": {"type": "string"},
        "language": {"type": "string"},
        "startLineNumber": {"type": "number"},
    },
    "required": ["type", "codeSnippet"],
    "additionalProperties": False,
}
MAX_BYTES = 26_000  # a margin under Teams' 28 KB
EMOJI = "😀"
NAME = "a" * 64  # agent names: `[A-Za-z0-9_-]{1,64}`
KEY = "K" * 64
CHANNEL = f"19:{'c' * 32}@thread.tacv2;messageid={'1' * 13}"
URL = f"https://example.com/{'u' * 2028}"
NOW = datetime(2026, 1, 1, tzinfo=UTC)
MessageSource = MessageActivityInput | AdaptiveCard | Mapping[str, object]


def _validator() -> Draft6Validator:
    schema = json.loads(SCHEMA.read_text())
    schema["definitions"]["ImplementationsOf.Element"]["anyOf"].append(CODE_BLOCK)
    return Draft6Validator(schema)


def _as_sent(source: MessageSource) -> dict[str, Any]:
    """The activity as the SDK posts it; the MCP client's envelope is smaller."""
    if isinstance(source, AdaptiveCard):
        message = MessageActivityInput().add_card(source)
    elif isinstance(source, MessageActivityInput):
        message = source
    else:
        attachment = Attachment(content_type=ADAPTIVE_CARD_TYPE, content=source)
        message = MessageActivityInput().add_attachments(attachment)
    message.from_ = Account(id=f"28:{uuid.UUID(int=1)}", name=NAME)
    message.conversation = ConversationAccount(
        id=CHANNEL, tenant_id=str(uuid.UUID(int=2)), conversation_type="channel"
    )
    return message.model_dump(by_alias=True, exclude_none=True)


def _turn_state() -> card.CardState:
    state = card.CardState(agent_name=NAME, started_at=0.0)
    for index in range(10):
        state = card.on_tool(state, f"{index}{'t' * 127}")
    return card.on_message(state, EMOJI * 10_000)


def _footer() -> str:
    return card.footer_text(
        _turn_state(), now=99 * 3600.0, tokens_in=10**9, tokens_out=10**9, cost="$123456.78"
    )


def _answer() -> MessageActivityInput:
    chunks = split_fenced(f"```python\n{EMOJI * 10 * card.TEAMS_LIMIT}", card.TEAMS_LIMIT)
    return card.answer_message(max(chunks, key=len), footer=_footer())


def _termination() -> MessageActivityInput:
    notice = TerminationNotice(
        reason=TerminationReason.UPSTREAM,
        headline=EMOJI * 10_000,
        cause=EMOJI * 10_000,
        survived=EMOJI * 10_000,
        next_step=EMOJI * 10_000,
        in_flight=tuple(f"{index}{'t' * 127}" for index in range(20)),
        finished_tools=10**6,
        request_id=f"req_{'r' * 60}",
    )
    return card.notice_card(card.termination_text(notice, footer=_footer()))


def _roster() -> AdaptiveCard:
    rows = tuple(
        RosterAgent(
            name=f"{index:02d}{NAME[2:]}",
            ma_agent_id=f"agent_{index:026d}",
            model_id="claude-opus-4-6",
            is_built_in=False,
        )
        for index in range(3 * setup_card.PAGE_SIZE)
    )
    page = paginate(rows, page=1, page_size=setup_card.PAGE_SIZE)
    roster = Roster(rows=rows, answering=rows[setup_card.PAGE_SIZE])
    return setup_card.roster_card(roster, page, routed={r.name for r in rows}, notice=GONE)


def _details(expanded: DetailListName) -> AdaptiveCard:
    details = AgentDetails(
        ma_agent_id=f"agent_{'0' * 26}",
        name=NAME,
        purpose=EMOJI * 10_000,
        model_id="claude-opus-4-6",
        model_display_name=NAME,
        daimon_managed=False,
        created_by_is_workspace=True,
        created_at=NOW,
        answers_in=tuple(
            AnsweringPlace(tier="channel", channel_id=f"{CHANNEL}{index}") for index in range(500)
        ),
        answers_here=False,
        repo=RepoBinding(
            repo_url=f"https://github.com/{'o' * 39}/{'r' * 100}",
            default_branch="b" * 255,
            access=RepoAccess(kind="needs_attention", credential="none", corrective=EMOJI * 1000),
        ),
        skills=tuple(
            SkillEntry(type="custom", skill_id=f"skill_{index}", title=EMOJI * 64, version="1")
            for index in range(61)
        ),
        skills_listing_truncated=True,
        mcp_servers=tuple(
            McpServerEntry(name=EMOJI * 64, url=f"https://example.com/{index}/{EMOJI * 64}")
            for index in range(61)
        ),
        keys=tuple(KeyEntry(name=f"{KEY}{index}", updated_at=NOW) for index in range(61)),
        applies_note=f"Changes to {NAME} apply from the next message to it.",
    )
    return setup_card.details_card(
        details, here=CHANNEL, page=10**6, coding_tools=True, expanded=expanded
    )


def _routing() -> AdaptiveCard:
    overrides = tuple(
        ChannelAnswer(channel_id=f"{CHANNEL}{index}", agent_name=NAME) for index in range(100)
    )
    answering_map = AnsweringMap(
        channel_overrides=overrides,
        tenant_default=TenantAnswer(agent_name=NAME),
        deployment_default=NAME,
        tenant_consumes_fallthrough=True,
        setup_threads=tuple(
            SetupThreadRef(
                thread_id=f"{CHANNEL};setup={index}",
                parent_channel_id=CHANNEL,
                target_name=EMOJI * 1000,
                updated_at=NOW,
            )
            for index in range(10)
        ),
        setup_threads_truncated=True,
    )
    page = paginate(overrides, page=1, page_size=setup_card.PAGE_SIZE)
    return setup_card.routing_card(answering_map, page, is_admin=False, request_agent=NAME)


def _models() -> list[ModelChoice]:
    return [
        ModelChoice(id=f"claude-{index}", label=EMOJI * 64, description=None, is_default=False)
        for index in range(20)
    ]


def _token() -> AdaptiveCard:
    cli, mcp_json = coding_tool_config(agent_name=NAME, public_url=URL, jwt="e" * 1000)
    return setup_card.token_card(agent_name=NAME, cli=cli, mcp_json=mcp_json, jti=str(NOW))


def _welcome() -> AdaptiveCard:
    opener = build_setup_opener(
        target_display=NAME, bot_mention=None, is_admin=False, admin_noun="an admin"
    )
    return setup_card.welcome_card(target_name=NAME, opener=opener, thread_id=f"{CHANNEL};setup=1")


def _routine(index: int = 0) -> RoutineRow:
    return RoutineRow(
        id=uuid.UUID(int=index),
        tenant_id=uuid.UUID(int=0),
        created_by_user_id=None,
        agent_id=f"agent_{index}",
        agent_name=NAME,
        cron_expr="0,1,2,3,4,5,6,7,8,9 0,1,2,3,4,5,6,7,8,9 1,2,3,4,5,6,7,8,9 * *",
        timezone="America/Argentina/ComodRivadavia",
        trigger_message=EMOJI * 10_000,
        enabled=True,
        next_fire_at=None,
        last_fired_at=NOW,
        last_error=None,
        last_result_tail=EMOJI * LAST_RESULT_TAIL_MAX,
        created_at=NOW,
        updated_at=NOW,
    )


def _preview() -> PurgePreview:
    row = PurgePreviewRow(count=10**6, example=EMOJI * 100)
    return PurgePreview(**dict.fromkeys(PurgePreview.model_fields, row))


def _purged() -> AccountPurgeResult:
    counts = dict.fromkeys(PurgeReport.model_fields, 10**6)
    sessions = SessionDeletionReport(deleted=10**6, failed=10**6, upstream_error=True)
    return AccountPurgeResult(db=PurgeReport(**counts), sessions=sessions)


def _billing(*, is_admin: bool) -> AdaptiveCard:
    rows = tuple(
        MemberRow(
            platform_user_id=str(uuid.UUID(int=index)),
            display_name=EMOJI * 100,
            cost_usd=10.0**6,
            turn_count=10**6,
            is_caller=index == 0,
        )
        for index in range(25)
    )
    state = BillingPanelState(
        is_admin=is_admin,
        caller_user_id=str(uuid.UUID(int=0)),
        caller_spend=10.0**6,
        caller_turns=10**6,
        caller_cap=Decimal(1),
        guild_balance_usd=Decimal(-(10**6)),
        guild_spend=10.0**6,
        guild_turns=10**6,
        guild_distinct_members=10**6,
        member_rows=rows,
        over_cap_count=10**6,
    )
    return billing_card(state, since=NOW)


def _request(kind: str) -> CredentialRequestRow:
    return CredentialRequestRow(
        token="t" * 43,
        kind=kind,
        tenant_id=uuid.UUID(int=0),
        agent_id=uuid.UUID(int=1),
        account_id=uuid.UUID(int=2),
        target=f"{URL}@{'b' * 255}#{'p' * 255}" if kind in ("repo", "skill_repo") else KEY,
        mcp_server_url=URL,
        requester_platform_user_id=str(uuid.UUID(int=3)),
        channel_id=CHANNEL,
        idempotency_key=uuid.UUID(int=4),
        target_name=NAME,
        responder_name=NAME,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=30),
        used_at=None,
    )


def _posted(kind: str, state: CardState) -> Mapping[str, object]:
    change = ConfigurationChange(
        target_name=NAME,
        kind="key",
        availability="next_message",
        detail=KEY,
        repo=URL,
        branch="b" * 255,
    )
    posted = card_for_request(
        _request(kind),
        state=state,
        outcome=change if state in ("applied", "partial") else None,
        refusal="token_rejected" if state == "refused" else None,
    )
    return build_adaptive_card(posted, token="t" * 43 if state == "requested" else None)


def _confirmation(state: ConfirmationCardState) -> AdaptiveCard:
    call = ToolCall(
        tool_use_id="toolu_1",
        server_name=EMOJI * 64,
        tool_name="t" * 128,
        input={"body": EMOJI * 10_000},
    )
    prompt = prompt_for_tool_call(call, requester_platform_user_id=str(uuid.UUID(int=0)), now=NOW)
    confirmation = build_confirmation_card(
        prompt, state=state, token="t" * 64 if state == "pending" else None
    )
    return confirmation_adaptive_card(confirmation, prompt, answered_by=EMOJI * 256)


MESSAGES: dict[str, Callable[[], MessageSource]] = {
    "status": lambda: card.status_card(_turn_state(), now=99 * 3600.0, cancel_key="k" * 64),
    "answer": _answer,
    "termination_notice": _termination,
    "raw_error_notice": lambda: card.notice_card(f"❌ {EMOJI * 10_000} · {_footer()}"),
    "interrupted_notice": lambda: card.notice_card(card.INTERRUPTED_NOTICE),
    "roster": _roster,
    "details_skills": lambda: _details("skills"),
    "details_connections": lambda: _details("connections"),
    "details_keys": lambda: _details("keys"),
    "routing": _routing,
    "welcome": _welcome,
    "setup_notice": lambda: setup_card.notice_card(setup_card.ENDED),
    "routines": lambda: routines_card.panel_card(
        [_routine(index) for index in range(PANEL_CAP)],
        10**6,
        user_id="u",
        is_admin=True,
        notice=f"✅ Created routine on {NAME} ({_routine().cron_expr}).",
    ),
    "routine_output": lambda: routines_card.output_card(_routine()),
    "routine_error": lambda: routines_card.output_card(
        _routine().model_copy(update={"last_error": "e" * 500})
    ),
    "routine_delete": lambda: routines_card.confirm_delete_card(_routine()),
    "privacy_none": lambda: privacy_card.no_data_card(NAME),
    "privacy": lambda: privacy_card.panel_card(_preview(), bot=NAME, policy_url=URL),
    "privacy_export": lambda: privacy_card.export_card(_preview(), bot=NAME),
    "privacy_confirm": lambda: privacy_card.confirm_card(
        _preview(), account_id=uuid.UUID(int=0), name=EMOJI * 256, error=NAME_MISMATCH
    ),
    "privacy_deleted": lambda: privacy_card.post_delete_card(_purged(), bot=NAME),
    "billing_member": lambda: _billing(is_admin=False),
    "billing_admin": lambda: _billing(is_admin=True),
    "billing_checkout": lambda: checkout_card(URL, 100),
    "memory": lambda: memory._card(f"/memories/{EMOJI * 100}.md", EMOJI * 100_000),  # pyright: ignore[reportPrivateUsage]  # what show_memory sends
    "help": lambda: help_card(COMMAND_HELP, bot=NAME),
    **{
        f"posted_{kind}_{state}": lambda kind=kind, state=state: _posted(kind, state)
        for kind in ("env", "mcp", "mcp_oauth", "repo", "skill_repo")
        for state in get_args(CardState)
    },
    **{
        f"confirmation_{state}": lambda state=state: _confirmation(state)
        for state in get_args(ConfirmationCardState)
    },
}
# Dialogs are invoke responses, never posted, so the message cap does not apply.
DIALOGS: dict[str, Callable[[], AdaptiveCard | TaskModuleResponse]] = {
    "new_agent_form": lambda: setup_card.new_agent_form(
        _models(), {"name": NAME, "purpose": EMOJI * 1000, "model": "claude-0"}, EMOJI * 1000
    ),
    "token": _token,
    "routine_form": lambda: routines_card.create_form(
        [f"{index:03d}{NAME[3:]}" for index in range(100)],
        {"agent": NAME, "cron": "c" * 100, "timezone": "t" * 100, "message": EMOJI * 1000},
        EMOJI * 1000,
    ),
    "credential_form": lambda: credential_form(_request("env"), EMOJI * 100),
    "credential_oauth": lambda: oauthdialog(_request("mcp_oauth"), URL),
}


def _assert_renders(content: dict[str, Any]) -> None:
    error = best_match(_validator().iter_errors(content))
    assert error is None, f"{list(error.absolute_path)}: {error.message[:300]}"
    version = tuple(int(part) for part in content["version"].split("."))
    assert version <= (1, 5), f"Teams renders up to 1.5, not {content['version']}"


@pytest.mark.parametrize("case", MESSAGES)
def test_message_fits_teams_when_every_input_is_at_its_worst(case: str) -> None:
    activity = _as_sent(MESSAGES[case]())
    for attachment in activity.get("attachments", []):
        _assert_renders(attachment["content"])
    size = len(httpx.Request("POST", "https://smba.example", json=activity).content)
    assert size < MAX_BYTES, f"the message is {size:,} bytes; keep it under {MAX_BYTES:,}"


@pytest.mark.parametrize("case", DIALOGS)
def test_dialog_renders_in_teams_when_every_input_is_at_its_worst(case: str) -> None:
    source = DIALOGS[case]()
    dumped = source.model_dump(by_alias=True, exclude_none=True)
    _assert_renders(
        dumped if isinstance(source, AdaptiveCard) else dumped["task"]["value"]["card"]["content"]
    )
