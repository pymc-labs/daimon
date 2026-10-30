"""Pure status-card state and Teams message builders. No I/O.

The status card says what Discord's and Slack's say, in the words of
`daimon.core.turn.status_lines`: a Thinking or Working headline with the
elapsed time, then in `CARD_SECTIONS` order the last few draft lines, the
agent's plan when progress plans are on and the turn's tool lines, and a Stop
button.
The answer replaces the card as Extended Markdown messages: a card TextBlock
renders no code blocks or math, a message does. A card follows it with the
summary line, 👍/👎 buttons (`vote_actions`, answered by `feedback`) and
Ask a person when support is on: Teams refuses an edit carrying both text
and a card. The votes sit beside Ask a person, as on Slack and Discord,
not in Teams' feedback loop. A tool-only or failed turn's notice card
carries them itself. Output previews ride in the answer's text as markdown
images, in plain markdown: Teams refuses an edit with text and an image
attachment too, and splits such a new post into two messages.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Sequence
from dataclasses import dataclass

from daimon.core.platform_words import scrub_other_platforms
from daimon.core.support_escalation import ASK_THE_TEAM, ESCALATE
from daimon.core.teams_markdown import escape_dollars
from daimon.core.turn.notices import TerminationNotice, fit_notice
from daimon.core.turn.progress_plans import progress_plans_shown
from daimon.core.turn.state import TurnState
from daimon.core.turn.status_lines import (
    SECTION_LABELS,
    CardSection,
    LabelledSection,
    draft_log,
    format_draft,
    format_duration,
    format_headline,
    format_plan_lines,
    format_tool_lines,
    has_running_tool,
    in_card_order,
    roll_draft,
)
from microsoft_teams.api import Account, Attachment, MentionEntity, MessageActivityInput
from microsoft_teams.cards import (
    Action,
    ActionSet,
    AdaptiveCard,
    CardElement,
    Container,
    ExecuteAction,
    OpenDialogData,
    SubmitAction,
    TextBlock,
)

# A Teams message is capped by payload size (about 28 KB), not characters;
# 4 000 stays under it at 4 UTF-8 bytes each, the widest (an emoji).
TEAMS_LIMIT = 4_000
CANCEL_VERB = "cancel_turn"
INTERRUPTED_NOTICE = (
    "Daimon restarted before this request finished.\n"
    "@mention Daimon with your request to try again."
)
CANCELLED_NOTICE = "Stopped.\nSend a message to start again."
STOPPING_HEADLINE = "Stopping…"
TOOLS_DONE_NOTICE = "Done."
# A group chat closed mid-turn: no word of the turn's work.
ASK_HUMAN = f"{ESCALATE} {ASK_THE_TEAM}"
ASK_HUMAN_DIALOG = "ask_human"
VOTE_UP_DIALOG = "feedback_up"
VOTE_DOWN_DIALOG = "feedback_down"
_FALLBACK_MAX_CHARS = 100


@dataclass(frozen=True, slots=True)
class CardState:
    """What the live status card shows. `on_*` return new instances."""

    started_at: float
    is_working: bool = False
    tool_lines: tuple[str, ...] = ()
    plan_lines: tuple[str, ...] = ()
    """The agent's latest progress plan as a checklist; empty unless progress plans are on."""
    draft: str | None = None
    """The latest draft line."""
    earlier_drafts: tuple[str, ...] = ()
    """The draft lines before ``draft`` that the card still shows, oldest first."""
    # Stop was clicked: the turn is winding down, so the card offers no Stop.
    stopping: bool = False


def on_stop(state: CardState) -> CardState:
    """Stop was clicked: every later render shows the stopping card."""
    return dataclasses.replace(state, stopping=True)


def on_message(state: CardState, text: str) -> CardState:
    """A whole agent message joins the draft log; an empty one changes nothing."""
    if not text:
        return state
    line = format_draft(scrub_other_platforms(text, "teams"))
    earlier, latest = roll_draft(state.earlier_drafts, state.draft, line)
    return dataclasses.replace(state, earlier_drafts=earlier, draft=latest)


def on_draft(state: CardState, text: str) -> CardState:
    """A provider's preview of the message in progress revises the latest draft line."""
    if not text:
        return state
    return dataclasses.replace(state, draft=format_draft(scrub_other_platforms(text, "teams")))


def on_activity(state: CardState, turn: TurnState) -> CardState:
    """Fold the turn's tool calls into the card: Working while one runs, else Thinking.

    With progress plans on for the turn, the latest plan update becomes the
    checklist and leaves the tool lines.
    """
    plan_shown = progress_plans_shown()
    lines = format_tool_lines(
        turn.content, finished_ids=turn.finished_tool_ids, plan_shown=plan_shown
    )
    plan = format_plan_lines(turn.content) if plan_shown else ()
    return dataclasses.replace(
        state,
        is_working=has_running_tool(turn.content),
        tool_lines=tuple(scrub_other_platforms(line, "teams") for line in lines),
        plan_lines=tuple(scrub_other_platforms(line, "teams") for line in plan),
    )


def _card(body: list[CardElement], *, fallback: str) -> MessageActivityInput:
    return MessageActivityInput().add_card(AdaptiveCard(body=body, fallback_text=fallback))


def status_card(state: CardState, *, now: float, cancel_key: str) -> MessageActivityInput:
    """The live card as a message. See `live_card`."""
    return MessageActivityInput().add_card(live_card(state, now=now, cancel_key=cancel_key))


def live_card(state: CardState, *, now: float, cancel_key: str) -> AdaptiveCard:
    """The live card. `cancel_key` routes a Cancel click to this turn.

    Once Stop is clicked it reads "Stopping…" and has no Stop button; the
    answer to the click is this card too (`handle_cancel`).
    A TextBlock renders no code fence and drops single line breaks, so the
    plan and the tool lines are monospace blocks with one paragraph each.
    """
    elapsed = now - state.started_at
    if state.stopping:
        headline = f"**{STOPPING_HEADLINE}**"
        fallback = STOPPING_HEADLINE
    else:
        headline = format_headline(
            is_working=state.is_working, elapsed_seconds=elapsed, bold=lambda word: f"**{word}**"
        )
        fallback = format_headline(
            is_working=state.is_working, elapsed_seconds=elapsed, bold=lambda word: word
        )
    body: list[CardElement] = [TextBlock(text=headline, wrap=True)]
    # A TextBlock drops single line breaks: one paragraph per line in each section.
    # The plan and the tool lines are drawn alike, monospace so their emoji look alike.
    sections: dict[CardSection, list[CardElement]] = {}
    if draft_lines := draft_log(state.earlier_drafts, state.draft):
        sections["updates"] = [TextBlock(text="\n\n".join(draft_lines), is_subtle=True, wrap=True)]
    labelled: dict[LabelledSection, tuple[str, ...]] = {
        "plan": state.plan_lines,
        "tools": state.tool_lines,
    }
    for name, lines in labelled.items():
        if lines:
            sections[name] = [
                TextBlock(text=SECTION_LABELS[name], size="Small", wrap=True),
                TextBlock(text="\n\n".join(lines), font_type="Monospace", size="Small", wrap=True),
            ]
    body.extend(element for section in in_card_order(sections) for element in section)
    body.append(TextBlock(text=format_duration(elapsed), is_subtle=True, size="Small", wrap=True))
    if not state.stopping:
        body.append(
            ActionSet(
                actions=[
                    ExecuteAction(
                        title="Stop",
                        verb=CANCEL_VERB,
                        data={"action": CANCEL_VERB, "turn": cancel_key},
                        style="destructive",
                    )
                ]
            )
        )
    return AdaptiveCard(body=body, fallback_text=fallback)


def termination_text(notice: TerminationNotice) -> str:
    """The notice as the ❌ card's text, under the Teams limit, request id kept.

    One paragraph per line: a card TextBlock drops single line breaks.
    """
    lines = [notice.title, notice.next_step]
    if (work := notice.work_line()) is not None:
        lines.append(work)
    return fit_notice(lines, tail=notice.ref_line, limit=TEAMS_LIMIT)


def _summary_line(summary: str) -> TextBlock:
    return TextBlock(text=summary, is_subtle=True, size="Small", wrap=True)


def notice_card(
    text: str,
    *,
    actions: Sequence[Action] = (),
    summary: str | None = None,
    error: bool = True,
) -> MessageActivityInput:
    """A terminal card: bailouts, failures, restarts; `actions` are its buttons, if any.

    `summary` is a finished turn's line (`format_summary`), quiet under the text.
    An error sits in a red "attention" container, so it reads as Daimon's own
    notice; `error=False` is for a plain "Done." pointer.
    Clipped as Slack clips its notices; the fallback repeats it, so it gets less.
    """
    body = fit_notice([text], tail=None, limit=TEAMS_LIMIT)
    fallback_parts = text.rsplit("\n\n", 1)
    fallback = (
        fit_notice([fallback_parts[0]], tail=fallback_parts[1], limit=_FALLBACK_MAX_CHARS)
        if len(fallback_parts) == 2 and fallback_parts[1].startswith("Ref ")
        else fit_notice([text], tail=None, limit=_FALLBACK_MAX_CHARS)
    )
    parts = body.split("\n\n")
    small: list[str] = []
    while parts and (
        parts[-1].startswith("Ref ")
        or parts[-1].startswith("Still running when it ended:")
        or parts[-1].startswith("Finished before that:")
    ):
        small.insert(0, parts.pop())
    elements: list[CardElement] = [TextBlock(text="\n\n".join(parts), wrap=True)]
    elements.extend(_summary_line(line) for line in small)
    if summary:
        elements.append(_summary_line(summary))
    if actions:
        elements.append(ActionSet(actions=list(actions)))
    if error:
        elements = [Container(style="attention", items=elements)]
    return _card(elements, fallback=fallback)


ANSWERED_BELOW = "Done."


def ask_human_action() -> SubmitAction:
    """Opens Ask a human's dialog; an Action.Submit, since Action.Execute opens none."""
    return SubmitAction(title=ASK_HUMAN, data=OpenDialogData(ASK_HUMAN_DIALOG))


def vote_actions() -> list[Action]:
    """Emoji-only 👍/👎: a click opens daimon's dialog, so a 👎 gets the reasons form."""
    return [
        SubmitAction(title="\N{THUMBS UP SIGN}", data=OpenDialogData(VOTE_UP_DIALOG)),
        SubmitAction(title="\N{THUMBS DOWN SIGN}", data=OpenDialogData(VOTE_DOWN_DIALOG)),
    ]


def controls_card(summary: str, *, ask_human: bool) -> MessageActivityInput:
    """Posted just below the answer: its summary line, 👍/👎 and Ask a human.

    A click names this message (`replyToId`), so a vote or request links to
    it, the answer's next message, and a request is made once per answer.
    """
    actions = [*vote_actions(), *([ask_human_action()] if ask_human else [])]
    return _card([_summary_line(summary), ActionSet(actions=actions)], fallback=summary)


# Alt text that could end the image's brackets or start markup.
_ALT_SPECIAL = re.compile(r"([\\\[\]*_`<>$])")


def inline_image(preview: Attachment) -> str:
    """A data-URI preview as a markdown image, which Teams hosts in the text's message.

    Only data URIs: a Graph thumbnail's https URL is kept as sent but expires.
    """
    alt = _ALT_SPECIAL.sub(r"\\\1", " ".join((preview.name or "image").split()))
    return f"![{alt}]({preview.content_url})"


def answer_message(
    text: str, *, mention: Account | None = None, images: Sequence[Attachment] = ()
) -> MessageActivityInput:
    """One chunk of the answer, text only, in Extended Markdown (math, tables, code).

    A completion ping leads with `mention` (an AAD object id is enough) and
    stays plain markdown: whether Extended Markdown keeps an `<at>` mention is
    undocumented. `images`, data-URI previews, follow the text as markdown
    images, outside `TEAMS_LIMIT`, and make it plain markdown too: Teams then
    stores each as hosted content Graph reads back, where Extended Markdown
    keeps a URL no daimon token can fetch. The caller keeps math out of such a
    message (`has_math`), since plain markdown shows no math.
    """
    tail = "".join(f"\n\n{inline_image(image)}" for image in images)
    if mention is None and not images:
        return MessageActivityInput(
            text=escape_dollars(text), text_format="extendedmarkdown"
        ).add_ai_generated()
    message = MessageActivityInput(text=text + tail, text_format="markdown").add_ai_generated()
    if mention is None:
        return message
    tag = f"<at>{(mention.name or 'you').replace('<', '').replace('>', '')}</at>"
    message.text = f"{tag}\n\n{text}{tail}"
    message.add_entity(MentionEntity(mentioned=mention, text=tag))
    return message
