"""Pure Adaptive Card builders for the `setup` panel and setup conversations. No I/O.

Panel buttons are `Action.Execute` carrying `{"action": VERB, "op": ..., ...}`, so a
click replaces the card in place. New agent and coding tools open dialogs: a dialog
is never written to chat history, which keeps a minted token out of it.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from typing import Any, Literal

from daimon.adapters.teams.card_actions import button, clip, heading
from daimon.adapters.teams.channel_settings_card import CHANNEL_DIALOG
from daimon.core.agent_detail_lists import (
    DETAIL_LIST_COLLAPSED_COUNT,
    DetailListName,
    format_detail_lists,
)
from daimon.core.agent_details import AgentDetails
from daimon.core.answering_map import AnsweringMap
from daimon.core.github_repo_auth import normalize_owner_repo
from daimon.core.models_catalog import ModelChoice
from daimon.core.panel_operator_tokens import PANEL_SCOPES, PANEL_TTL_DAYS, operator_token_line
from daimon.core.roster import Page, Roster, RosterAgent
from daimon.core.routing_facts import build_named_request, build_routing_request, precedence_line
from daimon.core.scope import AnsweringPlace
from daimon.core.setup_conversations import (
    EMPTY_ROSTER_COPY,
    setup_target_label,
    setup_thread_name,
    shared_keys_sentence,
)
from daimon.core.stores.domain import McpTokenRow
from microsoft_teams.cards import (
    Action,
    ActionSet,
    ActionStyle,
    AdaptiveCard,
    CardElement,
    Choice,
    ChoiceSetInput,
    CodeBlock,
    Container,
    ExecuteAction,
    OpenDialogData,
    SubmitAction,
    SubmitData,
    TextBlock,
    TextInput,
)

VERB = "agent_setup"
CREATE_DIALOG = "agent_create"
TOKEN_DIALOG = "agent_coding_tools"
OPERATOR_DIALOG = "operator_tokens"
PAGE_SIZE = 20
MAX_ENVIRONMENT_LINES = 10
"""Channel environment lines on Who answers where; the rest fold into "and N more"."""
ENDED = "Setup conversation ended. Your next message goes back to your usual agent."
_DETAIL_LIST_MAX_CHARS = 6_000
# Discord's bounds on the purpose and the places an agent answers in.
_PURPOSE_MAX_CHARS = 800
_ROUTING_MAX_CHARS = 1_000

_LIST_HEADINGS: dict[DetailListName, str] = {
    "skills": "Skills",
    "connections": "Connections",
    "keys": "Keys",
}

Op = Literal["agents", "details", "routing", "manage", "end"]


def _button(
    title: str, op: Op, style: ActionStyle | None = None, **fields: str | int
) -> ExecuteAction:
    return button(VERB, title, op, style=style, **fields)


def _text(text: str, *, bold: bool = False, subtle: bool = False) -> TextBlock:
    """A wrapped TextBlock. Teams renders a line break only as a blank line."""
    return TextBlock(
        text=re.sub(r"\n+", "\n\n", text),
        wrap=True,
        weight="Bolder" if bold else None,
        is_subtle=subtle or None,
        size="Small" if subtle else None,
    )


def _card(title: str, body: list[CardElement], actions: list[Action]) -> AdaptiveCard:
    tail: list[CardElement] = [ActionSet(actions=actions)] if actions else []
    return AdaptiveCard(body=[heading(title), *body, *tail], fallback_text=title)


def _manage(ma_agent_id: str | None, name: str | None) -> ExecuteAction:
    return _button(setup_target_label(name), "manage", "positive", agent=ma_agent_id or "")


def _pager(page: Page[Any], op: Op) -> list[Action]:
    actions: list[Action] = []
    if page.has_previous:
        actions.append(_button("◀ Previous", op, page=page.page - 1))
    if page.has_next:
        actions.append(_button("Next ▶", op, page=page.page + 1))
    return actions


def roster_card(
    roster: Roster, page: Page[RosterAgent], *, routed: Collection[str], notice: str | None = None
) -> AdaptiveCard:
    """The root screen: every agent, the one answering in this chat first."""
    body: list[CardElement] = [_text(notice)] if notice else []
    answering = roster.answering
    if not roster.rows:
        body.append(_text(EMPTY_ROSTER_COPY))
    for agent in page.items:
        if answering is not None and agent.name == answering.name:
            status = "Answers in this chat"
        else:
            status = "Answers in another channel" if agent.name in routed else "Not assigned"
        details = _button("🔍 Details", "details", agent=agent.name, page=page.page)
        row: list[CardElement] = [
            _text(agent.name, bold=True),
            _text(status, subtle=True),
            ActionSet(actions=[details]),
        ]
        body.append(Container(items=row, separator=True))
    if page.page_count > 1:
        body.append(_text(f"Page {page.page + 1} of {page.page_count}", subtle=True))
    actions: list[Action] = [
        _manage(
            answering.ma_agent_id if answering else None, answering.name if answering else None
        ),
        SubmitAction(title="➕ New agent", data=OpenDialogData(CREATE_DIALOG)),
        _button("📍 Who answers where", "routing"),
        *_pager(page, "agents"),
    ]
    return _card("Agents", body, actions)


def _place(place: AnsweringPlace, *, here: str) -> str:
    if place.tier == "channel" and place.channel_id is not None:
        return "this chat" if place.channel_id == here else f"channel `{place.channel_id}`"
    return "the organisation default" if place.tier == "tenant" else "the deployment default"


def _answers_in(places: Sequence[str]) -> str:
    """As many places as fit, then how many did not."""
    shown: list[str] = []
    used = 0
    for place in places:
        used += len(place) + 2
        if used > _ROUTING_MAX_CHARS:
            break
        shown.append(place)
    hidden = len(places) - len(shown)
    return ", ".join([*shown, f"+{hidden} more"] if hidden else shown)


def expanded_list(data: Mapping[str, object]) -> DetailListName | None:
    """The Details list a Show more click opens; None for Show fewer or an unknown name."""
    return next((k for k in _LIST_HEADINGS if data.get("expand") and data.get("list") == k), None)


def _detail_lists(
    details: AgentDetails, *, page: int, expanded: DetailListName | None
) -> list[CardElement]:
    items: Mapping[DetailListName, Sequence[str]] = {
        "skills": [skill.title or skill.skill_id for skill in details.skills],
        "connections": [f"[{server.name}]({server.url})" for server in details.mcp_servers],
        "keys": [key.name for key in details.keys],
    }
    lists = format_detail_lists(items, expanded=expanded, max_chars=_DETAIL_LIST_MAX_CHARS)
    body: list[CardElement] = []
    for kind, title in _LIST_HEADINGS.items():
        if items[kind]:
            body.append(_text(f"**{title}**\n\n{lists[kind]}"))
        if len(items[kind]) > DETAIL_LIST_COLLAPSED_COUNT:
            opened = expanded == kind
            toggle = _button(
                "Show fewer" if opened else "Show more",
                "details",
                agent=details.name,
                page=page,
                list=kind,
                expand=int(not opened),
            )
            body.append(ActionSet(actions=[toggle]))
    if details.skills_listing_truncated:
        body.append(_text("Some skill names may be missing.", subtle=True))
    if details.keys:
        body.append(_text(shared_keys_sentence(details.name), subtle=True))
    return body


def details_card(
    details: AgentDetails,
    *,
    here: str,
    page: int,
    coding_tools: bool,
    expanded: DetailListName | None = None,
) -> AdaptiveCard:
    """One agent's readable state. Key values are not in the model, so never here."""
    purpose = details.purpose
    body: list[CardElement] = [_text(clip(purpose, _PURPOSE_MAX_CHARS))] if purpose else []
    places = [_place(place, here=here) for place in details.answers_in]
    body.append(
        _text(f"**Answers in:** {_answers_in(places)}")
        if places
        else _text(details.unrouted_note or "Not assigned yet.")
    )
    body.append(ActionSet(actions=[_manage(details.ma_agent_id, details.name)]))
    body.append(_text(f"**Model:** {details.model_display_name}"))
    if details.repo is not None:
        access = details.repo.access
        state = "⚠️ needs attention" if access.kind == "needs_attention" else access.kind
        repo = normalize_owner_repo(details.repo.repo_url)
        body.append(_text(f"**Repository:** {repo} ({state.replace('_', ' ')})"))
        body.append(_text(f"**Branch:** `{details.repo.default_branch}`"))
    body += _detail_lists(details, page=page, expanded=expanded)
    actions: list[Action] = []
    if coding_tools:
        data = OpenDialogData(TOKEN_DIALOG, {"agent": details.name})
        actions.append(SubmitAction(title="🧰 Use from your coding tools", data=data))
    else:
        body.append(_text("Coding-tool access is not configured for this deployment.", subtle=True))
    actions.append(_button("Back", "agents", page=page))
    return _card(details.name, body, actions)


def routing_card(
    answering_map: AnsweringMap,
    page: Page[Any],
    *,
    is_admin: bool,
    request_agent: str | None,
    changes_channels: bool = False,
) -> AdaptiveCard:
    """The whole cascade, so the precedence is visible rather than inferred.

    `changes_channels` adds Channel settings, for a server admin or a channel's admin.
    """
    body: list[CardElement] = [
        _text(f"Channel `{answer.channel_id}`: **{answer.agent_name}**") for answer in page.items
    ] or [_text("No channel has its own setting.", subtle=True)]
    tenant = answering_map.tenant_default
    default = tenant.agent_name if tenant else "Not assigned"
    body.append(_text(f"**Organisation default:** {default}"))
    if answering_map.deployment_default is None:
        body.append(_text("No deployment default.", subtle=True))
    else:
        line = f"**Deployment default:** {answering_map.deployment_default}"
        if answering_map.tenant_consumes_fallthrough:
            line += " (not in effect while an organisation default is set)"
        body.append(_text(line))
    body.append(_text(_environments_text(answering_map)))
    names = [setup_thread_name(ref.target_name) for ref in answering_map.setup_threads]
    if answering_map.setup_threads_truncated:
        names.append("…and more")
    body.append(_text("**Setup conversations**\n\n" + ("\n\n".join(names) or "None open")))
    lead = "Tell Daimon: " if is_admin else "An admin can tell Daimon: "
    request = build_routing_request(
        agent_name=request_agent or "an agent", channel_label="a channel"
    )
    routing_hint = (
        build_named_request(agent_name=request_agent or "an agent", channel_label="a channel")
        if answering_map.channel_defaults == "own_channels_only"
        else f"{lead}{request}"
    )
    body.append(
        _text(f"{precedence_line(answering_map.channel_defaults)} {routing_hint}", subtle=True)
    )
    actions: list[Action] = [*_pager(page, "routing"), _button("Back", "agents")]
    if changes_channels:
        settings = OpenDialogData(CHANNEL_DIALOG)
        actions.append(SubmitAction(title="⚙ Channel settings", data=settings))
    if is_admin:
        tokens = SubmitAction(title="🔑 Operator tokens", data=OpenDialogData(OPERATOR_DIALOG))
        actions.append(tokens)
    return _card("Who answers where", body, actions)


def _environments_text(answering_map: AnsweringMap) -> str:
    """Each channel's own environment, then the defaults, as Discord and Slack list them."""
    rows = answering_map.channel_environments
    lines = [
        f"Channel `{row.channel_id}`: **{row.environment_name}**"
        for row in rows[:MAX_ENVIRONMENT_LINES]
    ]
    if len(rows) > len(lines):
        lines.append(f"…and {len(rows) - len(lines)} more")
    tenant, deployment = answering_map.tenant_environment, answering_map.deployment_environment
    lines = lines or ["No channel picks its own environment yet."]
    lines.append(f"**Organisation default:** {tenant or 'Not assigned'}")
    if deployment is not None:
        note = " (not in effect while an organisation default is set)" if tenant else ""
        lines.append(f"**Deployment default:** {deployment}{note}")
    return "**Environments**\n\n" + "\n\n".join(lines)


def new_agent_form(
    choices: Sequence[ModelChoice], values: Mapping[str, str], error: str | None = None
) -> AdaptiveCard:
    """Name, purpose and model, refilled with `values` and topped by `error` on a retry."""
    default = next((choice.id for choice in choices if choice.is_default), None)
    body: list[CardElement] = [_text(error)] if error else []
    body += [
        TextInput(
            id="name",
            label="Agent name",
            placeholder="e.g. my-data-analyst",
            is_required=True,
            value=values.get("name"),
        ),
        TextInput(
            id="purpose",
            label="What should it help with?",
            is_multiline=True,
            value=values.get("purpose"),
        ),
        ChoiceSetInput(
            id="model",
            label="Model",
            is_required=True,
            value=values.get("model") or default,
            choices=[Choice(title=choice.label, value=choice.id) for choice in choices],
        ),
    ]
    submit = SubmitAction(title="Create", data=SubmitData(CREATE_DIALOG))
    return AdaptiveCard(body=body, actions=[submit], fallback_text="New agent")


UNBOUND = "none"
"""The channel choice of a token bound to no channel (server admins only)."""


def token_channel_form(
    *, agent_name: str, channel_ids: Sequence[str], allow_unbound: bool
) -> AdaptiveCard:
    """Pick the channel a coding-tool token runs in: one the caller administers.

    Panels live in the 1:1 chat, so the channel the token binds to is chosen
    here rather than read from where the button was pressed.
    """
    choices = [Choice(title="Not bound to a channel", value=UNBOUND)] if allow_unbound else []
    choices += [
        Choice(title=f"Channel {channel_id}", value=channel_id) for channel_id in channel_ids
    ]
    submit = SubmitData(TOKEN_DIALOG, {"agent": agent_name, "op": "mint"})
    return AdaptiveCard(
        body=[
            _text(f"Where should **{agent_name}** run from your coding tools?"),
            ChoiceSetInput(
                id="channel",
                label="Channel",
                is_required=True,
                value=choices[0].value,
                choices=choices,
            ),
            _text("A bound token runs under that channel's rules and budget.", subtle=True),
        ],
        actions=[SubmitAction(title="Mint token", data=submit)],
        fallback_text=f"Use {agent_name} from your coding tools",
    )


def token_card(
    *, agent_name: str, cli: str, mcp_json: str, jti: str, channel_id: str | None = None
) -> AdaptiveCard:
    """The minted token, shown once inside a dialog, with a Revoke button."""
    revoke = SubmitAction(title="🗑 Revoke this token", data=SubmitData(TOKEN_DIALOG, {"jti": jti}))
    bound = (
        f" It runs in channel `{channel_id}`, under that channel's rules and budget."
        if channel_id is not None
        else ""
    )
    return AdaptiveCard(
        body=[
            _text(
                f"Use **{agent_name}** from your coding tools. Token shown once, copy it now."
                + bound
            ),
            _text("**Run this:**"),
            CodeBlock(code_snippet=cli, language="Bash"),
            _text("**Or paste into `.mcp.json`:**"),
            CodeBlock(code_snippet=mcp_json, language="Json"),
        ],
        actions=[revoke],
        fallback_text=f"Use {agent_name} from your coding tools",
    )


def operator_tokens_card(rows: Sequence[McpTokenRow], *, notice: str | None = None) -> AdaptiveCard:
    """The tenant's live operator tokens, a Mint form and a Revoke pick. Never a token."""
    body: list[CardElement] = [_text(notice)] if notice else []
    lines = [f"`{operator_token_line(row)}`" for row in rows]
    body.append(_text("\n".join(lines) or "No live operator tokens.", subtle=not lines))
    body += [
        ChoiceSetInput(
            id="scopes",
            label="Scopes",
            is_multi_select=True,
            choices=[Choice(title=scope, value=scope) for scope in PANEL_SCOPES],
        ),
        TextInput(id="label", label="Label", placeholder="what it is for", max_length=100),
        _text(
            f"An operator token lets an integration call daimon's tenant tools as you, for "
            f"{PANEL_TTL_DAYS} days. It is shown once.",
            subtle=True,
        ),
    ]
    actions: list[Action] = [
        SubmitAction(title="Mint token", data=SubmitData(OPERATOR_DIALOG, {"op": "mint"}))
    ]
    if rows:
        body.append(
            ChoiceSetInput(
                id="jti",
                label="Revoke",
                choices=[Choice(title=operator_token_line(r), value=str(r.jti)) for r in rows],
            )
        )
        revoke = SubmitData(OPERATOR_DIALOG, {"op": "revoke"})
        actions.append(SubmitAction(title="🗑 Revoke", data=revoke))
    return AdaptiveCard(body=body, actions=actions, fallback_text="Operator tokens")


def operator_token_card(*, token: str, scopes: Sequence[str], expires: str) -> AdaptiveCard:
    """A minted operator token, shown once inside the dialog."""
    return AdaptiveCard(
        body=[
            _text(f"Scopes: {', '.join(scopes)}. Expires {expires}. Shown once, copy it now."),
            CodeBlock(code_snippet=token, language="PlainText"),
        ],
        fallback_text="Operator token",
    )


def welcome_card(*, target_name: str | None, opener: str, thread_id: str) -> AdaptiveCard:
    """Opens a setup conversation in the chat. End stops it; so does `new`."""
    how = "Reply here to talk to Daimon. Send `new` or press End when you are done."
    end = _button("End setup", "end", "destructive", thread=thread_id)
    return _card(setup_thread_name(target_name), [_text(opener), _text(how, subtle=True)], [end])


def notice_card(text: str) -> AdaptiveCard:
    return AdaptiveCard(body=[_text(text)], fallback_text=text)
