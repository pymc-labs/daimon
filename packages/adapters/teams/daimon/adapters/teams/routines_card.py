"""Pure Adaptive Card builders for the `routines` panel and its create dialog. No I/O.

Panel buttons are `Action.Execute` carrying `{"action": VERB, "op": ..., "routine": id}`,
so a click can replace the card in place. New routine is an `Action.Submit` that
opens the create dialog, which Action.Execute cannot do.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal, cast

from daimon.adapters.teams.card_actions import button
from daimon.core.routines import PANEL_CAP, can_manage_routine, derive_glyph, routine_label
from daimon.core.stores.domain import RoutineRow
from microsoft_teams.api import (
    AdaptiveCardAttachment,
    CardTaskModuleTaskInfo,
    TaskModuleContinueResponse,
    TaskModuleMessageResponse,
    TaskModuleResponse,
    card_attachment,
)
from microsoft_teams.cards import (
    Action,
    ActionSet,
    ActionStyle,
    AdaptiveCard,
    CardElement,
    Choice,
    ChoiceSetInput,
    Container,
    ExecuteAction,
    OpenDialogData,
    SubmitAction,
    SubmitData,
    TextBlock,
    TextInput,
)

VERB = "routines"
CREATE_DIALOG = "routine_create"
FORM_FIELDS = {
    "agent": "Agent",
    "cron": "Cron expression",
    "timezone": "Timezone",
    "message": "Trigger message",
}
_EMPTY_HINT = "No routines yet. Ask your agent to schedule one, e.g. 'daily 9am stand-up summary'."

Op = Literal["refresh", "pause", "resume", "output", "delete", "confirm_delete"]


def _button(
    title: str, op: Op, row: RoutineRow | None = None, style: ActionStyle | None = None
) -> ExecuteAction:
    extra = {"routine": str(row.id)} if row else {}
    return button(VERB, title, op, style=style, **extra)


def _routine(row: RoutineRow, *, can_manage: bool) -> Container:
    items: list[CardElement] = [
        TextBlock(text=f"{derive_glyph(row)} {routine_label(row)}", weight="Bolder", wrap=True),
        TextBlock(
            text=f"{row.agent_name} · {row.cron_expr} ({row.timezone})",
            is_subtle=True,
            size="Small",
            wrap=True,
        ),
    ]
    if can_manage:
        toggle = _button("Pause", "pause", row) if row.enabled else _button("Resume", "resume", row)
        output = _button("Last output", "output", row)
        delete = _button("Delete", "delete", row, "destructive")
        items.append(ActionSet(actions=[toggle, output, delete]))
    return Container(items=items, separator=True)


def panel_card(
    rows: Sequence[RoutineRow],
    hidden: int,
    *,
    user_id: str,
    is_admin: bool,
    notice: str | None = None,
) -> AdaptiveCard:
    """Everyone sees every routine; buttons show only where the viewer may act."""
    body: list[CardElement] = [TextBlock(text="Routines", weight="Bolder", size="Medium")]
    if notice:
        body.append(TextBlock(text=notice, wrap=True))
    for row in rows:
        manage = can_manage_routine(row, user_id=user_id, is_admin=is_admin)
        body.append(_routine(row, can_manage=manage))
    if not rows:
        body.append(TextBlock(text=_EMPTY_HINT, wrap=True))
    if hidden:
        more = f"+{hidden} more routine(s) not shown (cap: {PANEL_CAP})"
        body.append(TextBlock(text=more, is_subtle=True, size="Small", wrap=True))
    actions: list[Action] = [_button("Refresh", "refresh")]
    if is_admin:
        actions.append(SubmitAction(title="New routine", data=OpenDialogData(CREATE_DIALOG)))
    body.append(ActionSet(actions=actions))
    return AdaptiveCard(body=body, fallback_text="Routines")


def output_card(row: RoutineRow) -> AdaptiveCard:
    """The last run's error, or its output tail. Both are bounded when written."""
    text = row.last_error if row.last_error is not None else row.last_result_tail or "(no output)"
    return AdaptiveCard(
        body=[
            TextBlock(text=f"Last output: {routine_label(row)}", weight="Bolder", wrap=True),
            TextBlock(text=text, font_type="Monospace", wrap=True),
            ActionSet(actions=[_button("Back", "refresh")]),
        ],
        fallback_text=text,
    )


def confirm_delete_card(row: RoutineRow) -> AdaptiveCard:
    delete = _button("Delete", "confirm_delete", row, "destructive")
    return AdaptiveCard(
        body=[
            TextBlock(text=f"Delete {routine_label(row)}?", weight="Bolder", wrap=True),
            TextBlock(text="This can't be undone.", wrap=True),
            ActionSet(actions=[delete, _button("Cancel", "refresh")]),
        ],
        fallback_text="Delete routine?",
    )


def create_form(
    agent_names: Sequence[str], values: Mapping[str, str], error: str | None = None
) -> AdaptiveCard:
    """The New routine form, refilled with `values` and topped by `error` on a retry."""
    body: list[CardElement] = []
    if error:
        body.append(TextBlock(text=error, color="Attention", wrap=True))
    body += [
        ChoiceSetInput(
            id="agent",
            label=FORM_FIELDS["agent"],
            is_required=True,
            value=values.get("agent"),
            choices=[Choice(title=name, value=name) for name in agent_names],
        ),
        TextInput(
            id="cron",
            label=FORM_FIELDS["cron"],
            placeholder="0 18 * * *",
            is_required=True,
            value=values.get("cron"),
        ),
        TextInput(
            id="timezone",
            label=FORM_FIELDS["timezone"],
            is_required=True,
            value=values.get("timezone") or "UTC",
        ),
        TextInput(
            id="message",
            label=FORM_FIELDS["message"],
            is_multiline=True,
            is_required=True,
            value=values.get("message"),
        ),
    ]
    submit = SubmitAction(title="Create", data=SubmitData(CREATE_DIALOG))
    return AdaptiveCard(body=body, actions=[submit], fallback_text="New routine")


def form_values(data: object) -> dict[str, str]:
    """The trimmed form fields of a dialog submit; a missing field is empty."""
    submitted: Mapping[str, object] = {}
    if isinstance(data, Mapping):
        submitted = cast(Mapping[str, object], data)
    return {field: str(submitted.get(field) or "").strip() for field in FORM_FIELDS}


def dialog(card: AdaptiveCard) -> TaskModuleResponse:
    attachment = card_attachment(AdaptiveCardAttachment(content=card))
    info = CardTaskModuleTaskInfo(title="New routine", card=attachment)
    return TaskModuleResponse(task=TaskModuleContinueResponse(value=info))


def dialog_message(text: str) -> TaskModuleResponse:
    return TaskModuleResponse(task=TaskModuleMessageResponse(value=text))
