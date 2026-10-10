"""Validated shared scenario contract. Invalid catalogs fail before any live action."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Self, cast, get_args

import yaml
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator

Status = Literal["PASS", "FAIL", "PENDING"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, strict=True)


class Step(Contract):
    do: Literal[
        "new_channel",
        "mention",
        "thread_reply",
        "channel_message",
        "burst",
        "react",
        "wait",
        "wait_done",
        "dm",
        "admin",
        "restart_workers",
        "headless_interrupt",
    ]
    ref: str | None = None
    channel: str | None = None
    mention: bool = False
    reply_to: str | None = Field(default=None, pattern=r"^turn[1-9][0-9]*\.chunk[1-9][0-9]*$")
    text: str | None = None
    file: str | None = None
    role: Literal["user", "admin"] = Field(default="user", alias="as")
    texts: list[str] = Field(default_factory=list)
    interval_s: float = Field(default=0, ge=0)
    target: Literal["last_answer"] | None = None
    emoji: str | None = None
    s: float | None = Field(default=None, ge=0)
    timeout_s: float = Field(default=900, gt=0)
    interrupt_after_s: float | None = Field(default=None, ge=0)
    tool: str | None = None
    args: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def required_arguments(self) -> Self:
        if self.do in {"mention", "thread_reply", "channel_message", "dm"} and self.text is None:
            raise ValueError(f"{self.do} requires text")
        if self.do == "burst" and not self.texts:
            raise ValueError("burst requires nonempty texts")
        if self.do == "react" and (self.target != "last_answer" or not self.emoji):
            raise ValueError("react requires target=last_answer and emoji")
        if self.do == "wait" and self.s is None:
            raise ValueError("wait requires s")
        if self.do == "headless_interrupt" and (not self.text or self.interrupt_after_s is None):
            raise ValueError("headless_interrupt requires text and interrupt_after_s")
        if self.do == "admin" and not self.tool:
            raise ValueError("admin requires tool")
        return self


class Assertion(Contract):
    kind: Literal[
        "reply_within_s",
        "done_within_s",
        "in_thread",
        "no_channel_post",
        "no_silent_drop",
        "text_present",
        "text_absent",
        "card_finalized",
        "reaction_present",
        "attachments",
        "log_present",
        "log_absent",
        "db_check",
        "judge",
        "interrupt_within_s",
        "same_thread",
        "progress_seen",
        "no_blank_message",
        "message_count",
        "fences_balanced",
        "footer_on_last_message",
        "thread_name",
        "channel_text_present",
        "channel_text_absent",
        "http_check",
    ]
    turn: int | None = Field(default=None, ge=1)
    url: str | None = None
    expect_status: int | None = Field(default=None, ge=100, le=599)
    expect_content_type: str | None = None
    body_absent: str | None = None
    since_turn: int | None = Field(default=None, ge=1)
    maximum: float | None = Field(default=None, alias="max", ge=0)
    minimum: int = Field(default=0, alias="min", ge=0)
    as_turn: int | None = Field(default=None, ge=1)
    within_s: float | None = Field(default=None, ge=0)
    pattern: str | None = None
    pattern_absent: str | None = None
    max_len: int | None = Field(default=None, ge=1)
    emoji: str | None = None
    name_pattern: str | None = None
    unique: bool = False
    event: str | None = None
    fields: dict[str, JsonValue] = Field(default_factory=dict)
    sql: str | None = None
    expect: JsonValue = None
    rubric: str | None = None

    @property
    def pending_extension(self) -> str | None:
        if self.kind == "reaction_present" and self.within_s is not None:
            return "timed reaction observation"
        return None

    @model_validator(mode="after")
    def required_arguments(self) -> Self:
        if (
            self.kind not in {"db_check", "interrupt_within_s", "http_check"}
            and self.turn is None
            and self.since_turn is None
        ):
            raise ValueError(f"{self.kind} requires turn")
        if (
            self.kind in {"reply_within_s", "done_within_s", "no_silent_drop", "interrupt_within_s"}
            and self.maximum is None
        ):
            raise ValueError(f"{self.kind} requires max")
        for prefix, field in (("text_", self.pattern), ("log_", self.event)):
            if (
                self.kind.startswith(prefix) or self.kind.startswith("channel_" + prefix)
            ) and not field:
                raise ValueError(f"{self.kind} requires {prefix} argument")
        if self.kind == "http_check" and (
            not self.url or not (self.expect_status or self.expect_content_type or self.body_absent)
        ):
            raise ValueError("http_check requires URL and an expectation")
        if self.kind == "same_thread" and self.as_turn is None:
            raise ValueError("same_thread requires as_turn")
        if self.kind == "progress_seen" and self.within_s is None:
            raise ValueError("progress_seen requires within_s")
        if self.kind == "reaction_present" and not self.emoji:
            raise ValueError("reaction_present requires emoji")
        if self.kind == "db_check" and not self.sql:
            raise ValueError("db_check requires sql")
        if self.kind == "judge" and not self.rubric:
            raise ValueError("judge requires rubric")
        if self.kind == "thread_name" and not (self.pattern or self.pattern_absent or self.max_len):
            raise ValueError("thread_name requires pattern, pattern_absent or max_len")
        if self.kind != "thread_name" and (
            self.pattern_absent is not None or self.max_len is not None
        ):
            raise ValueError("pattern_absent and max_len require thread_name")
        if (
            self.kind == "message_count"
            and self.maximum is not None
            and not self.maximum.is_integer()
        ):
            raise ValueError("message_count max must be an integer")
        for pattern in (self.pattern, self.pattern_absent, self.name_pattern, self.body_absent):
            if pattern:
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ValueError(f"invalid regex: {pattern!r}") from exc
        if self.maximum is not None and self.maximum < self.minimum:
            raise ValueError("max must be >= min")
        return self


class HumanCheck(Contract):
    click: str
    expect: str


class ScenarioMetadata(Contract):
    id: str = Field(pattern=r"^QA-[A-Z0-9-]+$")
    title: str = Field(min_length=1)
    friction: list[str]
    sources: list[str]
    set: Literal["A", "B"]
    surface: Literal["discord", "slack", "teams", "headless"]
    tier: Literal["canary", "full", "weekly", "manual"]
    priority: Literal["P0", "P1", "P2"]
    est_turns: int = Field(ge=0)
    notes: str = ""


class Scenario(ScenarioMetadata):
    setup: list[Step] = Field(default_factory=list[Step])
    steps: list[Step]
    assertions: list[Assertion] = Field(alias="assert")
    human: list[HumanCheck] = Field(default_factory=list[HumanCheck])
    teardown: list[Step] = Field(default_factory=list[Step])

    @model_validator(mode="after")
    def consistency(self) -> Self:
        if any(a.pending_extension for a in self.assertions):
            raise ValueError("scenario requires an unimplemented assertion extension")
        if self.set == "B" and not self.human:
            raise ValueError("set B requires human checklist")
        if (
            self.set == "A"
            and self.surface == "discord"
            and (not self.steps or not self.assertions)
        ):
            raise ValueError("automated Discord requires steps and assertions")
        if self.tier == "canary" and self.est_turns != 2:
            raise ValueError("canary must estimate exactly two turns")
        triggers = sum(
            len(s.texts)
            if s.do == "burst"
            else (
                s.do in {"mention", "thread_reply", "dm", "headless_interrupt"}
                or (s.do == "channel_message" and (s.mention or bool(s.reply_to)))
            )
            for s in [*self.setup, *self.steps, *self.teardown]
        )
        if self.tier != "weekly" and any(
            s.do == "restart_workers" for s in [*self.setup, *self.steps, *self.teardown]
        ):
            raise ValueError("restart_workers requires weekly tier")
        if any(a.turn and a.turn > triggers for a in self.assertions):
            raise ValueError("assertion references a nonexistent turn")
        return self


# Approved catalog extensions are recognized for inventory, never executed until
# a real implementation replaces this pending path.
PROPOSED_STEPS = frozenset({"pytest"})
PROPOSED_ASSERTIONS = frozenset(
    {
        "footer_cost_matches_ledger",
        "ledger_debits",
        "ledger_matches_usage",
        "answers_total",
        "threads_created",
        "chunks_gap_max_s",
        "component_present",
        "card_text_now",
        "cli_check",
        "any_of",
        "card_edits_min",
        "answer_length_chars",
        "answer_part_gap_s",
        "reaction_absent",
        "pytest_passed",
    }
)
PROPOSED_STEP_PARAMS = frozenset(
    {
        "guild",
        "during_downtime",
        "allow_fail",
    }
)
PROPOSED_ASSERT_PARAMS = frozenset({"target", "phase"})
PLACEHOLDER = re.compile(r"\{[a-z][a-z0-9_]*(?:\.[a-z0-9_]+|:[A-Za-z0-9_]+)*\}")


class ProposedScenario(ScenarioMetadata):
    setup: list[dict[str, JsonValue]] = Field(default_factory=list[dict[str, JsonValue]])
    steps: list[dict[str, JsonValue]]
    assertions: list[dict[str, JsonValue]] = Field(alias="assert")
    human: list[HumanCheck] = Field(default_factory=list[HumanCheck])
    teardown: list[dict[str, JsonValue]] = Field(default_factory=list[dict[str, JsonValue]])
    unsupported: list[str] = Field(default_factory=list[str], exclude=True)

    @model_validator(mode="after")
    def declared_proposals(self) -> Self:
        features: set[str] = set()
        if self.tier == "canary" and self.est_turns != 2:
            raise ValueError("canary must estimate exactly two turns")
        if self.set == "B" and not self.human:
            raise ValueError("set B requires human checklist")
        if (
            self.set == "A"
            and self.surface == "discord"
            and (not self.steps or not self.assertions)
        ):
            raise ValueError("automated Discord requires steps and assertions")
        for step in [*self.setup, *self.steps, *self.teardown]:
            kind = step.get("do")
            if isinstance(kind, str) and kind in PROPOSED_STEPS:
                features.add(f"step {kind}")
                continue
            if isinstance(kind, str) and kind not in get_args(Step.model_fields["do"].annotation):
                if self.tier == "canary":
                    raise ValueError(f"unknown step kind in canary: {kind}")
                features.add(f"unknown step {kind}")
                continue
            normalized = dict(step)
            for param in PROPOSED_STEP_PARAMS.intersection(step):
                features.add(f"step parameter {param}")
                normalized.pop(param)
            if kind == "admin" and isinstance(step.get("args"), str):
                features.add("string admin arguments")
                normalized["args"] = {}
            Step.model_validate(normalized)
        for assertion in self.assertions:
            kind = assertion.get("kind")
            if isinstance(kind, str) and kind in PROPOSED_ASSERTIONS:
                features.add(f"assertion {kind}")
                continue
            if isinstance(kind, str) and kind not in get_args(
                Assertion.model_fields["kind"].annotation
            ):
                if self.tier == "canary":
                    raise ValueError(f"unknown assertion kind in canary: {kind}")
                features.add(f"unknown assertion {kind}")
                continue
            normalized = dict(assertion)
            for param in PROPOSED_ASSERT_PARAMS.intersection(assertion):
                features.add(f"assertion parameter {param}")
                normalized.pop(param)
            if assertion.get("turn") == 0:
                features.add("whole-run turn 0")
                normalized["turn"] = 1
            supported = Assertion.model_validate(normalized)
            if supported.pending_extension:
                features.add(supported.pending_extension)
        if PLACEHOLDER.search(str([self.setup, self.steps, self.assertions, self.teardown])):
            features.add("catalog fixture/context placeholders")
        if not features:
            raise ValueError("invalid scenario has no declared unimplemented proposal")
        self.unsupported = sorted(features)
        return self


CatalogScenario = Scenario | ProposedScenario


def load_catalog(directory: Path) -> list[CatalogScenario]:
    root = directory / "scenarios" if (directory / "scenarios").is_dir() else directory
    paths = sorted([*root.glob("*.yaml"), *root.glob("*.yml")])
    if not paths:
        raise ValueError(f"no scenarios in {root}")
    scenarios: list[CatalogScenario] = []
    for path in paths:
        try:
            document = cast(JsonValue, yaml.safe_load(path.read_text()))
            if not isinstance(document, dict):
                raise ValueError("scenario document must be a mapping")
            data = document
            for section in ("setup", "teardown"):
                actions = data.get(section)
                if isinstance(actions, list):
                    data[section] = [
                        {"do": "admin", **step}
                        if isinstance(step, dict) and "do" not in step and "tool" in step
                        else step
                        for step in actions
                    ]
            try:
                scenario: CatalogScenario = Scenario.model_validate(data)
            except ValidationError:
                scenario = ProposedScenario.model_validate(data)
        except (ValueError, yaml.YAMLError) as exc:
            raise ValueError(f"invalid scenario {path.name}: {exc}") from exc
        if any(s.id == scenario.id for s in scenarios):
            raise ValueError(f"duplicate scenario id: {scenario.id}")
        if isinstance(scenario, Scenario):
            for step in [*scenario.setup, *scenario.steps, *scenario.teardown]:
                if step.file:
                    asset = Path(step.file).expanduser()
                    if not asset.is_absolute():
                        base = root.parent if root.name == "scenarios" else directory
                        asset = base / asset
                    if not asset.is_file():
                        raise ValueError(f"scenario {scenario.id}: attachment not found: {asset}")
                    step.file = str(asset.resolve())
        scenarios.append(scenario)
    return scenarios
