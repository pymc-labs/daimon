"""Support plans for Ask a person: credits, option catalogs, pricing, precedence, copy. Pure.

A plan on a workspace, an agent or a channel gives a shared `pool`, a
`per_person` cap, or both, over a window (`daimon.core.windows`). The most
specific plan governs and plans never stack; with none, the implicit default
plan is `credits_per_user` per person over all time, one option at 1 credit.

Credits are `Decimal` with at most two places. JSON and YAML carry them as
decimal strings ("0.5"); a float is refused so no value is ever rounded on
the way in. They never touch the USD ledger.

Prices bound the charge: `fixed` and `steps` settle at submit, `range` and
`quoted` stay pending and hold their minimum until staff settle them.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Final, Literal, Protocol

import yaml
from daimon.core.errors import DaimonError
from daimon.core.windows import (
    WindowBounds,
    Windowed,
    WindowError,
    parse_window,
    window_state,
)
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
MAX_CATALOG_NAME: Final = 75
MAX_AMOUNT: Final = Decimal(1000)
MAX_LIMIT: Final = Decimal(100000)
MIN_CHARGE: Final = Decimal("0.01")
CATALOG_KEY_PATTERN: Final = r"[a-z0-9-]{1,32}"
OPTION_KEY_PATTERN: Final = r"[a-z0-9_]{1,32}"

PlanScope = Literal["workspace", "agent", "channel"]
PLAN_SCOPES: Final[tuple[PlanScope, ...]] = ("channel", "agent", "workspace")
"""Most specific first: the order `governing_plan` tries."""
ChargedTo = Literal["default", "plan", "exempt"]
RequestStatus = Literal["pending", "settled", "declined"]


class SupportPlanError(DaimonError):
    """A plan, catalog, amount or answer that cannot be used; the message says what to fix."""


# ---------- Credits ----------


def _decimal(value: object, *, name: str) -> Decimal:
    if isinstance(value, bool | float):
        raise SupportPlanError(f'{name} must be a decimal string such as "0.5"')
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, Decimal):
        amount = value
    elif isinstance(value, str):
        try:
            amount = Decimal(value.strip())
        except (InvalidOperation, ValueError) as exc:
            raise SupportPlanError(f"{name} must be a number of credits, got {value!r}") from exc
    else:
        raise SupportPlanError(f'{name} must be a decimal string such as "0.5"')
    exponent = amount.as_tuple().exponent
    if not amount.is_finite():
        raise SupportPlanError(f"{name} must be a number of credits")
    if isinstance(exponent, int) and exponent < -2:
        raise SupportPlanError(f"{name} must have at most two decimal places")
    return amount


def parse_credits(value: object, *, name: str = "credits") -> Decimal:
    """A limit or allowance: 0 to 100000, at most two decimal places."""
    amount = _decimal(value, name=name)
    if amount < 0 or amount > MAX_LIMIT:
        raise SupportPlanError(f"{name} must be from 0 to {format_credits(MAX_LIMIT)}")
    return amount


def parse_amount(value: object, *, name: str = "amount", allow_zero: bool = False) -> Decimal:
    """A price: above 0 (or 0 with `allow_zero`) and at most 1000."""
    amount = _decimal(value, name=name)
    if amount > MAX_AMOUNT or amount < 0 or (amount == 0 and not allow_zero):
        low = "0" if allow_zero else "more than 0"
        raise SupportPlanError(f"{name} must be {low} and at most {format_credits(MAX_AMOUNT)}")
    return amount


def format_credits(value: Decimal) -> str:
    """`1`, `0.5`, `1.25`: no trailing zeros, never an exponent."""
    return f"{value.normalize():f}"


# Pydantic reports only a ValueError's message, so the parsers are wrapped.
def _credits_in(value: object) -> Decimal:
    try:
        return _decimal(value, name="credits")
    except SupportPlanError as exc:
        raise ValueError(str(exc)) from exc


def _amount_check(value: Decimal) -> Decimal:
    try:
        return parse_amount(value)
    except SupportPlanError as exc:
        raise ValueError(str(exc)) from exc


def _min_check(value: Decimal) -> Decimal:
    try:
        return parse_amount(value, name="min", allow_zero=True)
    except SupportPlanError as exc:
        raise ValueError(str(exc)) from exc


_CreditsText = PlainSerializer(format_credits, return_type=str)
Amount = Annotated[
    Decimal, BeforeValidator(_credits_in), AfterValidator(_amount_check), _CreditsText
]
"""A price amount in a catalog: a decimal string, above 0, at most 1000."""
MinAmount = Annotated[
    Decimal, BeforeValidator(_credits_in), AfterValidator(_min_check), _CreditsText
]

# ---------- Catalog spec ----------

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


class SupportCatalogSpec(_Spec):
    """A tenant's menu for Ask a person, as stored in `support_catalogs.options`."""

    name: Annotated[_Text, StringConstraints(max_length=MAX_CATALOG_NAME)]
    options: tuple[SupportOption, ...] = Field(min_length=1, max_length=MAX_OPTIONS)

    @model_validator(mode="after")
    def _distinct(self) -> SupportCatalogSpec:
        if len({o.key for o in self.options}) != len(self.options):
            raise ValueError("option keys must be different")
        return self

    def option(self, key: str) -> SupportOption | None:
        return next((o for o in self.options if o.key == key), None)


def _readable(exc: ValidationError) -> str:
    errors = exc.errors()
    # A tuple whose item failed also reports itself too short; the item is the news.
    own = [e for e in errors if e["type"] != "too_short"] or errors
    problems: list[str] = []
    for error in own[:3]:
        parts = [str(part) for part in error["loc"]]
        where = ".".join(p for p in parts if not p.startswith("function-"))
        message = str(error["msg"]).removeprefix("Value error, ")
        problems.append(f"{where}: {message}" if where else message)
    return "; ".join(problems)


def parse_catalog(data: object) -> SupportCatalogSpec:
    """Validate a catalog from a file, a tool or the web; the error names the bad entry."""
    try:
        return SupportCatalogSpec.model_validate(data)
    except ValidationError as exc:
        raise SupportPlanError(f"Catalog not valid: {_readable(exc)}") from exc


def load_catalog(text: str) -> SupportCatalogSpec:
    """`parse_catalog` over YAML or JSON text (JSON is YAML)."""
    try:
        data: object = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SupportPlanError("Catalog not valid: not YAML or JSON") from exc
    return parse_catalog(data)


def parse_catalog_key(key: str) -> str:
    key = key.strip()
    if not re.fullmatch(CATALOG_KEY_PATTERN, key):
        raise SupportPlanError("A catalog key is 1-32 lowercase letters, digits or dashes.")
    return key


DEFAULT_OPTION_KEY: Final = "request"
DEFAULT_FIELD_LABEL: Final = "What do you need help with?"
DEFAULT_CATALOG: Final = SupportCatalogSpec(
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
"""Implicit, never stored: today's one request at 1 credit. Its revision is 0."""
DEFAULT_REVISION: Final = 0

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
            raise SupportPlanError(f"Pick a {price.label.lower()}.")
        amount = price.steps[step].amount
        return PriceBounds(amount, amount)
    if step is not None:
        raise SupportPlanError("This option has no steps.")
    if isinstance(price, FixedPrice):
        return PriceBounds(price.amount, price.amount)
    return PriceBounds(price.min, price.max)


def settles_at_submit(price: Price) -> bool:
    """Fixed and steps prices are known at submit; range and quoted wait for staff."""
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
    """The terms a request was made on, stored with it; settling never reads the live catalog."""

    key: str
    label: str
    price: Price
    step: int | None = None

    @property
    def bounds(self) -> PriceBounds:
        return price_bounds(self.price, step=self.step)

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
        raise SupportPlanError(f"Unknown field: {extra[0]}.")
    clean: dict[str, str] = {}
    for spec in option.fields:
        value = answers.get(spec.key, "").strip()
        if not value:
            if spec.required:
                raise SupportPlanError(f"Fill in {spec.label}.")
            continue
        if isinstance(spec, ChoiceField):
            if value not in spec.choices:
                raise SupportPlanError(f"Pick one of the choices for {spec.label}.")
        elif len(value) > spec.max_chars:
            raise SupportPlanError(f"{spec.label} is too long.")
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


@dataclass(frozen=True)
class RequestChoice:
    """What a person picked on a menu of `revision`: an option, its step and their answers."""

    option_key: str
    revision: int
    step: int | None = None
    answers: Mapping[str, str] = field(default_factory=lambda: dict[str, str]())

    @classmethod
    def default(cls, note: str) -> RequestChoice:
        """Today's request on the implicit default menu."""
        return cls(DEFAULT_OPTION_KEY, DEFAULT_REVISION, answers={"note": note})


# ---------- Precedence ----------


class ScopedPlan(Protocol):
    @property
    def scope(self) -> PlanScope: ...
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


def plan_scope_id(scope: PlanScope, place: SupportPlace) -> str | None:
    if scope == "workspace":
        return ""
    return place.channel_id if scope == "channel" else place.agent_id


def governing_plan[P: ScopedPlan](plans: Sequence[P], place: SupportPlace) -> P | None:
    """Channel, then agent, then workspace; None means the implicit default plan."""
    for scope in PLAN_SCOPES:
        scope_id = plan_scope_id(scope, place)
        if scope_id is None:
            continue
        for plan in plans:
            if plan.scope == scope and plan.scope_id == scope_id:
                return plan
    return None


def parse_scope(scope: str, scope_id: str | None) -> tuple[PlanScope, str]:
    """`workspace` takes no id; `agent` and `channel` need one."""
    if scope == "workspace":
        if scope_id:
            raise SupportPlanError("A workspace plan takes no id.")
        return "workspace", ""
    if scope not in ("agent", "channel"):
        raise SupportPlanError("A plan is for a workspace, an agent or a channel.")
    if not scope_id or not scope_id.strip():
        raise SupportPlanError(f"Name the {scope} for this plan.")
    return ("agent" if scope == "agent" else "channel"), scope_id.strip()


def check_plan_limits(*, pool: Decimal | None, per_person: Decimal | None) -> None:
    if pool is None and per_person is None:
        raise SupportPlanError("Set a pool, a per-person limit or both.")
    for name, value in (("pool", pool), ("per_person", per_person)):
        if value is not None:
            parse_credits(value, name=name)


@dataclass(frozen=True)
class PlanSpec:
    """A plan's terms as a surface parsed them; `catalog` None is the default menu."""

    scope: PlanScope
    scope_id: str
    catalog: str | None
    pool: Decimal | None
    per_person: Decimal | None
    window: WindowBounds


def parse_plan_spec(
    *,
    scope: str,
    scope_id: str | None = None,
    catalog: str | None = None,
    pool: object = None,
    per_person: object = None,
    window: str = "total",
    starts_at: str | None = None,
    ends_at: str | None = None,
) -> PlanSpec:
    """One parser for the CLI, MCP and the web; `catalog` "default" is the default menu."""
    plan_scope, plan_scope_id_ = parse_scope(scope, scope_id)
    pool_credits = None if pool is None else parse_credits(pool, name="pool")
    person_credits = None if per_person is None else parse_credits(per_person, name="per_person")
    check_plan_limits(pool=pool_credits, per_person=person_credits)
    try:
        bounds = parse_window(window, starts_at=starts_at, ends_at=ends_at)
    except WindowError as exc:
        raise SupportPlanError(str(exc)) from exc
    key = None if catalog in (None, "default") else parse_catalog_key(catalog)
    return PlanSpec(plan_scope, plan_scope_id_, key, pool_credits, person_credits, bounds)


# ---------- Balance ----------

BalanceAxis = Literal["pool", "person"]


@dataclass(frozen=True)
class Balance:
    """Both axes of a plan for one person; an absent limit is unbounded."""

    pool: Decimal | None
    pool_used: Decimal
    cap: Decimal | None
    person_used: Decimal

    @property
    def pool_left(self) -> Decimal | None:
        return None if self.pool is None else max(self.pool - self.pool_used, Decimal(0))

    @property
    def person_left(self) -> Decimal | None:
        return None if self.cap is None else max(self.cap - self.person_used, Decimal(0))

    @property
    def axis(self) -> BalanceAxis | None:
        """The stricter axis; a tie shows the pool."""
        pool_left, person_left = self.pool_left, self.person_left
        if pool_left is None:
            return None if person_left is None else "person"
        if person_left is None or pool_left <= person_left:
            return "pool"
        return "person"

    @property
    def available(self) -> Decimal | None:
        axis = self.axis
        if axis is None:
            return None
        return self.pool_left if axis == "pool" else self.person_left


def _day(value: datetime) -> str:
    return f"{value.day} {value:%b}"


def _credits_word(amount: Decimal) -> str:
    return "credit" if amount == 1 else "credits"


def credits_text(amount: Decimal) -> str:
    """`0.5 credits`, `1 credit`."""
    return f"{format_credits(amount)} {_credits_word(amount)}"


def balance_text(balance: Balance, window: Windowed | None = None) -> str | None:
    """`1.5 of 2 team credits left, until 18 Dec` or `1 of 2 credits left`; None if unbounded."""
    axis = balance.axis
    if axis is None:
        return None
    left = balance.available or Decimal(0)
    limit = (balance.pool if axis == "pool" else balance.cap) or Decimal(0)
    noun = ("team " if axis == "pool" else "") + _credits_word(limit)
    text = f"{format_credits(left)} of {format_credits(limit)} {noun} left"
    if window is not None and window.window == "fixed" and window.ends_at is not None:
        text += f", until {_day(window.ends_at)}"
    elif window is not None and window.window == "monthly":
        text += ", this month"
    return text


def inactive_text(window: Windowed, *, now: datetime) -> str | None:
    """`Support starts on 18 Nov.` or `Support ended on 18 Dec.`; None while active."""
    state = window_state(window, now=now)
    if state == "not_started" and window.starts_at is not None:
        return f"Support starts on {_day(window.starts_at)}."
    if state == "ended" and window.ends_at is not None:
        return f"Support ended on {_day(window.ends_at)}."
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


NOT_ENOUGH_CREDITS: Final = "Not enough credits left."
OPTIONS_CHANGED: Final = "Options changed. Open Ask a person again."
EXEMPT_OFFER: Final = "As an admin, your requests use no credits."
EXEMPT_RECEIVED: Final = "This used no credits."
PENDING_RECEIVED: Final = "We will confirm the charge."
RECEIVED: Final = "Request sent. Someone will reply."
CLIENT_NOT_NOTIFIED: Final = "The client is not notified. Tell them the charge yourself."
"""Decision 8: on every settle control and every settler's confirmation."""


def received_text(*, status: RequestStatus, charged_to: ChargedTo) -> str:
    """The requester's confirmation: pending adds a line, an admin's says it used nothing."""
    if charged_to == "exempt":
        return f"{RECEIVED}\n{EXEMPT_RECEIVED}"
    if status == "pending":
        return f"{RECEIVED}\n{PENDING_RECEIVED}"
    return RECEIVED


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
    return f"Pending: {format_credits(bounds.lower)}-{format_credits(bounds.upper)} credits"


def settled_text(*, charge: Decimal | None) -> str:
    """The settler's confirmation (`charge` None is a decline), with the decision-8 warning."""
    done = "Declined. No credits used." if charge is None else f"Charged {credits_text(charge)}."
    return f"{done} {CLIENT_NOT_NOTIFIED}"


def already_settled_text(*, status: RequestStatus, charge: Decimal | None, by: str | None) -> str:
    what = "declined" if status == "declined" else f"charged {credits_text(charge or Decimal(0))}"
    return f"Already {what}{f' by {by}' if by else ''}."


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
    "DEFAULT_CATALOG",
    "DEFAULT_OPTION_KEY",
    "DEFAULT_REVISION",
    "Balance",
    "OptionSnapshot",
    "PlanSpec",
    "PriceBounds",
    "RequestChoice",
    "SupportCatalogSpec",
    "SupportOption",
    "SupportPlace",
    "SupportPlanError",
    "format_credits",
    "governing_plan",
    "parse_amount",
    "parse_catalog",
    "parse_credits",
    "parse_plan_spec",
    "price_bounds",
]
