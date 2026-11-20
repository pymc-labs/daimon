"""The eval bundle format 1.0 as Pydantic models, and the canonical JSON its hashes use.

`schema/bundle-1.0.schema.json` is the contract; these models mirror it field for field so
code reads documents with types. Stored and exported documents stay the JSON they arrived
as (`JsonObject`); a model is a view parsed from one, never the copy that is hashed.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

JsonObject = dict[str, JsonValue]
_OBJECT: Final = TypeAdapter(dict[str, JsonValue])
FORMAT_VERSION: Final = "1.0"
MANIFEST_SCHEMA: Final = "daimon.eval.bundle/1"
SUITE_SCHEMA: Final = "daimon.eval.suite/1"
CASE_SCHEMA: Final = "daimon.eval.case/1"
RUN_SCHEMA: Final = "daimon.eval.run/1"
ITEM_SCHEMA: Final = "daimon.eval.item/1"
SCORE_SCHEMA: Final = "daimon.eval.score/1"
REVIEW_SCHEMA: Final = "daimon.eval.review/1"

Platform = Literal["discord", "slack", "teams", "web"]
Backend = str
"""A model backend or `harness`; the schema lists the accepted names."""
ScrubStatus = Literal["scrubbed", "partial", "unscrubbed"]
Origin = Literal["recorded", "overlay", "simulated", "denied", "passthrough", "sandbox", "live"]
CardKind = Literal[
    "confirmation", "posted", "credential_request", "host_call", "provider_action",
    "refusal", "status", "panel", "form", "question",
]  # fmt: skip
ButtonAction = Literal[
    "approve", "deny", "always", "details", "save", "skip", "submit", "stop", "open_url", "custom"
]
MatchMode = Literal["strict", "unordered", "subset", "superset"]


class FormatModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, validate_by_name=True, validate_by_alias=True,
        serialize_by_alias=True,
    )  # fmt: skip


class BlobRef(FormatModel):
    sha256: str
    size: int
    media_type: str
    name: str | None = None


class Body(FormatModel):
    text: str | None = None
    json_: JsonValue = Field(default=None, alias="json")
    blob: BlobRef | None = None
    media_type: str | None = None


class PrincipalRef(FormatModel):
    kind: Literal["person", "group", "agent", "label"]
    id: str
    display_name: str | None = None


class Tool(FormatModel):
    name: str
    version: str


# ---------- Manifest ----------


class BlobEntry(FormatModel):
    sha256: str
    size: int
    media_type: str
    scrub: Literal["rebuilt", "redacted", "clean", "generated", "unscrubbed"]
    needs_human_check: bool = False


class SourceInfo(FormatModel):
    kind: Literal["deployment", "converter"]
    label: str | None = None
    instance: str | None = None
    daimon_version: str | None = None
    converter: Tool | None = None


class SuiteEntry(FormatModel):
    id: str
    version: int
    path: str
    cases_path: str
    case_count: int


class RunEntry(FormatModel):
    id: str
    path: str
    items_path: str
    scores_path: str | None = None
    reviews_path: str | None = None
    item_count: int


class BundleContents(FormatModel):
    suites: list[SuiteEntry] = []
    runs: list[RunEntry] = []


class FileEntry(FormatModel):
    path: str
    sha256: str
    size: int


class BundleScrub(FormatModel):
    status: ScrubStatus
    policy: str | None = None
    unchecked_blobs: int | None = None


class Signature(FormatModel):
    alg: Literal["ed25519"] = "ed25519"
    key_id: str
    value: str


class Manifest(FormatModel):
    schema_: Literal["daimon.eval.bundle/1"] = Field(default=MANIFEST_SCHEMA, alias="schema")
    format: Literal["daimon-eval-bundle"] = "daimon-eval-bundle"
    format_version: str = FORMAT_VERSION
    id: str
    title: str | None = None
    created_at: datetime
    created_by: PrincipalRef | None = None
    source: SourceInfo
    contents: BundleContents
    files: list[FileEntry]
    blobs: list[BlobEntry]
    scrub: BundleScrub
    includes_identities: bool = False
    signature: Signature | None = None
    ext: JsonObject | None = None


# ---------- Suite ----------


class CallPattern(FormatModel):
    effect: str | None = None
    tool: str | None = None
    server: str | None = None
    service: str | None = None
    surface: Literal["mcp", "http", "cli", "builtin", "provider_tool"] | None = None


class ToolTarget(FormatModel):
    server: str | None = None
    tool: str


class ToolAlias(FormatModel):
    from_: CallPattern = Field(alias="from")
    to_tool: ToolTarget | None = None
    to_effect: str | None = None
    args_map: dict[str, str] | None = None
    """Source JSON Pointer in the recorded arguments to its pointer in the canonical call."""


class ModelChoice(FormatModel):
    backend: Backend | None = None
    model: str
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None


JudgeSees = Literal["reply", "chat", "files", "tool_calls", "world_calls", "reference", "history"]


class JudgeConfig(FormatModel):
    id: str
    kind: Literal["rubric", "pairwise", "reference"]
    models: list[ModelChoice]
    aggregate: Literal["majority", "mean", "all"] = "majority"
    swap_positions: bool = True
    sees: list[JudgeSees] = ["reply", "chat", "files", "history"]
    instructions: str | None = None


class NetworkPolicy(FormatModel):
    package_registries: Literal["passthrough", "deny"] | None = None
    passthrough: list[str] | None = None
    deny: list[str] | None = None


class ReviewDefaults(FormatModel):
    layout: Literal["single", "side_by_side"] = "side_by_side"
    blind: bool = True
    reviewers_per_item: int = 1


class StatsDefaults(FormatModel):
    primary: Literal["pass_rate", "rubric_score", "preference"] = "pass_rate"
    non_inferiority_margin: float = 0.05
    cluster_by: Literal["case", "source_thread"] = "source_thread"


class SuiteDefaults(FormatModel):
    repeats: int = 1
    judges: list[JudgeConfig] | None = None
    simulator: ModelChoice | None = None
    network: NetworkPolicy | None = None
    clock: Literal["case", "real"] = "case"
    review: ReviewDefaults | None = None
    stats: StatsDefaults | None = None


class SuiteCaseRef(FormatModel):
    id: str
    revision: int
    hash: str
    weight: float = 1
    split: Literal["dev", "test", "holdout"] = "test"


class Suite(FormatModel):
    schema_: Literal["daimon.eval.suite/1"] = Field(default=SUITE_SCHEMA, alias="schema")
    id: str
    version: int
    name: str
    description: str | None = None
    tags: list[str] | None = None
    owner: PrincipalRef | None = None
    created_at: datetime | None = None
    cases: list[SuiteCaseRef]
    tool_aliases: list[ToolAlias] | None = None
    defaults: SuiteDefaults | None = None
    ext: JsonObject | None = None


# ---------- Case ----------


class Fidelity(FormatModel):
    level: Literal["full", "tools", "text", "authored"]
    missing: list[str] | None = None
    note: str | None = None


class ScrubRecord(FormatModel):
    status: ScrubStatus
    policy: str | None = None
    at: datetime | None = None
    entities: int | None = None
    human_checked: bool = False


class ExpectationsBy(FormatModel):
    kind: Literal["model", "person", "agent"]
    who: PrincipalRef | None = None
    model: str | None = None
    at: datetime
    action: Literal["drafted", "checked", "edited"] | None = None


class Provenance(FormatModel):
    kind: Literal["recorded", "authored", "derived", "imported"]
    source_system: str | None = None
    source_ref: str | None = None
    recorded_at: datetime | None = None
    converter: Tool | None = None
    derived_from: list[str] | None = None
    scrub: ScrubRecord | None = None
    expectations_by: list[ExpectationsBy] | None = None


class Clock(FormatModel):
    now: datetime
    timezone: str | None = None


class Place(FormatModel):
    id: str
    name: str
    topic: str | None = None
    private: bool | None = None
    parent_id: str | None = None


class Participant(FormatModel):
    id: str
    display_name: str
    handle: str | None = None
    email: str | None = None
    role: Literal["owner", "admin", "member", "visitor", "guest", "external"] = "member"
    groups: list[str] | None = None
    is_requester: bool = False
    is_bot: bool = False
    timezone: str | None = None


class Group(FormatModel):
    id: str
    name: str
    kind: Literal["discord_role", "slack_user_group", "teams_team", "web_group"]


class Surface(FormatModel):
    platform: Platform
    conversation: Literal["dm", "group_dm", "channel", "thread"]
    workspace: Place | None = None
    channel: Place | None = None
    thread: Place | None = None
    participants: list[Participant]
    groups: list[Group] | None = None
    bot: Participant


class Attachment(FormatModel):
    id: str
    name: str
    media_type: str
    size: int | None = None
    blob: BlobRef | None = None
    state: Literal["present", "rebuilt", "redacted", "missing"] = "present"


class Reaction(FormatModel):
    emoji: str
    by: list[str]


class CardFact(FormatModel):
    label: str
    value: str


class CardField(FormatModel):
    id: str
    label: str
    kind: Literal["text", "textarea", "select", "secret", "checkbox"]
    required: bool | None = None
    options: list[str] | None = None
    placeholder: str | None = None


class CardButton(FormatModel):
    id: str
    label: str
    style: Literal["primary", "secondary", "danger", "link"] = "secondary"
    action: ButtonAction
    url: str | None = None


class CardAnswer(FormatModel):
    by: str | None = None
    button: str | None = None
    fields: dict[str, str] | None = None


class NeutralCard(FormatModel):
    id: str
    kind: CardKind
    headline: str | None = None
    body: str | None = None
    facts: list[CardFact] | None = None
    fields: list[CardField] | None = None
    buttons: list[CardButton] | None = None
    footer: str | None = None
    state: Literal["open", "answered", "expired", "cancelled"] = "open"
    answer: CardAnswer | None = None


class ChatMessage(FormatModel):
    id: str
    author: str
    at: datetime
    text: str | None = None
    format: Literal["markdown", "plain"] = "markdown"
    reply_to: str | None = None
    thread_id: str | None = None
    mentions: list[str] | None = None
    attachments: list[Attachment] | None = None
    reactions: list[Reaction] | None = None
    cards: list[NeutralCard] | None = None
    edited_at: datetime | None = None
    deleted: bool | None = None
    kind: Literal["message", "system", "notice"] = "message"


class Turn(FormatModel):
    messages: list[ChatMessage]
    trigger: Literal["mention", "dm", "reply", "command", "auto"] = "mention"


class Trigger(FormatModel):
    event: Literal[
        "card",
        "message",
        "reaction",
        "form",
        "credential_request",
        "tool_call",
        "turn_end",
        "start",
    ]
    card_kind: CardKind | None = None
    button_action: ButtonAction | None = None
    text_contains: str | None = None
    text_regex: str | None = None
    tool: CallPattern | None = None
    nth: int = 1


class ButtonChoice(FormatModel):
    action: ButtonAction | None = None
    label_regex: str | None = None
    id: str | None = None


class ScriptAction(FormatModel):
    action: Literal[
        "click", "react", "unreact", "submit_form", "reply", "edit", "delete", "stop", "wait"
    ]
    as_: str | None = Field(default=None, alias="as")
    target: str | None = None
    button: ButtonChoice | None = None
    emoji: str | None = None
    fields: dict[str, str] | None = None
    text: str | None = None
    attachments: list[Attachment] | None = None
    ms: int | None = None


class ScriptStep(FormatModel):
    id: str
    on: Trigger
    do: list[ScriptAction]
    once: bool = True
    delay_ms: int = 0


class ScriptDefaults(FormatModel):
    confirmation: Literal["approve", "deny", "always", "ignore"] = "approve"
    credential_request: Literal["skip", "submit_fake", "ignore"] = "skip"
    form: Literal["skip", "ignore"] = "ignore"
    question: Literal["end", "simulate"] = "end"


class Script(FormatModel):
    steps: list[ScriptStep] | None = None
    defaults: ScriptDefaults | None = None
    max_follow_ups: int = 0


# ---------- World ----------


class HttpRequest(FormatModel):
    method: str
    url: str
    headers: dict[str, str] | None = None
    body: Body | None = None


class CliRequest(FormatModel):
    argv: list[str]
    shell: str | None = None
    stdin: Body | None = None
    cwd: str | None = None


class ExchangeRequest(FormatModel):
    args: JsonValue = None
    http: HttpRequest | None = None
    cli: CliRequest | None = None


class ContentBlock(FormatModel):
    type: Literal["text", "image", "resource", "file"]
    text: str | None = None
    blob: BlobRef | None = None
    uri: str | None = None


class HttpResponse(FormatModel):
    status: int
    headers: dict[str, str] | None = None
    body: Body | None = None


class CliResponse(FormatModel):
    exit_code: int
    stdout: Body | None = None
    stderr: Body | None = None


class ExchangeResponse(FormatModel):
    result: JsonValue = None
    content: list[ContentBlock] | None = None
    is_error: bool = False
    http: HttpResponse | None = None
    cli: CliResponse | None = None
    latency_ms: int | None = None


class Match(FormatModel):
    mode: Literal["exact", "normalized", "keys", "semantic"] = "normalized"
    keys: list[str] | None = None
    ignore: list[str] | None = None


class Effect(FormatModel):
    verb: str
    target: str | None = None
    args: JsonObject | None = None


class Exchange(FormatModel):
    id: str
    surface: Literal["mcp", "http", "cli", "provider_tool"]
    service: str | None = None
    server: str | None = None
    tool: str | None = None
    request: ExchangeRequest
    response: ExchangeResponse
    match: Match | None = None
    effects: list[Effect] | None = None
    kind: Literal["read", "write"] = "read"
    origin: Literal["recorded", "derived", "authored"] = "recorded"
    verified: bool | None = None
    derived_from: str | None = None
    order: int | None = None
    repeatable: bool = True


class Resource(FormatModel):
    service: str
    type: str
    key: str
    data: JsonObject
    blob: BlobRef | None = None


class WorldFile(FormatModel):
    path: str
    blob: BlobRef
    mode: str | None = None
    state: Literal["present", "rebuilt", "redacted"] = "present"


class Repo(FormatModel):
    url: str
    default_branch: str
    bundle: BlobRef
    refs: dict[str, str] | None = None
    private: bool = True
    writable: bool = True
    cloned_at: str | None = None


class WebPage(FormatModel):
    url: str
    status: int
    title: str | None = None
    body: Body | None = None
    fetched_at: datetime | None = None


class SearchResult(FormatModel):
    title: str
    url: str
    snippet: str | None = None


class SearchSet(FormatModel):
    query: str
    match: Literal["exact", "semantic"] = "semantic"
    results: list[SearchResult]


class McpTool(FormatModel):
    name: str
    input_schema: JsonObject
    output_schema: JsonObject | None = None
    annotations: JsonObject | None = None


class McpServer(FormatModel):
    name: str
    kind: Literal["daimon", "third_party", "relay"]
    instructions: str | None = None
    tools: list[McpTool] | None = None


class Identity(FormatModel):
    service: str
    login: str
    display_name: str | None = None
    email: str | None = None
    token_env: str | None = None
    token_shape: str | None = None
    scopes: list[str] | None = None


class WorldEnv(FormatModel):
    vars: dict[str, str] | None = None
    workdir: str | None = None


class SimulatorSettings(FormatModel):
    enabled: bool = True
    instructions: str | None = None
    max_calls: int = 50


class SimEntry(FormatModel):
    key: str
    surface: Literal["mcp", "http", "provider_tool"]
    request: ExchangeRequest
    response: ExchangeResponse
    effects: list[Effect] | None = None
    model: str
    created_at: datetime


class World(FormatModel):
    exchanges: list[Exchange] | None = None
    resources: list[Resource] | None = None
    files: list[WorldFile] | None = None
    repos: list[Repo] | None = None
    web: list[WebPage] | None = None
    search: list[SearchSet] | None = None
    mcp_servers: list[McpServer] | None = None
    identities: list[Identity] | None = None
    env: WorldEnv | None = None
    network: NetworkPolicy | None = None
    simulator: SimulatorSettings | None = None
    sim_cache: list[SimEntry] | None = None


class MemoryEntry(FormatModel):
    path: str
    content: str
    scope: Literal["agent", "person", "channel", "repo"] | None = None


class ContextEntry(FormatModel):
    provider: str
    text: str


class AgentContext(FormatModel):
    memory: list[MemoryEntry] | None = None
    context: list[ContextEntry] | None = None
    previous_session: str | None = None


# ---------- Expectations ----------


class Check(FormatModel):
    op: Literal[
        "exact", "normalized", "regex", "contains", "not_contains", "one_of", "range", "schema",
        "exists", "llm",
    ]  # fmt: skip
    value: JsonValue = None
    values: list[JsonValue] | None = None
    pattern: str | None = None
    min: float | None = None
    max: float | None = None
    schema_: JsonObject | None = Field(default=None, alias="schema")
    criterion: str | None = None


class ExpectedCall(FormatModel):
    match: CallPattern
    args: dict[str, Check] | None = None
    min: int = 1
    max: int | None = None
    hard: bool = True


class ExpectedCalls(FormatModel):
    mode: MatchMode
    calls: list[ExpectedCall]


class ExpectedEffect(FormatModel):
    verb: str
    target: str | None = None
    args: dict[str, Check] | None = None
    min: int = 1
    max: int | None = None
    hard: bool = True


class ExpectedEffects(FormatModel):
    mode: MatchMode
    items: list[ExpectedEffect]


class PageRange(FormatModel):
    min: int | None = None
    max: int | None = None


class ExpectedOutput(FormatModel):
    kind: Literal["reply", "message", "file", "card", "reaction", "no_reply"]
    where: Literal["final", "any", "first"] = "any"
    text: Check | None = None
    media_type: str | None = None
    name: Check | None = None
    card_kind: CardKind | None = None
    emoji: str | None = None
    pages: PageRange | None = None
    min: int = 1
    max: int | None = None
    hard: bool = True


class WorldCheck(FormatModel):
    service: str
    key: str
    path: str
    check: Check
    hard: bool = True


class RubricScale(FormatModel):
    min: int
    max: int
    anchors: dict[str, str] | None = None


class RubricItem(FormatModel):
    id: str
    criterion: str
    kind: Literal["binary", "scale"] = "binary"
    scale: RubricScale | None = None
    weight: float = 1
    hard: bool = False
    applies_to: list[Literal["reply", "chat", "files", "tool_calls", "world"]] = [
        "reply", "chat", "files",
    ]  # fmt: skip
    graders: list[Literal["llm", "human", "agent"]] = ["llm"]


class Reference(FormatModel):
    text: str | None = None
    files: list[BlobRef] | None = None
    note: str | None = None


class ScoredLimits(FormatModel):
    max_tool_calls: int | None = None
    max_duration_s: float | None = None
    max_cost_usd: float | None = None
    max_messages: int | None = None


class PassRule(FormatModel):
    checks: Literal["all", "hard_only"] = "all"
    rubric_min: float = 0.7


class Expectations(FormatModel):
    tool_calls: ExpectedCalls | None = None
    forbidden_calls: list[CallPattern] | None = None
    effects: ExpectedEffects | None = None
    forbidden_effects: list[str] | None = None
    outputs: list[ExpectedOutput] | None = None
    world: list[WorldCheck] | None = None
    rubric: list[RubricItem] | None = None
    reference: Reference | None = None
    limits: ScoredLimits | None = None
    pass_rule: PassRule | None = None


class Limits(FormatModel):
    timeout_s: int = 1800
    max_world_calls: int = 300
    max_cost_usd: float | None = None


ReviewFlagReason = Literal[
    "needs_human_check", "scrub_uncertain", "image_redacted", "file_rebuilt",
    "expectations_unreviewed", "world_incomplete",
]  # fmt: skip


class ReviewFlag(FormatModel):
    reason: ReviewFlagReason
    detail: str | None = None
    cleared_by: PrincipalRef | None = None
    cleared_at: datetime | None = None


class Case(FormatModel):
    schema_: Literal["daimon.eval.case/1"] = Field(default=CASE_SCHEMA, alias="schema")
    id: str
    revision: int
    hash: str | None = None
    title: str
    tags: list[str] | None = None
    source_thread: str | None = None
    source_turn: int | None = None
    dedupe_group: str | None = None
    fidelity: Fidelity
    provenance: Provenance
    surface: Surface
    clock: Clock
    history: list[ChatMessage]
    turn: Turn
    script: Script | None = None
    world: World
    agent_context: AgentContext | None = None
    expect: Expectations
    limits: Limits | None = None
    review_flags: list[ReviewFlag] | None = None
    notes: str | None = None
    ext: JsonObject | None = None


# ---------- Runs ----------


class LiveArm(FormatModel):
    agent: str | None = None
    agent_snapshot: BlobRef | None = None
    backend: Backend
    model: str
    effort: str | None = None
    overrides: JsonObject | None = None


class BaselineArm(FormatModel):
    source_system: str | None = None
    recorded_from: datetime | None = None
    recorded_to: datetime | None = None


class Arm(FormatModel):
    id: str
    label: str
    kind: Literal["live", "baseline"]
    live: LiveArm | None = None
    baseline: BaselineArm | None = None
    config_hash: str | None = None


class SuitePin(FormatModel):
    id: str
    version: int


class RunOrigin(FormatModel):
    kind: Literal["local", "imported", "recorded"]
    deployment: str | None = None
    daimon_version: str | None = None


class EstimateLine(FormatModel):
    category: Literal["agent", "simulator", "judge", "compile", "sandbox"]
    arm_id: str | None = None
    model: str | None = None
    items: int | None = None
    expected_usd: float
    basis: Literal["prior_runs", "baseline_usage", "suite_median", "default"] | None = None


class Estimate(FormatModel):
    low_usd: float
    expected_usd: float
    high_usd: float
    at: datetime
    lines: list[EstimateLine]


class Approval(FormatModel):
    by: PrincipalRef
    at: datetime
    cap_usd: float


class Spend(FormatModel):
    total_usd: float | None = None
    by_category: dict[str, float] | None = None


RunStatus = Literal[
    "draft", "estimated", "approved", "running", "done", "partial", "cancelled", "failed"
]


class Run(FormatModel):
    schema_: Literal["daimon.eval.run/1"] = Field(default=RUN_SCHEMA, alias="schema")
    id: str
    name: str | None = None
    suite: SuitePin
    arms: list[Arm]
    case_ids: list[str] | None = None
    repeats: int = 1
    judges: list[JudgeConfig] | None = None
    status: RunStatus
    origin: RunOrigin
    estimate: Estimate | None = None
    approval: Approval | None = None
    spend: Spend | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    blind_labels: dict[str, str] | None = None
    ext: JsonObject | None = None


class Usage(FormatModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    tool_calls: int | None = None


class TranscriptTool(FormatModel):
    name: str
    server: str | None = None
    surface: Literal["mcp", "builtin", "provider_tool", "custom"] | None = None
    input: JsonValue = None


class TranscriptResult(FormatModel):
    content: list[ContentBlock] | None = None
    is_error: bool | None = None
    origin: Origin | None = None
    world_call: int | None = None


class TranscriptEvent(FormatModel):
    seq: int
    t_ms: int | None = None
    turn: int | None = None
    kind: Literal[
        "user_message", "assistant_text", "thinking", "tool_call", "tool_result", "status",
        "error", "turn_start", "turn_end",
    ]  # fmt: skip
    text: str | None = None
    redacted: bool | None = None
    call_id: str | None = None
    tool: TranscriptTool | None = None
    result: TranscriptResult | None = None
    model: str | None = None
    usage: Usage | None = None


class EventStream(FormatModel):
    events: list[TranscriptEvent] | None = None
    blob: BlobRef | None = None


class WorldCall(FormatModel):
    seq: int
    t_ms: int | None = None
    surface: Literal["mcp", "http", "provider_tool", "git"]
    service: str | None = None
    server: str | None = None
    tool: str | None = None
    request: ExchangeRequest | None = None
    response: ExchangeResponse | None = None
    origin: Origin
    exchange_id: str | None = None
    sim_key: str | None = None
    effects: list[Effect] | None = None
    reason: str | None = None


class PlatformPayloads(FormatModel):
    discord: JsonObject | None = None
    slack: JsonObject | None = None
    teams: JsonObject | None = None
    web: JsonObject | None = None


class ChatEvent(FormatModel):
    seq: int
    t_ms: int | None = None
    kind: Literal[
        "message", "edit", "delete", "reaction_add", "reaction_remove", "card", "card_update",
        "typing", "status", "title", "notice", "file",
    ]  # fmt: skip
    actor: str
    message_id: str | None = None
    text: str | None = None
    format: Literal["markdown", "plain"] | None = None
    reply_to: str | None = None
    emoji: str | None = None
    card: NeutralCard | None = None
    files: list[str] | None = None
    scripted: str | None = None
    platform_payloads: PlatformPayloads | None = None


class Output(FormatModel):
    id: str
    name: str
    media_type: str
    blob: BlobRef
    source: Literal["chat_upload", "sandbox", "artifact", "world"] | None = None
    path: str | None = None
    renders: list[BlobRef] | None = None
    text: BlobRef | None = None


class SandboxInfo(FormatModel):
    runtime: Literal["docker", "kubernetes"] | None = None
    image: str | None = None
    parity: Literal["matched", "drift", "unchecked"] | None = None


class EnclosureNetwork(FormatModel):
    intercept: Literal["transparent"] | None = None
    control_plane: list[str] | None = None
    package_registries: Literal["passthrough", "deny"] | None = None
    passthrough: list[str] | None = None


class EnclosureClock(FormatModel):
    mode: Literal["case", "real"] | None = None
    real_time_binaries: list[str] | None = None


class Substitution(FormatModel):
    from_: str = Field(alias="from")
    to: str


class Enclosure(FormatModel):
    mode: Literal["enclosed", "recorded"]
    backend: Backend | None = None
    sandbox: SandboxInfo | None = None
    network: EnclosureNetwork | None = None
    clock: EnclosureClock | None = None
    substitutions: list[Substitution] | None = None
    probes: dict[str, Literal["pass", "fail", "skipped"]] | None = None
    simulated_calls: int | None = None
    denied_calls: int | None = None
    daimon_version: str | None = None


class ItemCaseRef(FormatModel):
    id: str
    revision: int
    hash: str


class ItemError(FormatModel):
    kind: Literal["timeout", "cap", "provider", "sandbox", "world", "agent", "cancelled", "other"]
    message: str | None = None


ItemStatus = Literal["pending", "running", "done", "error", "skipped", "cancelled"]


class RunItem(FormatModel):
    schema_: Literal["daimon.eval.item/1"] = Field(default=ITEM_SCHEMA, alias="schema")
    id: str
    run_id: str
    case: ItemCaseRef
    arm_id: str
    repeat: int
    status: ItemStatus
    error: ItemError | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    transcript: EventStream | None = None
    chat: list[ChatEvent] | None = None
    outputs: list[Output] | None = None
    world_calls: list[WorldCall] | None = None
    usage: Usage | None = None
    enclosure: Enclosure | None = None
    ext: JsonObject | None = None


# ---------- Scores and reviews ----------


class Region(FormatModel):
    x: float
    y: float
    w: float
    h: float


class Anchor(FormatModel):
    target: Literal[
        "message", "tool_call", "world_call", "output", "page", "image_region", "table_range",
        "text_range", "card_element", "diff_hunk",
    ]  # fmt: skip
    item_id: str
    ref: str | None = None
    page: int | None = None
    region: Region | None = None
    sheet: str | None = None
    cells: str | None = None
    start: int | None = None
    end: int | None = None
    quote: str | None = None
    element: str | None = None


class ItemPair(FormatModel):
    a: str
    b: str


class Grader(FormatModel):
    kind: Literal["code", "llm", "human", "agent"]
    who: PrincipalRef | None = None
    model: str | None = None
    judge_id: str | None = None
    version: str | None = None


class ScoreValue(FormatModel):
    pass_: bool | None = Field(default=None, alias="pass")
    score: float | None = None
    preference: Literal["a", "b", "tie"] | None = None
    label: str | None = None
    unknown: bool | None = None


class Score(FormatModel):
    schema_: Literal["daimon.eval.score/1"] = Field(default=SCORE_SCHEMA, alias="schema")
    id: str
    item_id: str | None = None
    pair: ItemPair | None = None
    criterion: str
    grader: Grader
    value: ScoreValue
    rationale: str | None = None
    anchors: list[Anchor] | None = None
    blind: bool | None = None
    swapped: bool | None = None
    cost_usd: float | None = None
    at: datetime
    supersedes: str | None = None


class ReviewQueue(FormatModel):
    schema_: Literal["daimon.eval.review/1"] = Field(default=REVIEW_SCHEMA, alias="schema")
    kind: Literal["queue"] = "queue"
    id: str
    name: str
    layout: Literal["single", "side_by_side"]
    blind: bool
    arms: list[str] | None = None
    criteria: list[str] | None = None
    reviewers_per_item: int | None = None
    reviewers: list[PrincipalRef] | None = None
    status: Literal["open", "closed"] | None = None


class Comment(FormatModel):
    schema_: Literal["daimon.eval.review/1"] = Field(default=REVIEW_SCHEMA, alias="schema")
    kind: Literal["comment"] = "comment"
    id: str
    item_id: str | None = None
    case_id: str | None = None
    parent_id: str | None = None
    author: PrincipalRef
    text: str
    anchors: list[Anchor] | None = None
    resolved: bool = False
    at: datetime


# ---------- Canonical JSON (RFC 8785) ----------


def _number(value: float) -> str:
    """ECMAScript `Number.prototype.toString`, which JCS prescribes for every number."""
    if not math.isfinite(value):
        raise ValueError("JSON has no NaN or Infinity")
    if value == 0:
        return "0"
    # repr gives the shortest digits that round-trip, as ECMAScript requires.
    _, digit_tuple, exponent = Decimal(repr(abs(value))).normalize().as_tuple()
    digits = "".join(map(str, digit_tuple))
    point = int(exponent) + len(digits)  # value = 0.digits * 10**point
    sign = "-" if value < 0 else ""
    if len(digits) <= point <= 21:
        return sign + digits + "0" * (point - len(digits))
    if 0 < point <= 21:
        return f"{sign}{digits[:point]}.{digits[point:]}"
    if -6 < point <= 0:
        return f"{sign}0.{'0' * -point}{digits}"
    tail = f".{digits[1:]}" if len(digits) > 1 else ""
    return f"{sign}{digits[0]}{tail}e{'+' if point > 0 else '-'}{abs(point - 1)}"


def _canonical(value: JsonValue, out: list[str]) -> None:
    if isinstance(value, dict):
        out.append("{")
        for index, key in enumerate(sorted(value, key=lambda k: k.encode("utf-16-be"))):
            if index:
                out.append(",")
            out.append(json.dumps(key, ensure_ascii=False))
            out.append(":")
            _canonical(value[key], out)
        out.append("}")
    elif isinstance(value, list):
        out.append("[")
        for index, element in enumerate(value):
            if index:
                out.append(",")
            _canonical(element, out)
        out.append("]")
    elif isinstance(value, bool) or value is None or isinstance(value, str):
        out.append(json.dumps(value, ensure_ascii=False))
    elif isinstance(value, int):
        out.append(str(value))
    else:
        out.append(_number(value))


def canonical_json(value: JsonValue) -> bytes:
    """RFC 8785 (JCS) bytes of `value`: sorted keys, no whitespace, ECMAScript numbers."""
    out: list[str] = []
    _canonical(value, out)
    return "".join(out).encode()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def case_hash(case: Mapping[str, JsonValue]) -> str:
    """A case's `hash`: sha256 of its canonical form without the `hash` field."""
    return sha256_hex(canonical_json({k: v for k, v in case.items() if k != "hash"}))


def to_document(model: BaseModel) -> JsonObject:
    """A model as the JSON document it stands for, unset optional fields left out."""
    return _OBJECT.validate_python(model.model_dump(mode="json", exclude_none=True))
