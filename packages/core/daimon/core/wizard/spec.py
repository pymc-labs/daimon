"""Agent-authored form schema and its edge validation.

`WizardSpec` is parsed from untrusted tool input: the whole form (prompt,
questions, option labels, image handles) is model-authored text arriving at
an MCP tool boundary. Pydantic validation happens here, at that boundary,
rather than deep inside a renderer.

Discord itself enforces hard ceilings — a message tops out at 40 nested
components, a button label at 80 characters, a select at 25 options — and
those are the ultimate source of truth. But letting an oversized form reach
the renderer means it fails as a bare `ValueError` from inside discord.py,
with no indication of which step or field caused it. `WizardSpec`'s model
validator is the proactive inner ceiling: it walks every step before any
component is built and raises an actionable `ValueError` that names the
offending step's index and key, and tells the author to split the form into
a shorter follow-up wizard.
"""

from __future__ import annotations

import re
from enum import StrEnum
from math import ceil
from typing import Final, cast

from daimon.core.wizard.state import build_custom_id, new_short_id
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

MAX_SELECT_OPTIONS: Final[int] = 25
MAX_TOTAL_COMPONENTS: Final[int] = 40
MAX_BUTTONS_PER_ROW: Final[int] = 5
MAX_LABEL_CHARS: Final[int] = 80
MAX_STEPS: Final[int] = 20
MAX_IMAGES: Final[int] = 10
# A step screen's head text is one TextDisplay carrying the prompt, a
# "Step N of M" line, and the question.
MAX_TEXT_DISPLAY_CHARS: Final[int] = 4000
# Room for the "\nStep NN of NN\n" line the renderer inserts between the
# prompt and the question.
HEAD_TEXT_OVERHEAD_CHARS: Final[int] = 32
# A multi step's question is rendered as the select's placeholder.
MAX_SELECT_PLACEHOLDER_CHARS: Final[int] = 150
MAX_SELECT_OPTION_DESCRIPTION_CHARS: Final[int] = 100
MAX_SELECT_OPTION_VALUE_CHARS: Final[int] = 100


class StepKind(StrEnum):
    """The three step shapes a wizard form can author."""

    CHOICE = "choice"
    MULTI = "multi"
    TEXT = "text"


class Option(BaseModel):
    """One selectable option on a `choice` or `multi` step."""

    model_config = ConfigDict(frozen=True)

    label: str = Field(description="Human-readable button/select-option text shown to the user.")
    value: str = Field(
        description="Stable machine value recorded in the answer when this option is chosen."
    )
    description: str | None = Field(
        default=None, description="Optional secondary text shown alongside the option label."
    )


_KIND_SYNONYMS: Final[dict[str, str]] = {
    "single_choice": "choice",
    "single": "choice",
    "radio": "choice",
    "multi_select": "multi",
    "multiselect": "multi",
    "checkbox": "multi",
    "free_text": "text",
    "freetext": "text",
    "input": "text",
}

_SLUG_RE: Final = re.compile(r"[^a-z0-9]+")


def _derive_key(question: str) -> str:
    """Slug a question into a step key. Only used when the author omits `key`."""
    slug = _SLUG_RE.sub("_", question.strip().lower()).strip("_")[:40].rstrip("_")
    return slug or "step"


class Step(BaseModel):
    """One screen of the wizard: a question plus how it is answered.

    Accepts the shape a model actually writes as well as the canonical one.
    Authored by an LLM at a tool boundary, and the canonical field names lose
    every coin-flip against the prior set by other form APIs: `type` for
    `kind`, `title`/`label` for `question`, plain strings for `options`, and
    `single_choice`/`multi_select`/`free_text` for the enum's
    `choice`/`multi`/`text`. A real session sent exactly that, collected 28
    validation errors, retried with the same shape, and abandoned the form.
    Rejecting it was defensible per-field and useless in aggregate, so the
    aliases and coercions below absorb the difference instead.
    """

    # This docstring is rendered into the post_wizard tool schema, so it ships
    # to the model on every request that carries the tool -- keep provenance in
    # comments like this one rather than adding it above.
    #
    # `label` joined the alias set after a later session lost only the
    # `question` coin-flip (`type` and `single_choice` were both absorbed) and
    # had all four of its steps rejected. It is the predictable miss:
    # `Option.label` is this schema's own word for user-facing text, so it is
    # the name a model reaches for when writing a step's text.

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    key: str = Field(
        default="",
        description=(
            "Stable identifier for this step's answer; must be unique across the form. "
            "Omit it and a slug of the question is used."
        ),
    )
    question: str = Field(
        validation_alias=AliasChoices("question", "title", "label"),
        description="The question text shown to the user on this step.",
    )
    kind: StepKind = Field(
        validation_alias=AliasChoices("kind", "type"),
        description="Whether this step is a single choice, a multi-select, or free text.",
    )
    options: list[Option] = Field(
        default_factory=list[Option],
        description=(
            "The selectable options for a `choice` or `multi` step. Unused for `text`. "
            "A plain string is accepted and becomes both the label and the value."
        ),
    )

    allow_custom: bool = Field(
        default=True,
        description="Whether the user may add a free-text option alongside the listed options.",
    )
    min: int | None = Field(
        default=None, description="Minimum number of selections required on a `multi` step."
    )
    max: int | None = Field(
        default=None, description="Maximum number of selections allowed on a `multi` step."
    )
    image_handle: str | None = Field(
        default=None,
        description=(
            "File-store handle id previously returned by the image-generation tool -- "
            "a finished PNG, never a URL and never a diagram source. Anything that "
            "produces an image does so upstream, before the handle is attached here."
        ),
    )
    image_url: str | None = Field(
        default=None,
        description="Server-populated; ignored when supplied by the caller.",
    )

    @field_validator("kind", mode="before")
    @classmethod
    def _accept_kind_synonyms(cls, value: object) -> object:
        """Map the vocabulary models reach for onto the canonical enum values."""
        if isinstance(value, str):
            return _KIND_SYNONYMS.get(value.strip().lower(), value)
        return value

    @field_validator("options", mode="before")
    @classmethod
    def _accept_bare_string_options(cls, value: object) -> object:
        """Coerce ``["A", "B"]`` into full ``Option`` objects.

        A list of strings is the obvious way to express choices, and carries
        every bit of information a two-field Option does when label and value
        are the same — which is the common case.
        """
        if isinstance(value, list):
            return [
                {"label": item, "value": item} if isinstance(item, str) else item
                for item in cast(list[object], value)
            ]
        return value

    @model_validator(mode="after")
    def _fill_key_from_question(self) -> Step:
        if not self.key:
            # frozen model: rebuild rather than mutate.
            return self.model_copy(update={"key": _derive_key(self.question)})
        return self


def _estimate_step_components(step: Step) -> int:
    """Conservative upper bound on the rendered component count for one step.

    Base 4 (container, head text, separator, pre-nav gap), +1 when the step
    carries an image, plus a per-kind body, plus the nav row and its buttons
    (back, next, and the custom opener when `allow_custom`).

    A `choice` option that carries a `description` renders heavier than a
    bare button: it needs its own text element alongside the button (a
    described option is a small section, not a single node), so it costs 2
    components instead of 1. Ignoring this would silently undercount any
    choice step that uses descriptions -- exactly the gap this ceiling
    exists to catch before the renderer does.
    """
    total = 4
    if step.image_handle is not None:
        total += 1
    if step.kind is StepKind.CHOICE:
        num_options = len(step.options)
        option_weight = sum(2 if option.description is not None else 1 for option in step.options)
        total += ceil(num_options / MAX_BUTTONS_PER_ROW) + option_weight
    elif step.kind is StepKind.MULTI:
        total += 2
    else:  # StepKind.TEXT
        total += 2
    nav_buttons = 2 + (1 if step.allow_custom else 0)  # back, next, [custom opener]
    total += 1 + nav_buttons  # nav row + its buttons
    return total


class WizardSpec(BaseModel):
    """The full agent-authored form: a prompt plus an ordered list of steps."""

    model_config = ConfigDict(frozen=True)

    prompt: str
    steps: list[Step]

    @model_validator(mode="after")
    def _validate_ceilings(self) -> WizardSpec:
        if not self.steps:
            raise ValueError("wizard spec declares zero steps; add at least one `steps` entry")
        if len(self.steps) > MAX_STEPS:
            raise ValueError(
                f"wizard spec declares {len(self.steps)} steps, exceeding the "
                f"{MAX_STEPS}-step ceiling; split the form into a shorter follow-up wizard"
            )

        seen_keys: set[str] = set()
        for index, step in enumerate(self.steps):
            if step.key in seen_keys:
                raise ValueError(
                    f"step {index} ({step.key!r}) reuses a key already used by an "
                    f"earlier step; give every step a unique `key`"
                )
            seen_keys.add(step.key)

        for index, step in enumerate(self.steps):
            if step.kind in (StepKind.CHOICE, StepKind.MULTI):
                if not step.options:
                    raise ValueError(
                        f"step {index} ({step.key!r}) is a {step.kind.value} step with "
                        f"no options; add at least one `options` entry"
                    )
                if len(step.options) > MAX_SELECT_OPTIONS:
                    raise ValueError(
                        f"step {index} ({step.key!r}) declares {len(step.options)} "
                        f"options, exceeding the {MAX_SELECT_OPTIONS}-option ceiling; "
                        f"split the form into a shorter follow-up wizard"
                    )
            for option in step.options:
                if len(option.label) > MAX_LABEL_CHARS:
                    raise ValueError(
                        f"step {index} ({step.key!r}) has an option label "
                        f"{len(option.label)} characters long, exceeding Discord's "
                        f"{MAX_LABEL_CHARS}-character button label limit; shorten it"
                    )
                if len(option.value) > MAX_SELECT_OPTION_VALUE_CHARS:
                    raise ValueError(
                        f"step {index} ({step.key!r}) has an option value "
                        f"{len(option.value)} characters long, exceeding Discord's "
                        f"{MAX_SELECT_OPTION_VALUE_CHARS}-character option value "
                        f"limit; shorten it"
                    )
                if (
                    option.description is not None
                    and len(option.description) > MAX_SELECT_OPTION_DESCRIPTION_CHARS
                ):
                    raise ValueError(
                        f"step {index} ({step.key!r}) has an option description "
                        f"{len(option.description)} characters long, exceeding "
                        f"Discord's {MAX_SELECT_OPTION_DESCRIPTION_CHARS}-character "
                        f"option description limit; shorten it"
                    )

        head_budget = MAX_TEXT_DISPLAY_CHARS - HEAD_TEXT_OVERHEAD_CHARS
        for index, step in enumerate(self.steps):
            head_chars = len(self.prompt) + len(step.question)
            if head_chars > head_budget:
                raise ValueError(
                    f"step {index} ({step.key!r}) renders a head text of {head_chars} "
                    f"characters (prompt plus question), exceeding the {head_budget}-"
                    f"character budget within Discord's {MAX_TEXT_DISPLAY_CHARS}-"
                    f"character text limit; shorten the prompt or the question"
                )
            if step.kind is StepKind.MULTI and len(step.question) > MAX_SELECT_PLACEHOLDER_CHARS:
                raise ValueError(
                    f"step {index} ({step.key!r}) is a multi step whose question is "
                    f"{len(step.question)} characters long; a multi step's question "
                    f"is rendered as the select placeholder, which Discord caps at "
                    f"{MAX_SELECT_PLACEHOLDER_CHARS} characters; shorten it"
                )

        for index, step in enumerate(self.steps):
            if step.kind is not StepKind.MULTI:
                continue
            if step.min is not None and step.min < 0:
                raise ValueError(
                    f"step {index} ({step.key!r}) has `min` {step.min}, which is "
                    f"negative; set `min` to 0 or higher"
                )
            if step.max is not None and step.max > len(step.options):
                raise ValueError(
                    f"step {index} ({step.key!r}) has `max` {step.max} above its "
                    f"{len(step.options)} available options; lower `max` or add more options"
                )
            # Checked independently of `max`: when `max` is None the renderer
            # derives max_values from the option count, so a `min` above that
            # count is a select no user could ever satisfy -- and one Discord
            # refuses to render at all.
            if step.min is not None and step.min > len(step.options):
                raise ValueError(
                    f"step {index} ({step.key!r}) has `min` {step.min} above its "
                    f"{len(step.options)} available options; lower `min` or add more options"
                )
            if step.min is not None and step.max is not None and step.min > step.max:
                raise ValueError(
                    f"step {index} ({step.key!r}) has `min` {step.min} greater than "
                    f"`max` {step.max}; `min` must not exceed `max`"
                )

        image_step_indices = [
            index for index, step in enumerate(self.steps) if step.image_handle is not None
        ]
        if len(image_step_indices) > MAX_IMAGES:
            raise ValueError(
                f"wizard spec attaches images to {len(image_step_indices)} steps, "
                f"exceeding the {MAX_IMAGES}-image ceiling; split the form into a "
                f"shorter follow-up wizard"
            )

        seen_handles: dict[str, int] = {}
        for index, step in enumerate(self.steps):
            if step.image_handle is None:
                continue
            earlier_index = seen_handles.get(step.image_handle)
            if earlier_index is not None:
                raise ValueError(
                    f"step {index} ({step.key!r}) shares image_handle "
                    f"{step.image_handle!r} with step {earlier_index}; each step's "
                    f"image_handle must be unique because the post path uploads one "
                    f"file per handle and maps the returned attachments back by position"
                )
            seen_handles[step.image_handle] = index

        for index, step in enumerate(self.steps):
            estimate = _estimate_step_components(step)
            if estimate > MAX_TOTAL_COMPONENTS:
                raise ValueError(
                    f"step {index} ({step.key!r}) is estimated to render {estimate} "
                    f"components, exceeding Discord's {MAX_TOTAL_COMPONENTS}-component "
                    f"ceiling; split the form into a shorter follow-up wizard"
                )

        for index, step in enumerate(self.steps):
            actions = [f"s{index}_c{n}" for n in range(len(step.options))]
            actions += [f"s{index}_sel", f"s{index}_enter", f"s{index}_custom"]
            actions += ["next", "back", "submit"]
            for action in actions:
                try:
                    build_custom_id(new_short_id(), action)
                except ValueError as exc:
                    raise ValueError(
                        f"step {index} ({step.key!r}) would emit an unroutable or "
                        f"oversized custom_id for action {action!r}: {exc}"
                    ) from exc

        return self
