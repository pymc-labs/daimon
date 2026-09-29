"""One reader for a setup-panel screen, whichever platform drew it.

The panel scenarios assert on what a person actually reads, so they need the
screen back as data after it has gone through a platform's own renderer:
Discord's components-v2 `LayoutView`, Slack's Block Kit modal or a Teams
Adaptive Card. The renderers draw the same three screens from the same core models, so one reader
per platform returning one shared `CapturedView` lets a scenario be written
once and run twice.

What the readers normalise away is presentation, never content:

* **Markup.** `**bold**`, `*bold*`, a fully-wrapped `_italic_` line, backticks
  and the mrkdwn entities (`&amp;` `&lt;` `&gt;`) are dropped.
* **Links.** Discord `[text](url)` and Slack `<url|text>` both become `text`.
* **References.** `<#C123>` / `<#123>` become `#C123` / `#123`, and `<@U1>` /
  `<@1>` become `@U1` / `@1`, so a scenario's `aliases` can then rewrite the
  one channel and the one user it created into platform-neutral names.
* **Times.** Discord renders `<t:…:R>` and Slack `<!date^…|…>` — the same
  instant, drawn by two clients in the reader's own locale. Neither is text
  anybody can compare, so both become the placeholder `{time}`.
* **Emoji.** Pictographs and Slack's `:shortcode:` form are stripped from
  line text and from every button label.
* **Shape.** A block's body is joined into one line: Discord writes
  `**Model** claude-…` on one line where Slack writes `*Model*\nclaude-…` on
  two, and the difference is Block Kit's, not the panel's.

What the readers keep is the order the blocks were emitted in, which line is
subtext and which is body, and the button labels. Where the two platforms
genuinely say different things — a different heading, a different fact under a
roster row, a different page size — that survives normalisation and the
scenarios pin it as a per-platform divergence rather than hiding it here.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

import discord

__all__ = [
    "CapturedView",
    "LineKind",
    "captured_titles",
    "read_discord_modal",
    "read_discord_view",
    "read_slack_view",
    "read_teams_view",
]

LineKind = Literal["heading", "row", "note", "field"]
"""What one rendered line is doing.

`heading` is the screen's own title — Discord's `## ` header, Slack's modal
title. `row` is a plain body line, including every agent row (the line wearing
the Details button). `field` is a labelled block: Slack's `fields` entries and
its bold-led sections, Discord's `**Label** …` text displays. `note` is
subtext: Discord's `-# ` lines, Slack's `context` elements.
"""

#: The accessory label that marks a roster row on both platforms, once emoji
#: are stripped. The row a reader clicks is the row they read, so "the block
#: carrying this button" is the one structural fact both renderers share.
_DETAILS_LABEL: Final = "Details"

#: Pager labels, recognised so the pager can be reported as `page` instead of
#: as two buttons whose position in the action list differs per platform.
_PAGER_LABELS: Final[frozenset[str]] = frozenset({"Previous", "Next"})

_PAGE_COUNTER = re.compile(r"^Page (\d+) of (\d+)$")

_DISCORD_TIMESTAMP = re.compile(r"<t:\d+(?::[tTdDfFR])?>")
_SLACK_DATE = re.compile(r"<!date\^\d+\^[^|>]*(?:\|[^>]*)?>")
_SLACK_LINK = re.compile(r"<(https?://[^|>]+)\|([^>]*)>")
_SLACK_BARE_LINK = re.compile(r"<(https?://[^|>]+)>")
_MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_CHANNEL_REF = re.compile(r"<#([^|>]+)(?:\|[^>]*)?>")
_USER_REF = re.compile(r"<@([^|>]+)(?:\|[^>]*)?>")
_SHORTCODE = re.compile(r":[a-z0-9_+-]+:")
_BOLD_DOUBLE = re.compile(r"\*\*([^*]+)\*\*")
_BOLD_SINGLE = re.compile(r"\*([^*]+)\*")
_WRAPPED_ITALIC = re.compile(r"^_(.+)_$")
_EMOJI = re.compile(
    "[\U0001f000-\U0001faff\u2190-\u21ff\u2300-\u23ff\u25a0-\u27bf\u2b00-\u2bff\ufe0f\u200d]"
)
_TIME_PLACEHOLDER: Final = "{time}"


def _strip_markup(text: str) -> str:
    """Drop every marker neither platform's reader is shown."""
    text = _BOLD_DOUBLE.sub(r"\1", text)
    text = _BOLD_SINGLE.sub(r"\1", text)
    text = text.replace("`", "")
    return _WRAPPED_ITALIC.sub(r"\1", text.strip())


def normalize_line(text: str, *, aliases: Mapping[str, str]) -> str:
    """One rendered line, with everything platform-specific taken out.

    `aliases` rewrites the ids a scenario created — its channel, its user —
    into names both platforms can be compared on, and is applied last so it
    sees the extracted `#id` / `@id` forms rather than the raw references.
    """
    text = _DISCORD_TIMESTAMP.sub(_TIME_PLACEHOLDER, text)
    text = _SLACK_DATE.sub(_TIME_PLACEHOLDER, text)
    # Unescaping comes before the references are read: Slack's mrkdwn escaper
    # leaves a channel whose id carries an underscore as `&lt;#C_x&gt;`, and a
    # reader that matched first would quote the entities at a person.
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = _SLACK_LINK.sub(r"\2", text)
    text = _SLACK_BARE_LINK.sub(r"\1", text)
    text = _MARKDOWN_LINK.sub(r"\1", text)
    text = _CHANNEL_REF.sub(r"#\1", text)
    text = _USER_REF.sub(r"@\1", text)
    text = _SHORTCODE.sub("", text)
    text = _EMOJI.sub("", text)
    text = _strip_markup(text)
    for raw, alias in aliases.items():
        text = text.replace(raw, alias)
    return re.sub(r"\s+", " ", text).strip()


@dataclass(frozen=True)
class CapturedView:
    """One panel screen, as the reader on either platform sees it."""

    title: str
    lines: tuple[tuple[LineKind, str], ...]
    action_labels: tuple[str, ...]
    page: int | None

    def texts(self, kind: LineKind) -> tuple[str, ...]:
        """Every line of one kind, in render order."""
        return tuple(text for line_kind, text in self.lines if line_kind == kind)

    @property
    def rows(self) -> tuple[str, ...]:
        return self.texts("row")

    @property
    def notes(self) -> tuple[str, ...]:
        return self.texts("note")

    @property
    def fields(self) -> tuple[str, ...]:
        return self.texts("field")

    @property
    def body(self) -> str:
        """Every line joined, for a plain "does the screen say this" check."""
        return "\n".join(text for _kind, text in self.lines)

    def says(self, needle: str) -> bool:
        """Whether any line carries `needle` (already normalised the same way)."""
        return needle in self.body

    def labelled(self, label: str) -> str | None:
        """The field or row whose first word(s) are `label`, or None."""
        for kind, text in self.lines:
            if kind in ("field", "row") and text.startswith(label):
                return text
        return None


class _Collector:
    """Accumulates the lines, buttons and page of one screen being read."""

    def __init__(self, aliases: Mapping[str, str]) -> None:
        self._aliases = aliases
        self.title = ""
        self.lines: list[tuple[LineKind, str]] = []
        self.actions: list[str] = []
        self.page: int | None = None

    def normalize(self, text: str) -> str:
        return normalize_line(text, aliases=self._aliases)

    def add_title(self, text: str) -> None:
        normalized = self.normalize(text)
        if not normalized:
            return
        self.title = normalized
        self.lines.insert(0, ("heading", normalized))

    def add_note(self, text: str) -> None:
        # The pager reads the same on both platforms but sits at the top of the
        # Discord card and the bottom of the Slack modal, so it is reported as
        # a number rather than as a line in a position.
        self.add_prepared_note(self.normalize(text))

    def add_body(self, text: str, *, kind: LineKind) -> None:
        self.add_prepared(self.normalize(text), kind=kind)

    def add_prepared(self, text: str, *, kind: LineKind) -> None:
        """Append an already-normalised line."""
        if text:
            self.lines.append((kind, text))

    def add_prepared_note(self, text: str) -> None:
        """Append an already-normalised subtext line, minding the pager."""
        if not text:
            return
        counter = _PAGE_COUNTER.match(text)
        if counter is not None:
            self.page = int(counter.group(1))
            return
        self.lines.append(("note", text))

    def add_button(self, label: str | None) -> None:
        normalized = self.normalize(label or "")
        if not normalized or normalized in _PAGER_LABELS:
            return
        self.actions.append(normalized)

    def finish(self) -> CapturedView:
        return CapturedView(
            title=self.title,
            lines=tuple(self.lines),
            action_labels=tuple(self.actions),
            page=self.page,
        )


def _body_kind(body_lines: list[str], *, is_row: bool) -> LineKind:
    """Whether a block reads as a labelled field or as plain body text."""
    if is_row:
        return "row"
    first = body_lines[0] if body_lines else ""
    return "field" if ("**" in first or "*" in first) else "row"


#: Discord writes a roster row's status inline after a middot where Slack
#: writes it in the context line under the row, so a row is split here and the
#: tail becomes subtext on both platforms.
_ROW_STATUS_SEPARATOR: Final = " · "


def _add_block(
    collector: _Collector, raw: str, *, is_row: bool = False, force_field: bool = False
) -> None:
    """Fold one text block into a body line plus however many notes it carried.

    The body's own line breaks are joined away: Discord writes a field's label
    and value on one line where Slack writes them on two, and neither shape is
    something a reader would describe differently.
    """
    body_lines: list[str] = []
    notes: list[str] = []
    for line in raw.split("\n"):
        if line.startswith("## "):
            collector.add_title(line.removeprefix("## "))
        elif line.startswith("-# "):
            notes.append(collector.normalize(line.removeprefix("-# ")))
        else:
            body_lines.append(line)
    kind: LineKind = "field" if force_field else _body_kind(body_lines, is_row=is_row)
    body = " ".join(normalized for line in body_lines if (normalized := collector.normalize(line)))
    if body and is_row:
        head, _, tail = body.partition(_ROW_STATUS_SEPARATOR)
        collector.add_prepared(head, kind=kind)
        if tail:
            notes.insert(0, tail)
    elif body:
        collector.add_prepared(body, kind=kind)
    for note in notes:
        collector.add_prepared_note(note)


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------


def _discord_texts(section: discord.ui.Section[Any]) -> str:
    return "\n".join(
        child.content for child in section.children if isinstance(child, discord.ui.TextDisplay)
    )


def _walk_discord(collector: _Collector, item: object) -> None:
    if isinstance(item, discord.ui.Container):
        for child in item.children:
            _walk_discord(collector, child)
        return
    if isinstance(item, discord.ui.Section):
        accessory = item.accessory
        label = (
            normalize_line(accessory.label or "", aliases={})
            if isinstance(accessory, discord.ui.Button)
            else ""
        )
        _add_block(collector, _discord_texts(item), is_row=label == _DETAILS_LABEL)
        if isinstance(accessory, discord.ui.Button):
            collector.add_button(accessory.label)
        return
    if isinstance(item, discord.ui.TextDisplay):
        _add_block(collector, item.content)
        return
    if isinstance(item, discord.ui.ActionRow):
        for child in item.children:
            if isinstance(child, discord.ui.Button):
                collector.add_button(child.label)
        return
    if isinstance(item, discord.ui.Button):
        collector.add_button(item.label)


def read_discord_view(
    view: discord.ui.LayoutView, *, aliases: Mapping[str, str] | None = None
) -> CapturedView:
    """Read one panel screen off a live `LayoutView`.

    The view is walked in render order — Container, then each Section,
    TextDisplay and ActionRow inside it — so the resulting lines are in the
    order the card puts them on screen.
    """
    collector = _Collector(aliases or {})
    for item in view.children:
        _walk_discord(collector, item)
    return collector.finish()


def read_discord_modal(
    modal: discord.ui.Modal, *, aliases: Mapping[str, str] | None = None
) -> CapturedView:
    """Read a Discord form as a screen: its title plus one field per input.

    Discord opens a form as a modal rather than as another card, so there is
    no `LayoutView` to walk. The field labels are what a reader sees, which is
    what makes this comparable at all with Slack's pushed form view.
    """
    collector = _Collector(aliases or {})
    collector.add_title(modal.title)
    for child in modal.children:
        text = getattr(child, "text", None)
        if isinstance(text, str):
            collector.add_body(text, kind="field")
    return collector.finish()


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------


def _slack_elements(block: Mapping[str, Any]) -> list[dict[str, Any]]:
    elements = block.get("elements")
    return [element for element in elements if isinstance(element, dict)] if elements else []


def _slack_button_label(element: Mapping[str, Any]) -> str | None:
    text = element.get("text")
    if isinstance(text, dict):
        label = text.get("text")
        if isinstance(label, str):
            return label
    return None


def _slack_accessory_label(block: Mapping[str, Any]) -> str | None:
    accessory = block.get("accessory")
    if isinstance(accessory, dict) and accessory.get("type") == "button":
        return _slack_button_label(accessory)
    return None


def _read_slack_block(collector: _Collector, block: Mapping[str, Any]) -> None:
    block_type = block.get("type")
    if block_type == "context":
        for element in _slack_elements(block):
            text = element.get("text")
            if isinstance(text, str):
                collector.add_note(text)
        return
    if block_type == "actions":
        for element in _slack_elements(block):
            collector.add_button(_slack_button_label(element))
        return
    if block_type == "section":
        accessory_label = _slack_accessory_label(block)
        is_row = (
            accessory_label is not None
            and normalize_line(accessory_label, aliases={}) == _DETAILS_LABEL
        )
        text = block.get("text")
        if isinstance(text, dict) and isinstance(text.get("text"), str):
            _add_block(collector, str(text["text"]), is_row=is_row)
        for field in block.get("fields") or []:
            if isinstance(field, dict) and isinstance(field.get("text"), str):
                _add_block(collector, str(field["text"]), force_field=True)
        if accessory_label is not None:
            collector.add_button(accessory_label)
        return
    if block_type == "input":
        label = block.get("label")
        if isinstance(label, dict) and isinstance(label.get("text"), str):
            collector.add_body(str(label["text"]), kind="field")
        return
    # `divider` and anything else carry no text a reader could quote.


def _slack_chrome_label(view: Mapping[str, Any], key: str) -> str | None:
    chrome = view.get(key)
    if isinstance(chrome, dict) and isinstance(chrome.get("text"), str):
        return str(chrome["text"])
    return None


def read_slack_view(
    view: Mapping[str, Any], *, aliases: Mapping[str, str] | None = None
) -> CapturedView:
    """Read one panel screen off the Block Kit modal Slack was sent.

    The modal's `close` and `submit` are appended to the action labels rather
    than kept as chrome: Slack draws Done as the modal's close button where
    Discord draws it as a button in the card, and they are the same action.
    """
    collector = _Collector(aliases or {})
    title = _slack_chrome_label(view, "title")
    collector.add_title(title or "")
    blocks = view.get("blocks") or []
    for block in blocks:
        if isinstance(block, dict):
            _read_slack_block(collector, block)
    for key in ("submit", "close"):
        collector.add_button(_slack_chrome_label(view, key))
    return collector.finish()


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------


def _teams_items(node: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    items: object = node.get(key)
    if not isinstance(items, list):
        return []
    return [cast(dict[str, Any], i) for i in cast(list[object], items) if isinstance(i, dict)]


def _read_teams_element(collector: _Collector, element: Mapping[str, Any]) -> None:
    kind = element.get("type")
    if kind == "TextBlock":
        text = str(element.get("text") or "")
        if element.get("size") == "Medium":
            collector.add_title(text)
        elif element.get("isSubtle"):
            collector.add_note(text)
        else:
            _add_block(collector, text)
    elif kind == "Container":
        # A roster row: the agent's name, its status as subtext, its Details button.
        items = _teams_items(element, "items")
        is_row = any(
            normalize_line(str(action.get("title") or ""), aliases={}) == _DETAILS_LABEL
            for item in items
            for action in _teams_items(item, "actions")
        )
        for item in items:
            if is_row and item.get("type") == "TextBlock" and not item.get("isSubtle"):
                _add_block(collector, str(item.get("text") or ""), is_row=True)
            else:
                _read_teams_element(collector, item)
    elif kind == "ActionSet":
        for action in _teams_items(element, "actions"):
            collector.add_button(action.get("title"))
    elif kind in ("Input.Text", "Input.ChoiceSet"):
        collector.add_body(str(element.get("label") or ""), kind="field")


def read_teams_view(
    card: Mapping[str, Any], *, aliases: Mapping[str, str] | None = None, title: str | None = None
) -> CapturedView:
    """Read one panel screen off the Adaptive Card Teams was sent.

    The Medium heading is the title (a dialog's card has none, so `title` names
    it), subtle TextBlocks are subtext, and every ActionSet and the card's own
    actions are buttons.
    """
    collector = _Collector(aliases or {})
    if title is not None:
        collector.add_title(title)
    for element in _teams_items(card, "body"):
        _read_teams_element(collector, element)
    for action in _teams_items(card, "actions"):
        collector.add_button(action.get("title"))
    return collector.finish()


def captured_titles(views: Iterable[CapturedView]) -> list[str]:
    """The titles of a run of captures, for asserting on a navigation path."""
    return [view.title for view in views]
