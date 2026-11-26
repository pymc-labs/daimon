"""Ask a person: credits, limits, menus, pricing, precedence, copy. Pure.

A limit on a workspace, an agent or a channel gives `shared` credits, an
`each_person` cap, or both, all time or monthly, between optional dates. The
most specific limit governs and limits never stack; a person's own limit only
replaces their cap. With no limit, the default ledger is `credits_per_user`
per person over all time. Menus are chosen per place, apart from limits.

Credits are `Decimal` with at most two places. JSON and YAML carry them as
decimal strings ("0.5"); a float is refused so no value is ever rounded on
the way in. They never touch the USD ledger.

Prices bound the charge, and nothing is charged until staff accept a request:
`fixed` and `steps` hold their price until accepted, `range` and `quoted` hold
their minimum until settled. A menu may name the channel its requests go to.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Annotated, Final, Literal, Protocol, cast

import yaml
from daimon.core.errors import DaimonError
from daimon.core.support_escalation import ALREADY_REQUESTED, OUT_OF_CREDITS, UNAVAILABLE
from daimon.core.windows import WindowError, WindowPeriod, WindowState, parse_instant
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    StringConstraints,
    ValidationError,
    model_validator,
)

if TYPE_CHECKING:
    from daimon.core.stores.support_escalation import SubmitOutcome
    from daimon.core.stores.support_rows import LimitView, SupportEscalationRow

# Limits: the lowest common denominator of Discord, Slack and Teams.
MAX_OPTIONS: Final = 10  # Slack radio_buttons
MAX_OPTION_LABEL: Final = 75  # Slack option text
MAX_OPTION_DESCRIPTION: Final = 75
MAX_FIELDS: Final = 4  # a Discord modal's 5 components, less the option line
MAX_FIELD_LABEL: Final = 45  # Discord Label
MAX_PLACEHOLDER: Final = 100
MAX_TEXT_CHARS: Final = 200
MAX_PARAGRAPH_CHARS: Final = 3000  # Slack plain_text_input
MIN_CHOICES: Final = 2
MAX_CHOICES: Final = 10
MIN_STEPS: Final = 2
MAX_STEPS: Final = 10
MAX_MENU_NAME: Final = 75
MAX_AMOUNT: Final = Decimal(1000)
MAX_LIMIT: Final = Decimal(100000)
MIN_CHARGE: Final = Decimal("0.01")
_CENT: Final = Decimal("0.01")
MENU_KEY_PATTERN: Final = r"[a-z0-9-]{1,32}"
OPTION_KEY_PATTERN: Final = r"[a-z0-9_]{1,32}"

SupportScope = Literal["workspace", "agent", "channel"]
SUPPORT_SCOPES: Final[tuple[SupportScope, ...]] = ("channel", "agent", "workspace")
"""Most specific first: the order `governing_limit` tries."""
ChargedTo = Literal["default", "limit", "exempt"]
SupportWindow = Literal["total", "monthly"]
SUPPORT_WINDOWS: Final[tuple[SupportWindow, ...]] = ("total", "monthly")
RequestStatus = Literal["pending", "settled", "declined"]


class SupportError(DaimonError):
    """A limit, menu, amount or answer that cannot be used; the message says what to fix.

    `field` names the input at fault (`shared`, `ends_at`, ...) for a form to point at.
    """

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


# ---------- Credits ----------


def _decimal(value: object, *, name: str) -> Decimal:
    if isinstance(value, bool | float):
        raise SupportError(f'{name} must be a decimal string such as "0.5"', field=name)
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, str):
        try:
            amount = Decimal(value.strip())
        except (InvalidOperation, ValueError) as exc:
            raise SupportError(
                f"{name} must be a number of credits, got {value!r}", field=name
            ) from exc
    else:
        raise SupportError(f'{name} must be a decimal string such as "0.5"', field=name)
    if not amount.is_finite():
        raise SupportError(f"{name} must be a number of credits", field=name)
    # "1.500" is 1.5: only a nonzero digit past the second place is refused.
    if cast(int, amount.normalize().as_tuple().exponent) < -2:
        raise SupportError(f"{name} must have at most two decimal places", field=name)
    return amount.quantize(_CENT) if cast(int, amount.as_tuple().exponent) < -2 else amount


def parse_credits(value: object, *, name: str = "credits") -> Decimal:
    """A limit or allowance: 0 to 100000, at most two decimal places."""
    amount = _decimal(value, name=name)
    if amount < 0 or amount > MAX_LIMIT:
        raise SupportError(f"{name} must be from 0 to {format_credits(MAX_LIMIT)}", field=name)
    return amount


def parse_amount(value: object, *, name: str = "amount", allow_zero: bool = False) -> Decimal:
    """A price: above 0 (or 0 with `allow_zero`) and at most 1000."""
    amount = _decimal(value, name=name)
    if amount > MAX_AMOUNT or amount < 0 or (amount == 0 and not allow_zero):
        low = "0" if allow_zero else "more than 0"
        raise SupportError(f"{name} must be {low} and at most {format_credits(MAX_AMOUNT)}")
    return amount


def format_credits(value: Decimal) -> str:
    """`1`, `0.5`, `1.25`: no trailing zeros, never an exponent."""
    return f"{value.normalize():f}"


# Pydantic reports only a ValueError's message, so the parsers are wrapped.
def _credits_in(value: object) -> Decimal:
    try:
        return _decimal(value, name="credits")
    except SupportError as exc:
        raise ValueError(str(exc)) from exc


def _amount_check(value: Decimal) -> Decimal:
    try:
        return parse_amount(value)
    except SupportError as exc:
        raise ValueError(str(exc)) from exc


def _min_check(value: Decimal) -> Decimal:
    try:
        return parse_amount(value, name="min", allow_zero=True)
    except SupportError as exc:
        raise ValueError(str(exc)) from exc


_CreditsText = PlainSerializer(format_credits, return_type=str)
Amount = Annotated[
    Decimal, BeforeValidator(_credits_in), AfterValidator(_amount_check), _CreditsText
]
"""A price amount in a menu: a decimal string, above 0, at most 1000."""
MinAmount = Annotated[
    Decimal, BeforeValidator(_credits_in), AfterValidator(_min_check), _CreditsText
]

# ---------- Menu spec ----------

_Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
OptionKey = Annotated[str, StringConstraints(pattern=f"^{OPTION_KEY_PATTERN}$")]
OptionLabel = Annotated[_Text, StringConstraints(max_length=MAX_OPTION_LABEL)]
FieldLabel = Annotated[_Text, StringConstraints(max_length=MAX_FIELD_LABEL)]
Placeholder = Annotated[_Text, StringConstraints(max_length=MAX_PLACEHOLDER)]
Description = Annotated[_Text, StringConstraints(max_length=MAX_OPTION_DESCRIPTION)]


class _Spec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FixedPrice(_Spec):
    kind: Literal["fixed"]
    amount: Amount


class Step(_Spec):
    label: OptionLabel
    amount: Amount


class StepsPrice(_Spec):
    """The requester picks one step; `label` is the question, e.g. "Length"."""

    kind: Literal["steps"]
    label: FieldLabel
    steps: tuple[Step, ...] = Field(min_length=MIN_STEPS, max_length=MAX_STEPS)

    @model_validator(mode="after")
    def _distinct(self) -> StepsPrice:
        if len({s.label for s in self.steps}) != len(self.steps):
            raise ValueError("step labels must be different")
        return self


class RangePrice(_Spec):
    """Staff settle anywhere from `min` to `max`."""

    kind: Literal["range"]
    min: Amount
    max: Amount

    @model_validator(mode="after")
    def _ordered(self) -> RangePrice:
        if self.min >= self.max:
            raise ValueError("a range needs min below max")
        return self


class QuotedPrice(_Spec):
    """Staff set the charge, from `min` up to `max` or, with no max, the balance left."""

    kind: Literal["quoted"]
    min: MinAmount = Decimal(0)
    max: Amount | None = None

    @model_validator(mode="after")
    def _ordered(self) -> QuotedPrice:
        if self.max is not None and self.max < self.min:
            raise ValueError("a quoted max must be at least its min")
        return self


Price = Annotated[FixedPrice | StepsPrice | RangePrice | QuotedPrice, Field(discriminator="kind")]


class _FieldBase(_Spec):
    key: OptionKey
    label: FieldLabel
    placeholder: Placeholder | None = None
    required: bool = True


class TextField(_FieldBase):
    kind: Literal["text"]
    max_chars: int = Field(default=MAX_TEXT_CHARS, ge=1, le=MAX_TEXT_CHARS)


class ParagraphField(_FieldBase):
    kind: Literal["paragraph"]
    max_chars: int = Field(default=MAX_PARAGRAPH_CHARS, ge=1, le=MAX_PARAGRAPH_CHARS)


class ChoiceField(_FieldBase):
    kind: Literal["choice"]
    choices: tuple[OptionLabel, ...] = Field(min_length=MIN_CHOICES, max_length=MAX_CHOICES)

    @model_validator(mode="after")
    def _distinct(self) -> ChoiceField:
        if len(set(self.choices)) != len(self.choices):
            raise ValueError("choices must be different")
        return self


OptionField = Annotated[TextField | ParagraphField | ChoiceField, Field(discriminator="kind")]


class SupportOption(_Spec):
    """One thing a person can ask for, its price and up to four fields."""

    key: OptionKey
    label: OptionLabel
    description: Description | None = None
    price: Price
    fields: tuple[OptionField, ...] = ()

    @model_validator(mode="after")
    def _fits(self) -> SupportOption:
        used = len(self.fields) + (1 if isinstance(self.price, StepsPrice) else 0)
        if used > MAX_FIELDS:
            raise ValueError(f"an option takes at most {MAX_FIELDS} fields (a steps price is one)")
        if len({f.key for f in self.fields}) != len(self.fields):
            raise ValueError("field keys must be different")
        return self


class MenuChannel(_Spec):
    """Where a menu's requests go: a channel of the workspace; Teams adds its team and URL."""

    id: _Text
    team_id: _Text | None = None
    service_url: _Text | None = None


class MenuSpec(_Spec):
    """A tenant's menu for Ask a person, as stored in `support_menus`."""

    name: Annotated[_Text, StringConstraints(max_length=MAX_MENU_NAME)]
    channel: MenuChannel | None = None
    options: tuple[SupportOption, ...] = Field(min_length=1, max_length=MAX_OPTIONS)

    @model_validator(mode="after")
    def _distinct(self) -> MenuSpec:
        if len({o.key for o in self.options}) != len(self.options):
            raise ValueError("option keys must be different")
        return self

    def option(self, key: str) -> SupportOption | None:
        return next((o for o in self.options if o.key == key), None)


_ENTRIES: Final = {"options": "option", "fields": "field", "steps": "step"}
_PRICE_KINDS: Final = frozenset({"fixed", "steps", "range", "quoted"})
_FIELD_KINDS: Final = frozenset({"text", "paragraph", "choice"})


_NO_TAGS: Final = frozenset[str]()


def _child(node: object, part: int | str) -> object:
    if isinstance(part, int):
        if isinstance(node, list | tuple) and part < len(cast("Sequence[object]", node)):
            return cast("Sequence[object]", node)[part]
        return None
    return cast("Mapping[str, object]", node).get(part) if isinstance(node, Mapping) else None


def _entry(kind: str, item: object, index: int) -> str:
    """`option "review"`, by key, else label, else position (1-based)."""
    for name in ("key", "label"):
        value = _child(item, name)
        if isinstance(value, str) and value.strip():
            return f'{kind} "{value.strip()}"'
    return f"{kind} {index + 1}"


def _where(loc: tuple[int | str, ...], data: object) -> str:
    """A pydantic path read against the input: `option "review", field "details", max_chars`."""
    names: list[str] = []
    node, entry, tags = data, "", _NO_TAGS
    for part in loc:
        if isinstance(part, str) and (part.startswith("function-") or part in tags):
            tags = _NO_TAGS  # a union's tag, e.g. the "range" in price.range.min
            continue
        tags = _NO_TAGS
        node = _child(node, part)
        if isinstance(part, int) and entry:
            names.append(_entry(entry, node, part))
            tags = _FIELD_KINDS if entry == "field" else _NO_TAGS
        elif part not in _ENTRIES:
            names.append(str(part))
            tags = _PRICE_KINDS if part == "price" else _NO_TAGS
        entry = _ENTRIES.get(part, "") if isinstance(part, str) else ""
    if entry:  # the list itself, e.g. too few options
        names.append(str(loc[-1]))
    return ", ".join(names)


def _readable(exc: ValidationError, data: object) -> str:
    errors = exc.errors()
    # A tuple whose item failed also reports itself too short; the item is the news.
    own = [e for e in errors if e["type"] != "too_short"] or errors
    problems: list[str] = []
    for error in own[:3]:
        loc = tuple(error["loc"])
        where = _where(loc, data)
        message = str(error["msg"]).removeprefix("Value error, ")
        if loc[-2:] == ("text", "max_chars") and error["type"] == "less_than_equal":
            message = f"a text field takes at most {MAX_TEXT_CHARS}; use a paragraph field"
        problems.append(f"{where}: {message}" if where else message)
    return "; ".join(problems)


def parse_menu(data: object) -> MenuSpec:
    """Validate a menu from a file, a tool or the web; the error names the bad entry."""
    try:
        return MenuSpec.model_validate(data)
    except ValidationError as exc:
        raise SupportError(f"Menu not valid: {_readable(exc, data)}") from exc


def load_menu(text: str) -> MenuSpec:
    """`parse_menu` over YAML or JSON text (JSON is YAML)."""
    try:
        data: object = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SupportError(f"Menu not valid: not YAML or JSON{_yaml_where(exc)}") from exc
    return parse_menu(data)


def _yaml_where(exc: yaml.YAMLError) -> str:
    """` at line 3, column 7: expected ',' or ']'`, from the parser's mark (1-based)."""
    if not isinstance(exc, yaml.MarkedYAMLError) or exc.problem_mark is None:
        return ""
    mark = exc.problem_mark
    problem = f": {exc.problem}" if exc.problem else ""
    return f" at line {mark.line + 1}, column {mark.column + 1}{problem}"


def parse_menu_key(key: str) -> str:
    key = key.strip()
    if not re.fullmatch(MENU_KEY_PATTERN, key):
        raise SupportError("A menu key is 1-32 lowercase letters, digits or dashes.")
    return key


DEFAULT_OPTION_KEY: Final = "request"
DEFAULT_FIELD_LABEL: Final = "What do you need help with?"
DEFAULT_MENU: Final = MenuSpec(
    name="Ask a person",
    options=(
        SupportOption(
            key=DEFAULT_OPTION_KEY,
            label="Ask a person",
            price=FixedPrice(kind="fixed", amount=Decimal(1)),
            fields=(ParagraphField(key="note", kind="paragraph", label=DEFAULT_FIELD_LABEL),),
        ),
    ),
)
"""Implicit, never stored: today's one request at 1 credit."""

# ---------- Pricing ----------


@dataclass(frozen=True)
class PriceBounds:
    """What a request may finally cost; `upper` None is "up to the balance left"."""

    lower: Decimal
    upper: Decimal | None


def price_bounds(price: Price, *, step: int | None = None) -> PriceBounds:
    """`step` indexes a steps price and is refused on any other."""
    if isinstance(price, StepsPrice):
        if step is None or not 0 <= step < len(price.steps):
            raise SupportError(f"Pick a {price.label.lower()}.")
        amount = price.steps[step].amount
        return PriceBounds(amount, amount)
    if step is not None:
        raise SupportError("This option has no steps.")
    if isinstance(price, FixedPrice):
        return PriceBounds(price.amount, price.amount)
    return PriceBounds(price.min, price.max)


def cheapest_bounds(price: Price) -> PriceBounds:
    """The least an option can hold, before a step is picked: a steps price's smallest step."""
    if isinstance(price, StepsPrice):
        low = min(step.amount for step in price.steps)
        return PriceBounds(low, low)
    return price_bounds(price)


def has_set_charge(price: Price) -> bool:
    """Fixed and steps prices are known at submit, so Accept charges them as they are."""
    return isinstance(price, FixedPrice | StepsPrice)


def hold_for(bounds: PriceBounds) -> Decimal:
    """A request holds its minimum until settled (decision 3)."""
    return bounds.lower


def may_submit(bounds: PriceBounds, available: Decimal | None) -> bool:
    """Some credit must be left even for a quoted request with min 0; None is unbounded."""
    return available is None or available >= max(bounds.lower, MIN_CHARGE)


SettleProblem = Literal["invalid", "over_balance"]


def settle_problem(
    bounds: PriceBounds,
    *,
    charge: Decimal,
    counted: Decimal,
    available: Decimal | None,
    correction: bool = False,
) -> SettleProblem | None:
    """Why `charge` cannot settle a request now counting `counted`, or None.

    A `correction` (an admin re-settling) may go down to 0, for refunds.
    `available` is the balance left with this request's `counted` already spent.
    """
    lower = Decimal(0) if correction else bounds.lower
    if charge < lower or (bounds.upper is not None and charge > bounds.upper):
        return "invalid"
    if available is not None and charge - counted > available:
        return "over_balance"
    return None


class OptionSnapshot(_Spec):
    """The terms a request was made on, stored with it; settling never reads the live menu."""

    key: str
    label: str
    price: Price
    step: int | None = None

    @property
    def bounds(self) -> PriceBounds:
        return price_bounds(self.price, step=self.step)

    @property
    def accept_charge(self) -> Decimal | None:
        """What Accept charges: a fixed or steps price; None when staff set the amount."""
        return self.bounds.lower if has_set_charge(self.price) else None

    @property
    def step_label(self) -> str | None:
        if isinstance(self.price, StepsPrice) and self.step is not None:
            return self.price.steps[self.step].label
        return None


def snapshot_option(option: SupportOption, *, step: int | None) -> OptionSnapshot:
    price_bounds(option.price, step=step)
    return OptionSnapshot(key=option.key, label=option.label, price=option.price, step=step)


def validate_answers(option: SupportOption, answers: Mapping[str, str]) -> dict[str, str]:
    """The answers trimmed, required ones present, sizes and choices checked."""
    known = {f.key for f in option.fields}
    if extra := sorted(set(answers) - known):
        raise SupportError(f"Unknown field: {extra[0]}.")
    clean: dict[str, str] = {}
    for spec in option.fields:
        value = answers.get(spec.key, "").strip()
        if not value:
            if spec.required:
                raise SupportError(f"Fill in {spec.label}.")
            continue
        if isinstance(spec, ChoiceField):
            if value not in spec.choices:
                raise SupportError(f"Pick one of the choices for {spec.label}.")
        elif len(value) > spec.max_chars:
            raise SupportError(f"{spec.label} is too long.")
        clean[spec.key] = value
    return clean


def request_summary(option: SupportOption, answers: Mapping[str, str]) -> str:
    """The note stored and posted for a configured option: one `Label: answer` per field."""
    return "\n".join(f"{f.label}: {answers[f.key]}" for f in option.fields if f.key in answers)


def request_note(option: SupportOption, answers: Mapping[str, str]) -> str:
    """A single field's answer as is (today's note), else `request_summary`."""
    if len(option.fields) == 1:
        return answers.get(option.fields[0].key, "")
    return request_summary(option, answers)


def _normal(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value.normalize())
    raise TypeError(type(value).__name__)


def option_terms(option: SupportOption) -> str:
    """An option's price and fields, hashed: what a menu carries and a submit compares.

    A label or description change keeps them. A removed menu re-added on other
    terms does not, so a revision is never needed to tell them apart.
    """
    data = option.model_dump(include={"price", "fields"})
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), default=_normal)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def option_value(option: SupportOption) -> str:
    """A menu entry's value, `key:terms`: under Discord's 100 and Slack's 150 characters."""
    return f"{option.key}:{option_terms(option)}"


def offered_option(spec: MenuSpec, key: str, terms: str) -> SupportOption | None:
    """`key`'s option if its price and fields are still `terms`, else None (`OPTIONS_CHANGED`)."""
    option = spec.option(key)
    return option if option is not None and option_terms(option) == terms else None


def picked_option(spec: MenuSpec, value: str) -> SupportOption | None:
    """The option an `option_value` names, as `offered_option` decides it."""
    key, _, terms = value.partition(":")
    return offered_option(spec, key, terms)


@dataclass(frozen=True)
class RequestChoice:
    """What a person picked: an option on the `terms` they saw, its step and their answers."""

    option_key: str
    terms: str
    step: int | None = None
    answers: Mapping[str, str] = field(default_factory=lambda: dict[str, str]())

    @classmethod
    def of(
        cls,
        option: SupportOption,
        *,
        step: int | None = None,
        answers: Mapping[str, str] | None = None,
    ) -> RequestChoice:
        """A pick of `option` as shown."""
        return cls(option.key, option_terms(option), step, dict(answers or {}))

    @classmethod
    def default(cls, note: str) -> RequestChoice:
        """Today's request on the implicit default menu."""
        return cls.of(DEFAULT_MENU.options[0], answers={"note": note})


# ---------- Precedence ----------


class ScopedLimit(Protocol):
    @property
    def scope(self) -> SupportScope: ...
    @property
    def scope_id(self) -> str: ...


@dataclass(frozen=True)
class SupportPlace:
    """Where an answer sits: its channel (a thread's parent) and its agent."""

    channel_id: str | None
    agent_id: str | None

    @classmethod
    def of_thread(
        cls, *, channel_id: str | None, budget_channel_id: str | None, agent_id: str | None
    ) -> SupportPlace:
        """Threads roll up to their parent channel, as budgets do."""
        return cls(channel_id=budget_channel_id or channel_id, agent_id=agent_id)


def place_scope_id(scope: SupportScope, place: SupportPlace) -> str | None:
    if scope == "workspace":
        return ""
    return place.channel_id if scope == "channel" else place.agent_id


def governing_limit[P: ScopedLimit](items: Sequence[P], place: SupportPlace) -> P | None:
    """Channel, then agent, then workspace; None means the default ledger."""
    for scope in SUPPORT_SCOPES:
        scope_id = place_scope_id(scope, place)
        if scope_id is None:
            continue
        for item in items:
            if item.scope == scope and item.scope_id == scope_id:
                return item
    return None


def parse_scope(scope: str, scope_id: str | None) -> tuple[SupportScope, str]:
    """`workspace` takes no id; `agent` and `channel` need one."""
    if scope == "workspace":
        if scope_id:
            raise SupportError("Everywhere takes no id.", field="scope_id")
        return "workspace", ""
    if scope not in ("agent", "channel"):
        given = f", not {scope.strip()}" if scope.strip() else ""
        raise SupportError(
            f"A limit is for everywhere, an agent or a channel{given}.", field="scope"
        )
    if not scope_id or not scope_id.strip():
        raise SupportError("No such place.", field="scope_id")
    return ("agent" if scope == "agent" else "channel"), scope_id.strip()


_MENTION = re.compile(r"<@!?([^>|]+)(?:\|[^>]*)?>")
_DISCORD_ID = re.compile(r"\d{17,20}")
_SLACK_ID = re.compile(r"[UW][A-Z0-9]+")


def parse_person_id(platform: str, raw: str) -> str:
    """A person's id as the platform stores it: `<@id>` unwrapped, then checked.

    Discord takes a numeric snowflake, Slack a `U`/`W` member id, Teams an
    Entra object id (lowercase UUID); another platform any non-empty id.
    """
    text = raw.strip()
    mention = _MENTION.fullmatch(text)
    value = mention.group(1).strip() if mention else text
    if not value:
        raise SupportError("Name the person by their user id.")
    if platform == "discord" and not _DISCORD_ID.fullmatch(value):
        raise SupportError(f"{text} is not a Discord user id. Use their numeric id.")
    if platform == "slack":
        value = value.upper()
        if not _SLACK_ID.fullmatch(value):
            raise SupportError(f"{text} is not a Slack user id. Use their member id (U...).")
    if platform == "teams":
        try:
            value = str(uuid.UUID(value))
        except ValueError:
            raise SupportError(
                f"{text} is not a Teams user id. Use their Microsoft Entra object id."
            ) from None
    return value


def check_limit_values(
    *, shared: Decimal | None, each_person: Decimal | None, person: bool = False
) -> None:
    """A place row needs `shared`, `each_person` or both; a person row only `each_person`."""
    if person and shared is not None:
        raise SupportError("A person's limit has no shared credits.", field="shared")
    if person and each_person is None:
        raise SupportError("Field required", field="each_person")
    if shared is None and each_person is None:
        raise SupportError("Set a shared limit, a limit for each person, or both", field="shared")
    for name, value in (("shared", shared), ("each_person", each_person)):
        if value is not None:
            parse_credits(value, name=name)


def check_dates(starts_at: datetime | None, ends_at: datetime | None) -> None:
    if starts_at is not None and ends_at is not None and ends_at <= starts_at:
        raise SupportError("The end must be after the start", field="ends_at")


@dataclass(frozen=True)
class LimitSpec:
    """A limit as a surface parsed it; `platform_user_id` set is a person's own limit."""

    scope: SupportScope
    scope_id: str
    platform_user_id: str | None
    shared: Decimal | None
    each_person: Decimal | None
    window: SupportWindow
    starts_at: datetime | None
    ends_at: datetime | None


def _instant(value: str | datetime | None, *, name: str) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    try:
        return parse_instant(value, name=name)
    except WindowError as exc:
        raise SupportError(str(exc), field=name) from exc


def parse_limit_spec(
    *,
    scope: str,
    scope_id: str | None = None,
    platform_user_id: str | None = None,
    shared: object = None,
    each_person: object = None,
    window: str = "total",
    starts_at: str | datetime | None = None,
    ends_at: str | datetime | None = None,
) -> LimitSpec:
    """One parser for the CLI, MCP and the web; `platform_user_id` is already checked."""
    limit_scope, limit_scope_id = parse_scope(scope, scope_id)
    shared_credits = None if shared is None else parse_credits(shared, name="shared")
    each = None if each_person is None else parse_credits(each_person, name="each_person")
    check_limit_values(shared=shared_credits, each_person=each, person=platform_user_id is not None)
    if window not in SUPPORT_WINDOWS:
        raise SupportError("Choose all time or monthly.", field="window")
    since = _instant(starts_at, name="starts_at")
    until = _instant(ends_at, name="ends_at")
    check_dates(since, until)
    return LimitSpec(
        limit_scope,
        limit_scope_id,
        platform_user_id,
        shared_credits,
        each,
        "monthly" if window == "monthly" else "total",
        since,
        until,
    )


class Dated(Protocol):
    """A limit's period: all time or monthly, between optional dates (end exclusive)."""

    @property
    def window(self) -> SupportWindow: ...
    @property
    def starts_at(self) -> datetime | None: ...
    @property
    def ends_at(self) -> datetime | None: ...


def limit_state(item: Dated, *, now: datetime) -> WindowState:
    """`not_started` before `starts_at`, `ended` from `ends_at` on; dates bind either window."""
    if item.starts_at is not None and now < item.starts_at:
        return "not_started"
    if item.ends_at is not None and now >= item.ends_at:
        return "ended"
    return "active"


def _month_after(start: datetime) -> datetime:
    return datetime(start.year + start.month // 12, start.month % 12 + 1, 1, tzinfo=UTC)


def limit_period(item: Dated, *, as_of: datetime) -> WindowPeriod:
    """What a limit counts at `as_of`: its dates, cut to that UTC month when monthly."""
    since, until = item.starts_at, item.ends_at
    if item.window == "monthly":
        as_of = as_of.astimezone(UTC)
        month = datetime(as_of.year, as_of.month, 1, tzinfo=UTC)
        since = month if since is None else max(since, month)
        nxt = _month_after(month)
        until = nxt if until is None else min(until, nxt)
    return WindowPeriod(since=since, until=until)


# ---------- Balance ----------

BalanceAxis = Literal["shared", "person"]


@dataclass(frozen=True)
class Balance:
    """Both axes of a ledger for one person; an absent limit is unbounded."""

    shared: Decimal | None
    shared_used: Decimal
    cap: Decimal | None
    person_used: Decimal

    @property
    def shared_left(self) -> Decimal | None:
        return None if self.shared is None else max(self.shared - self.shared_used, Decimal(0))

    @property
    def person_left(self) -> Decimal | None:
        return None if self.cap is None else max(self.cap - self.person_used, Decimal(0))

    @property
    def axis(self) -> BalanceAxis | None:
        """The stricter axis; a tie shows the shared one."""
        shared_left, person_left = self.shared_left, self.person_left
        if shared_left is None:
            return None if person_left is None else "person"
        if person_left is None or shared_left <= person_left:
            return "shared"
        return "person"

    @property
    def available(self) -> Decimal | None:
        axis = self.axis
        if axis is None:
            return None
        return self.shared_left if axis == "shared" else self.person_left


def _day(value: datetime) -> str:
    return f"{value.day} {value:%b}"


def last_day_text(ends_at: datetime) -> str:
    """The last day an exclusive end still includes: an end of 18 Dec 00:00 is `17 Dec`."""
    return _day(ends_at.astimezone(UTC) - timedelta(microseconds=1))


def instant_text(value: datetime, *, now: datetime) -> str:
    """An admin's exact time: `24 Nov 09:00 UTC`, with the year when it is not `now`'s."""
    value = value.astimezone(UTC)
    year = "" if value.year == now.year else f" {value.year}"
    return f"{_day(value)}{year} {value:%H:%M} UTC"


def _credits_word(amount: Decimal) -> str:
    return "credit" if amount == 1 else "credits"


def credits_text(amount: Decimal) -> str:
    """`0.5 credits`, `1 credit`."""
    return f"{format_credits(amount)} {_credits_word(amount)}"


def balance_text(balance: Balance, window: Dated | None = None) -> str | None:
    """`1.5 of 2 team credits left, until 17 Dec` or `1 of 2 credits left`; None if unbounded.

    The day is the last one the window includes (`last_day_text`).
    """
    axis = balance.axis
    if axis is None:
        return None
    left = balance.available or Decimal(0)
    limit = (balance.shared if axis == "shared" else balance.cap) or Decimal(0)
    noun = ("team " if axis == "shared" else "") + _credits_word(limit)
    text = f"{format_credits(left)} of {format_credits(limit)} {noun} left"
    if window is not None and window.window == "monthly":
        text += ", this month"
    elif window is not None and window.ends_at is not None:
        text += f", until {last_day_text(window.ends_at)}"
    return text


def inactive_text(window: Dated, *, now: datetime) -> str | None:
    """`Support starts on 18 Nov.` or `Support ended on 17 Dec.` (its last day); None if active."""
    state = limit_state(window, now=now)
    if state == "not_started" and window.starts_at is not None:
        return f"Support starts on {_day(window.starts_at)}."
    if state == "ended" and window.ends_at is not None:
        return f"Support ended on {last_day_text(window.ends_at)}."
    return None


def price_text(price: Price) -> str:
    """`0.5 credits`, `0.5-1 credits`, `Quoted, up to 2 credits`, `Quoted`."""
    if isinstance(price, FixedPrice):
        return credits_text(price.amount)
    if isinstance(price, QuotedPrice):
        return "Quoted" if price.max is None else f"Quoted, up to {credits_text(price.max)}"
    if isinstance(price, StepsPrice):
        amounts = [s.amount for s in price.steps]
        low, high = min(amounts), max(amounts)
    else:
        low, high = price.min, price.max
    if low == high:
        return credits_text(low)
    return f"{format_credits(low)}-{format_credits(high)} credits"


def step_text(step: Step) -> str:
    """`30 min: 0.5 credits`."""
    return f"{step.label}: {credits_text(step.amount)}"


def option_text(option: OptionSnapshot) -> str:
    """A request's terms on its support post: `Review: 0.5-1 credits`, `Call, 30 min: 1 credit`."""
    if (step := option.step_label) is not None:
        return f"{option.label}, {step}: {credits_text(option.bounds.lower)}"
    return f"{option.label}: {price_text(option.price)}"


NOT_ENOUGH_CREDITS: Final = "Not enough credits left. Ask an admin for more."
OPTIONS_CHANGED: Final = "Options changed. Open Ask a person again."
EXEMPT_OFFER: Final = "As an admin, your requests use no credits."
EXEMPT_RECEIVED: Final = "This used no credits."
PENDING_RECEIVED: Final = "We will confirm the charge."
RECEIVED: Final = "Request sent. Someone will reply."
CLIENT_NOT_NOTIFIED: Final = "The client is not notified. Tell them the charge yourself."
"""Decision 8: on every settle control and every settler's confirmation."""
CHOOSE_OPTION: Final = "Choose an option"
SET_CHARGE: Final = "Set charge"
ACCEPT: Final = "Accept"
DECLINE: Final = "Decline"
CHARGE_FIELD: Final = "Credits"
REQUEST_GONE: Final = "This request no longer exists."
LIMIT_GONE: Final = "This request's limit was removed. Decline it instead."
SETTLE_ELSEWHERE: Final = "Set the charge in the support channel."
OWN_REQUEST: Final = "You can't settle your own request. Ask an admin or an owner."


def received_text(row: SupportEscalationRow, view: LimitView) -> str:
    """The requester's confirmation. A limit request adds what it used and what is left.

    `view` is `SubmitOutcome.view`, read with this request already spent. An
    admin's says it used nothing; the default ledger keeps its one line.
    """
    if row.charged_to == "exempt":
        return f"{RECEIVED}\n{EXEMPT_RECEIVED}"
    if row.charged_to == "default":
        return f"{RECEIVED}\n{PENDING_RECEIVED}" if row.status == "pending" else RECEIVED
    if row.status == "pending":
        held = f"Holding {credits_text(row.hold)}. " if row.hold > 0 else ""
        return f"{RECEIVED}\n{held}{PENDING_RECEIVED}"
    left = balance_text(view.balance, view.period)
    used = f"Used {credits_text(row.counted)}."
    return f"{RECEIVED}\n{used} {left}." if left else f"{RECEIVED}\n{used}"


def out_of_credits_text(charged_to: ChargedTo) -> str:
    """The default ledger keeps its request count's words; a limit speaks credits."""
    return OUT_OF_CREDITS if charged_to == "default" else NOT_ENOUGH_CREDITS


def offer_refusal(view: LimitView, *, asked: AbstractSet[str]) -> str | None:
    """Why Ask a person offers nothing here, with the balance; None while an option is open.

    `asked` is `asked_options` on this answer. A repeat reads as already asked
    before any credit check, as at submit, so the menu shows no picker. The
    default ledger on the default menu checks only credits, as before limits.
    Every platform calls this before offering a menu or a form.
    """
    if view.limit is None and view.menu is None:
        cheapest = cheapest_bounds(view.spec.options[0].price)
        return None if view.affordable(cheapest) else out_of_credits_text(view.charged_to)
    open_options = [o for o in view.spec.options if o.key not in asked]
    if any(view.affordable(cheapest_bounds(o.price)) for o in open_options):
        return None
    refusal = ALREADY_REQUESTED if asked else NOT_ENOUGH_CREDITS
    left = None if view.exempt else balance_text(view.balance, view.period)
    return f"{refusal}\n{left}." if left else refusal


def pick_refusal(view: LimitView, option: SupportOption, *, asked: AbstractSet[str]) -> str | None:
    """Why a picked option opens no form, in the submit's order: already asked, then credits."""
    if option.key in asked:
        return ALREADY_REQUESTED
    if not view.affordable(cheapest_bounds(option.price)):
        return NOT_ENOUGH_CREDITS
    return None


def ended_text(view: LimitView, *, now: datetime) -> str:
    """Why a limit outside its dates takes no request."""
    for item in (view.limit, view.person):
        if item is not None and (text := inactive_text(item, now=now)):
            return text
    return UNAVAILABLE


def refusal_text(outcome: SubmitOutcome, *, now: datetime) -> str:
    """Why `submit_request` recorded nothing; a "duplicate" is a neutral notice, the rest errors."""
    view = outcome.view
    if outcome.status == "duplicate":
        return ALREADY_REQUESTED
    if outcome.status == "out_of_credits" and view is not None:
        return out_of_credits_text(view.charged_to)
    if outcome.status == "stale":
        return OPTIONS_CHANGED
    if outcome.status == "limit_inactive" and view is not None:
        return ended_text(view, now=now)
    return UNAVAILABLE


def post_status_text(
    *, status: RequestStatus, charged_to: ChargedTo, charge: Decimal | None, bounds: PriceBounds
) -> str:
    """The support post's status line: `Charged 0.5 credits`, `Pending: 0.5-1 credits`, ..."""
    if charged_to == "exempt":
        return "No charge: an admin asked"
    if status == "declined":
        return "Declined"
    if status == "settled" and charge is not None:
        return f"Charged {credits_text(charge)}"
    if bounds.upper is None:
        return f"Pending: at least {credits_text(bounds.lower)}"
    if bounds.upper == bounds.lower:
        return f"Pending: {credits_text(bounds.lower)}"
    return f"Pending: {format_credits(bounds.lower)}-{format_credits(bounds.upper)} credits"


def settled_text(*, charge: Decimal | None) -> str:
    """The settler's confirmation (`charge` None is a decline), with the decision-8 warning."""
    done = "Declined. No credits used." if charge is None else f"Charged {credits_text(charge)}."
    return f"{done} {CLIENT_NOT_NOTIFIED}"


def already_settled_text(*, status: RequestStatus, charge: Decimal | None, by: str | None) -> str:
    what = "declined" if status == "declined" else f"charged {credits_text(charge or Decimal(0))}"
    return f"Already {what}{f' by {by}' if by else ''}."


def charged_at_submit_text(*, charge: Decimal | None) -> str:
    """The refusal for another amount on a request already charged at submit."""
    return (
        f"Charged {credits_text(charge or Decimal(0))} already. Keep that or decline to refund it."
    )


def settle_range_text(
    bounds: PriceBounds, *, counted: Decimal, available: Decimal | None, correction: bool = False
) -> str:
    """The refusal for an amount out of bounds or over the balance left (as `settle_problem`)."""
    lower = Decimal(0) if correction else bounds.lower
    upper = bounds.upper
    if available is not None and (upper is None or counted + available < upper):
        upper = counted + available
    if upper is None:
        return f"Enter at least {format_credits(lower)}."
    return f"Enter an amount from {format_credits(lower)} to {format_credits(upper)}."


__all__ = [
    "CLIENT_NOT_NOTIFIED",
    "DEFAULT_MENU",
    "DEFAULT_OPTION_KEY",
    "Balance",
    "OptionSnapshot",
    "LimitSpec",
    "MenuChannel",
    "PriceBounds",
    "RequestChoice",
    "MenuSpec",
    "SupportOption",
    "SupportPlace",
    "SupportError",
    "charged_at_submit_text",
    "format_credits",
    "governing_limit",
    "parse_amount",
    "parse_menu",
    "parse_credits",
    "parse_person_id",
    "parse_limit_spec",
    "price_bounds",
]
