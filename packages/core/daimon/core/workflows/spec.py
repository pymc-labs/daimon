"""The workflow spec, schema `daimon.workflow/v1`: Pydantic models, JSON Schema and hashes.

Optional fields default to None and the engine applies the defaults, so a spec dumps back
to what its author wrote (`dump_spec`). Value templates are any JSON; their strings are
`${{ }}` templates (`daimon.core.workflows.expressions`). The web mirror of these
shapes is `web/src/modules/workflows/types.ts`.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, timedelta
from typing import Annotated, Final, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)

SCHEMA: Final = "daimon.workflow/v1"
MAX_NODES: Final = 50
MAX_VISITS: Final = 20
MAX_STEPS: Final = 500
MAX_ITEMS_CONCURRENCY: Final = 10
MAX_DEADLINE: Final = timedelta(days=90)
MAX_AGENT_TIMEOUT: Final = timedelta(minutes=45)
MAX_CODE_TIMEOUT: Final = timedelta(minutes=60)
MAX_ATTACHMENTS: Final = 10
MAX_ALSO: Final = 5
MAX_MENTIONS: Final = 10
MAX_TITLE: Final = 100

Id = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
#: `1h30m`, or business days alone (`2bd`: Mon-Fri in the spec's timezone, no holidays).
Duration = Annotated[str, StringConstraints(pattern=r"^((\d+[smhd])+|\d+bd)$")]
Template = JsonValue
ToolName = Annotated[str, StringConstraints(pattern=r"^[^/\s]+/[^/\s]+$")]
_DURATION_PART = re.compile(r"(\d+)([smhd])")
_BUSINESS_DAYS = re.compile(r"(\d+)bd")
_UNITS: Final = {"s": 1, "m": 60, "h": 3600, "d": 86400}
#: owner, editors, admins, triggered_by, thread, person:<id>, role:<web role>, or `${{ }}`.
_APPROVER = re.compile(
    r"^(owner|editors|admins|triggered_by|thread|person:\S+|role:\S+|\$\{\{.+\}\})$"
)


def business_days(text: str) -> int | None:
    """`3bd` → 3; None for a fixed-length duration."""
    found = _BUSINESS_DAYS.fullmatch(text)
    return int(found.group(1)) if found else None


def parse_duration(text: str) -> timedelta:
    """`1h30m` → 90 minutes. The `Duration` pattern guarantees the shape. Business days
    have no fixed length (`clock.add_duration` counts them), so they raise `ValueError`."""
    if business_days(text) is not None:
        raise ValueError(f"{text} counts business days; it has no fixed length")
    return timedelta(seconds=sum(int(n) * _UNITS[u] for n, u in _DURATION_PART.findall(text)))


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class SchemaField(_Model):
    """One field of inputs, outputs and approval forms: a JSON Schema subset."""

    type: Literal["string", "number", "integer", "boolean", "object", "array"] | None = None
    enum: list[str] | None = None
    title: str | None = None
    description: str | None = None
    format: str | None = None
    default: JsonValue = None
    max_length: int | None = Field(default=None, alias="maxLength", ge=1)
    items: SchemaField | None = None


class ObjectSchema(_Model):
    type: Literal["object"]
    required: list[str] | None = None
    properties: dict[str, SchemaField]


class Route(_Model):
    to: str
    when: str | None = None


class Retry(_Model):
    attempts: int = Field(ge=1, le=10)


class _Base(_Model):
    title: str | None = None
    description: str | None = None
    join: Literal["all", "first"] | None = None
    #: The board column a run shows in while this step is its current one.
    stage: str | None = Field(default=None, max_length=40)


class _Flow(_Base):
    next: str | list[str] | None = None
    on_error: str | None = None
    retry: Retry | None = None
    budget_usd: float | None = Field(default=None, gt=0)
    max_visits: int | None = Field(default=None, ge=1, le=MAX_VISITS)
    on_max_visits: str | None = None


class CreatedBy(_Model):
    """The value is an id this run got back from an earlier allowed call to `tool`."""

    tool: ToolName
    #: Where the id sits in that call's result, as `.id` or `.page.id`.
    id: str


class ArgRule(_Model):
    """What one argument may be; any one of the set rules passing is enough."""

    in_: list[str | int | float | bool] | None = Field(default=None, alias="in", min_length=1)
    prefix: list[str] | None = Field(default=None, min_length=1)
    #: One plain email address whose domain is listed.
    domains: list[str] | None = Field(default=None, min_length=1)
    created_by: CreatedBy | None = None

    @model_validator(mode="after")
    def _some_rule(self) -> ArgRule:
        if all(x is None for x in (self.in_, self.prefix, self.domains, self.created_by)):
            raise ValueError("An argument rule needs in, prefix, domains or created_by")
        return self


class WriteScope(_Model):
    """A write a step may make with nobody watching, when its arguments fit: every argument
    is named in `where` (dotted for nested ones) or `free`, and each `where` rule passes."""

    tool: ToolName
    where: dict[str, ArgRule] = Field(default_factory=dict[str, ArgRule])
    free: list[str] | None = None


class AgentNode(_Flow):
    kind: Literal["agent"]
    agent: str | None = None
    model: str | None = None
    instructions: str
    inputs: dict[str, Template] | None = None
    output: dict[str, SchemaField] | None = None
    routes: dict[str, str | Route] | None = None
    tools: Literal["inherit"] | list[str] | None = None
    writes: Literal["refuse", "propose", "allow"] | None = None
    review: Literal["auto"] | list[str] | None = None
    builtins: Literal["none", "read", "all"] | None = None
    session: str | None = None
    can_ask: bool | None = None
    can_finish_early: bool | None = None
    untrusted_waiver: str | None = None
    timeout: Duration | None = None
    writes_to: list[WriteScope] | None = None
    #: Library skills the step's agent gets, pinned by content at publish.
    skills: list[str] | None = None


class BranchCase(_Model):
    when: str
    to: str


class BranchNode(_Flow):
    kind: Literal["branch"]
    cases: list[BranchCase] = Field(min_length=1)
    else_: str | None = Field(default=None, alias="else")


class TransformNode(_Flow):
    kind: Literal["transform"]
    set: dict[str, Template] = Field(min_length=1)


class CodeNode(_Flow):
    kind: Literal["code"]
    language: Literal["python"] | None = None
    source: str
    inputs: dict[str, Template] | None = None
    output: dict[str, SchemaField] | None = None
    network: Literal["off", "on"] | None = None
    environment: str | None = None
    timeout: Duration | None = None


class ActionNode(_Flow):
    kind: Literal["action"]
    tool: ToolName
    args: dict[str, Template] | None = None
    idempotent: bool | None = None
    timeout: Duration | None = None
    writes_to: list[WriteScope] | None = None


class ApprovalTimeout(_Model):
    after: Duration
    #: A route label, or `fail`.
    route: str


class ApprovalNode(_Flow):
    """A person decides or answers. `answer` picks the shape: approve or reject, one of the
    routes, free text with files, or a form. Output: route, text, values, files, by, via,
    with form answers also at the top level."""

    kind: Literal["approval"]
    approvers: list[Annotated[str, StringConstraints(pattern=_APPROVER.pattern)]] | None = None
    min_approvals: int | None = Field(default=None, ge=1)
    allow_self_approval: bool | None = None
    deliver: Literal["dm", "thread", "inbox"] | None = None
    channel: str | None = None
    show: str | None = None
    attach: str | None = None
    answer: Literal["approve", "choice", "text", "form"] | None = None
    form: dict[str, SchemaField] | None = None
    binds: str | list[str] | None = None
    routes: dict[str, str] | None = None
    timeout: ApprovalTimeout | None = None
    remind_every: Duration | None = None
    #: Where a reply that starts with no option word goes, with its text.
    otherwise: str | None = None


class WaitEvent(_Model):
    source: str
    match: str | None = None
    key: str | None = None


class WaitPoll(_Model):
    """Call a read tool every `every` until `until` (an expression over `result`) holds."""

    agent: str
    tool: ToolName
    args: dict[str, Template] | None = None
    every: Duration
    until: str


class WaitNode(_Flow):
    kind: Literal["wait"]
    for_: Duration | None = Field(default=None, alias="for")
    until: str | None = None
    event: WaitEvent | None = None
    poll: WaitPoll | None = None
    timeout: Duration | None = None
    routes: dict[Literal["arrived", "timed_out"], str] | None = None

    @model_validator(mode="after")
    def _one_mode(self) -> WaitNode:
        if sum(x is not None for x in (self.for_, self.until, self.event, self.poll)) != 1:
            raise ValueError("A wait needs exactly one of for, until, event or poll")
        return self


class WorkflowNode(_Flow):
    kind: Literal["workflow"]
    workflow: str
    version: Literal["live"] | Annotated[int, Field(ge=1)] | None = None
    inputs: dict[str, Template] | None = None


class Deliver(_Model):
    #: home, owner, run_as, origin, a channel or thread id, or a `${{ }}` template.
    to: str
    #: Under a channel, the thread with exactly this title, made on first use.
    thread: str | None = Field(default=None, max_length=MAX_TITLE)
    #: Direct messages besides `to`: owner, run_as, `person:<id>` or a template.
    also: list[str] | None = Field(default=None, max_length=MAX_ALSO)
    #: `person:<id>`, `role:<id>` or a template.
    mention: list[str] | None = Field(default=None, max_length=MAX_MENTIONS)
    message: str | None = None
    #: File refs to attach, such as `${{ steps.report.files }}` (at most 10).
    files: Template = None


class PostNode(_Flow, Deliver):
    """Post now and carry on; the step's output is where the post went."""

    kind: Literal["post"]


class FinishNode(_Base):
    kind: Literal["finish"]
    output: Template = None
    outcome: Literal["succeeded", "failed"] | None = None
    deliver: Deliver | None = None
    #: Merged into `workflow.state` by a live run; a null value deletes the key.
    state: dict[Annotated[str, StringConstraints(max_length=MAX_TITLE)], Template] | None = None


ItemNode = Annotated[
    AgentNode | CodeNode | ActionNode | WorkflowNode | PostNode, Field(discriminator="kind")
]


class ForEachNode(_Flow):
    kind: Literal["for_each"]
    items: str
    as_: Id | None = Field(default=None, alias="as")
    do: ItemNode
    concurrency: int | None = Field(default=None, ge=1, le=MAX_ITEMS_CONCURRENCY)
    tolerate_failures: int | None = Field(default=None, ge=0)


Node = Annotated[
    AgentNode
    | BranchNode
    | TransformNode
    | CodeNode
    | ActionNode
    | ApprovalNode
    | WaitNode
    | ForEachNode
    | WorkflowNode
    | PostNode
    | FinishNode,
    Field(discriminator="kind"),
]
Kind = Literal[
    "agent",
    "branch",
    "transform",
    "code",
    "action",
    "approval",
    "wait",
    "for_each",
    "workflow",
    "post",
    "finish",
]


class ManualTrigger(_Model):
    id: Id
    kind: Literal["manual"]


class ScheduleTrigger(_Model):
    """A cron (optionally every few weeks from `anchor`'s week) or one time `at`."""

    id: Id
    kind: Literal["schedule"]
    cron: str | None = None
    timezone: str | None = None
    catch_up: Literal["skip", "run-once"] | None = None
    with_: dict[str, JsonValue] | None = Field(default=None, alias="with")
    every_weeks: int | None = Field(default=None, ge=2, le=8)
    anchor: date | None = None
    at: AwareDatetime | None = None


class WebhookTrigger(_Model):
    id: Id
    kind: Literal["webhook"]
    verify: Literal["standard-webhooks", "github", "stripe", "slack", "bearer"] | None = None
    #: A mapping from `payload` to the workflow's inputs.
    inputs: Template = None
    respond: Literal["accepted", "result"] | None = None
    start_when: str | None = None
    #: Where the delivery id lives when no header carries one.
    delivery_id: str | None = None
    spend_cap_usd: float | None = Field(default=None, gt=0)


class EventTrigger(_Model):
    id: Id
    kind: Literal["event"]
    source: str
    match: str | None = None
    inputs: Template = None
    spend_cap_usd: float | None = Field(default=None, gt=0)


class CallTrigger(_Model):
    id: Id
    kind: Literal["call"]


class MentionTrigger(_Model):
    """A chat message in `channels` (their threads too) or passing `match` starts a run."""

    id: Id
    kind: Literal["mention"]
    channels: list[str] | None = None
    #: An expression over `message` and `thread`.
    match: str | None = None
    inputs: Template = None


class PollTrigger(_Model):
    """Call a read tool every `every`; each new (or, with `updated`, changed) row starts a run."""

    id: Id
    kind: Literal["poll"]
    agent: str
    tool: ToolName
    args: dict[str, Template] | None = None
    every: Duration
    #: Where the rows are in the result, and each row's key, as `.results` and `.id`.
    items: str | None = None
    key: str
    rule: Literal["added", "updated"] | None = None
    #: With `updated`: the fields whose change counts.
    watch: list[str] | None = None
    start_when: str | None = None
    #: A key starts at most one run, ever.
    once: bool | None = None
    inputs: Template = None
    spend_cap_usd: float | None = Field(default=None, gt=0)


Trigger = Annotated[
    ManualTrigger
    | ScheduleTrigger
    | WebhookTrigger
    | EventTrigger
    | CallTrigger
    | MentionTrigger
    | PollTrigger,
    Field(discriminator="kind"),
]


class Home(_Model):
    channel: str


class Concurrency(_Model):
    key: str | None = None
    limit: int | None = Field(default=None, ge=1, le=100)
    on_conflict: Literal["queue", "skip", "replace", "debounce"] | None = None
    debounce: Duration | None = None


class Defaults(_Model):
    agent: str | None = None
    budget_usd: float | None = Field(default=None, gt=0)


class Limits(_Model):
    deadline: Duration | None = None
    max_steps: int | None = Field(default=None, ge=1, le=MAX_STEPS)
    budget_usd: float | None = Field(default=None, gt=0)


class Card(_Model):
    """What a run's card on the board shows; `date` names the field the calendar uses."""

    fields: dict[str, Template] = Field(min_length=1)
    date: str | None = None


class Spec(_Model):
    schema_: Literal["daimon.workflow/v1"] = Field(alias="schema")
    name: str = Field(min_length=1, max_length=120)
    goal: str | None = None
    home: Home | None = None
    #: Channel ids and `person:<id>` that templated delivery targets may resolve to.
    destinations: list[str] | None = None
    #: For `run.date`, business days and schedules without their own; UTC when unset.
    timezone: str | None = None
    card: Card | None = None
    visibility: Literal["private", "shared"] | None = None
    label: str | None = None
    inputs: ObjectSchema | None = None
    triggers: list[Trigger] | None = None
    concurrency: Concurrency | None = None
    defaults: Defaults | None = None
    limits: Limits | None = None
    start: str
    nodes: dict[Id, Node] = Field(min_length=1, max_length=MAX_NODES)


AnyNode = (
    AgentNode
    | BranchNode
    | TransformNode
    | CodeNode
    | ActionNode
    | ApprovalNode
    | WaitNode
    | ForEachNode
    | WorkflowNode
    | PostNode
    | FinishNode
)


def parse_spec(document: JsonValue) -> Spec:
    """Validate a JSON document as a spec; raises `pydantic.ValidationError`."""
    return Spec.model_validate(document)


def dump_spec(spec: Spec) -> dict[str, JsonValue]:
    return spec.model_dump(mode="json", by_alias=True, exclude_none=True)


def dump_node(node: AnyNode) -> dict[str, JsonValue]:
    return node.model_dump(mode="json", by_alias=True, exclude_none=True)


def _digest(document: JsonValue) -> str:
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def spec_hash(spec: Spec) -> str:
    """sha256 of the canonical JSON; versions store it."""
    return _digest(dump_spec(spec))


def node_hash(node: AnyNode) -> str:
    """sha256 of one node's canonical JSON; grants pin it."""
    return _digest(dump_node(node))


def spec_json_schema() -> dict[str, JsonValue]:
    """The spec's JSON Schema, for editors and the authoring tools."""
    return Spec.model_json_schema(by_alias=True)


def slug(name: str) -> str:
    """An agent's key in a spec: its name in lowercase words joined by dashes."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
