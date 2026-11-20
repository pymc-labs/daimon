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
from datetime import timedelta
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, model_validator

SCHEMA: Final = "daimon.workflow/v1"
MAX_NODES: Final = 50
MAX_VISITS: Final = 20
MAX_STEPS: Final = 500
MAX_ITEMS_CONCURRENCY: Final = 10
MAX_DEADLINE: Final = timedelta(days=90)
MAX_AGENT_TIMEOUT: Final = timedelta(minutes=45)
MAX_CODE_TIMEOUT: Final = timedelta(minutes=60)
MAX_ATTACHMENTS: Final = 10

Id = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Duration = Annotated[str, StringConstraints(pattern=r"^(\d+[smhd])+$")]
Template = JsonValue
_DURATION_PART = re.compile(r"(\d+)([smhd])")
_UNITS: Final = {"s": 1, "m": 60, "h": 3600, "d": 86400}
#: owner, editors, admins, triggered_by, thread, person:<id>, role:<web role>, or `${{ }}`.
_APPROVER = re.compile(
    r"^(owner|editors|admins|triggered_by|thread|person:\S+|role:\S+|\$\{\{.+\}\})$"
)


def parse_duration(text: str) -> timedelta:
    """`1h30m` → 90 minutes. The `Duration` pattern guarantees the shape."""
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


class _Flow(_Base):
    next: str | list[str] | None = None
    on_error: str | None = None
    retry: Retry | None = None
    budget_usd: float | None = Field(default=None, gt=0)
    max_visits: int | None = Field(default=None, ge=1, le=MAX_VISITS)
    on_max_visits: str | None = None


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
    tool: Annotated[str, StringConstraints(pattern=r"^[^/\s]+/[^/\s]+$")]
    args: dict[str, Template] | None = None
    idempotent: bool | None = None
    timeout: Duration | None = None


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


class WaitEvent(_Model):
    source: str
    match: str | None = None
    key: str | None = None


class WaitNode(_Flow):
    kind: Literal["wait"]
    for_: Duration | None = Field(default=None, alias="for")
    until: str | None = None
    event: WaitEvent | None = None
    timeout: Duration | None = None
    routes: dict[Literal["arrived", "timed_out"], str] | None = None

    @model_validator(mode="after")
    def _one_mode(self) -> WaitNode:
        if sum(x is not None for x in (self.for_, self.until, self.event)) != 1:
            raise ValueError("A wait needs exactly one of for, until or event")
        return self


class WorkflowNode(_Flow):
    kind: Literal["workflow"]
    workflow: str
    version: Literal["live"] | Annotated[int, Field(ge=1)] | None = None
    inputs: dict[str, Template] | None = None


class Deliver(_Model):
    to: str
    message: str | None = None
    #: File refs to attach, such as `${{ steps.report.files }}` (at most 10).
    files: Template = None


class FinishNode(_Base):
    kind: Literal["finish"]
    output: Template = None
    outcome: Literal["succeeded", "failed"] | None = None
    deliver: Deliver | None = None


ItemNode = Annotated[AgentNode | CodeNode | ActionNode | WorkflowNode, Field(discriminator="kind")]


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
    "finish",
]


class ManualTrigger(_Model):
    id: Id
    kind: Literal["manual"]


class ScheduleTrigger(_Model):
    id: Id
    kind: Literal["schedule"]
    cron: str
    timezone: str | None = None
    catch_up: Literal["skip", "run-once"] | None = None
    with_: dict[str, JsonValue] | None = Field(default=None, alias="with")


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


Trigger = Annotated[
    ManualTrigger | ScheduleTrigger | WebhookTrigger | EventTrigger | CallTrigger,
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


class Spec(_Model):
    schema_: Literal["daimon.workflow/v1"] = Field(alias="schema")
    name: str = Field(min_length=1, max_length=120)
    goal: str | None = None
    home: Home | None = None
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
