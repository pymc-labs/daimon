"""Workflow cards in chat: the draft card, the run confirmation, the run card and the panel.

The words and the rules live here once. Each adapter draws a `PostedCard` with its own
renderer and hands every press back to `press`, which makes the same service call the web
app makes, as the person who pressed, so the web's rights and refusals apply unchanged. A
run started from chat names its card in `triggered_by["card"]`; `follow_cards` edits that
card as the run moves, at most once every `EDIT_EVERY`.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

import structlog
from daimon.core.config import Settings
from daimon.core.posted_controls import CardButton, PostedCard
from daimon.core.stores import workflow_runs as runs_store
from daimon.core.stores import workflows as workflows_store
from daimon.core.stores.identity import get_or_create_platform_principal
from daimon.core.web_links import WebLinks
from daimon.core.workflows import run_views, service, views
from daimon.core.workflows.access import who_of
from daimon.core.workflows.runs import Sessions
from daimon.core.workflows.service import Env, Patch, WorkflowError
from daimon.core.workflows.views import obj, seq

log = structlog.get_logger(__name__)

#: A card button: `wfc:<workflow or run id>:<action>[:<revision or digest, hex>]`.
CUSTOM_ID_PATTERN: Final[str] = (
    r"wfc:(?P<id>[0-9a-f-]{36}):(?P<action>test|publish|go|cancel|run|pause|resume)"
    r"(?::(?P<arg>[0-9a-f]{1,16}))?"
)
_CUSTOM_ID = re.compile(CUSTOM_ID_PATTERN)
#: The panel's workflow picker, and the run form's submit (`wff:<id>:<t|l>`).
PICK_ID: Final[str] = "wfl:pick"
FORM_PATTERN: Final[str] = r"wff:(?P<id>[0-9a-f-]{36}):(?P<mode>[tl])"
_FORM = re.compile(FORM_PATTERN)
CardAction = Literal["test", "publish", "go", "cancel", "run", "pause", "resume"]

EDIT_EVERY: Final[timedelta] = timedelta(seconds=3)
#: How long after a run ends its card is still brought up to date (a restarted adapter).
FOLLOW_FOR: Final[timedelta] = timedelta(minutes=10)
PANEL_LIMIT: Final[int] = 25
_STEP_LINES: Final[int] = 15

NOT_IN_WORKSPACE: Final[str] = "This card is not for this workspace."
STALE: Final[str] = "This draft changed since the card. Ask for a new card."
CHANGED: Final[str] = "This workflow changed since the card. Ask again to run it."
UNAVAILABLE: Final[str] = "This card is unavailable. Ask again to redo it."
NO_WORKFLOWS: Final[str] = "No workflows yet. Describe one in chat or make one in the web app."

_STEP_ICON: Final[dict[str, str]] = {
    "succeeded": "✅",
    "running": "🔄",
    "waiting": "⏸️",
    "failed": "❌",
    "skipped": "⏭️",
    "cancelled": "⏹️",
    "unknown": "❓",
}
_RUN_WORDS: Final[dict[str, str]] = {
    "queued": "starting",
    "running": "running",
    "waiting": "waiting for someone",
    "succeeded": "done",
    "failed": "failed",
    "cancelled": "stopped",
    "timed_out": "ran out of time",
}
_STATUS_WORDS: Final[dict[str, str]] = {
    "live": "Live",
    "paused": "Paused",
    "draft": "Draft, not published yet",
    "needs_owner": "Needs a new owner",
    "needs_review": "Waiting for an admin",
}
_TRIGGER_WORDS: Final[dict[str, str]] = {
    "manual": "by hand",
    "webhook": "from a webhook",
    "event": "when another workflow finishes",
    "call": "from another workflow",
}
_GRANT_WORDS: Final[dict[str, str]] = {
    "unattended_write": "writes without asking",
    "builtins": "built-in tools",
    "code_network": "code with network access",
    "unverified_webhook": "an unsigned webhook",
    "budget_over_default": "a budget over the default",
    "untrusted_waiver": "a step that trusts outside input",
}


@dataclass(frozen=True)
class ChatPlace:
    """One chat message: a card a run shows itself on. Teams' channel is its conversation."""

    platform: str
    channel_id: str
    message_id: str
    thread_id: str | None = None

    def ref(self) -> dict[str, str]:
        out = {"platform": self.platform, "channel": self.channel_id, "message": self.message_id}
        return out | ({"thread": self.thread_id} if self.thread_id else {})

    @classmethod
    def of(cls, ref: Mapping[str, Any]) -> ChatPlace:
        return cls(
            str(ref["platform"]), str(ref["channel"]), str(ref["message"]), ref.get("thread")
        )


@dataclass(frozen=True)
class Presser:
    tenant_id: uuid.UUID
    platform: str
    user_id: str


@dataclass(frozen=True)
class FormField:
    key: str
    label: str
    required: bool
    kind: Literal["text", "number", "integer", "boolean"]
    choices: tuple[str, ...] = ()
    default: str = ""
    max_length: int | None = None


@dataclass(frozen=True)
class RunForm:
    workflow_id: uuid.UUID
    title: str
    fields: tuple[FormField, ...]
    test: bool

    @property
    def submit_id(self) -> str:
        return f"wff:{self.workflow_id}:{'t' if self.test else 'l'}"


@dataclass(frozen=True)
class Pressed:
    """What the adapter does next: tell the presser, replace the card, open a form or post a
    run card (`start`, then `start_on_card`)."""

    say: str | None = None
    card: PostedCard | None = None
    form: RunForm | None = None
    start: tuple[uuid.UUID, bool] | None = None
    link: str | None = None


def parse_custom_id(custom_id: str) -> tuple[uuid.UUID, CardAction, str | None] | None:
    match = _CUSTOM_ID.fullmatch(custom_id)
    if match is None:
        return None
    action: CardAction = match["action"]  # pyright: ignore[reportAssignmentType]
    return uuid.UUID(match["id"]), action, match["arg"]


def parse_form_id(custom_id: str) -> tuple[uuid.UUID, bool] | None:
    match = _FORM.fullmatch(custom_id)
    return None if match is None else (uuid.UUID(match["id"]), match["mode"] == "t")


def _id(target: uuid.UUID | str, action: CardAction, arg: str | None = None) -> str:
    return f"wfc:{target}:{action}" + (f":{arg}" if arg else "")


def _card(
    headline: str,
    facts: tuple[str, ...] = (),
    buttons: tuple[CardButton, ...] = (),
    *,
    bad: bool = False,
) -> PostedCard:
    state = "refused" if bad else "applied"
    return PostedCard(kind="workflow", state=state, headline=headline, facts=facts, buttons=buttons)


def notice(text: str) -> PostedCard:
    """A refusal shown in place of a card."""
    return _card(f"🛡️ {text}", bad=True)


def web_root(settings: Settings, tenant_id: uuid.UUID) -> str | None:
    links = WebLinks.from_settings(settings.web)
    if links is None or "workflows" not in links.modules:
        return None
    return f"{links.for_tenant(tenant_id, admin=False).root()}/workflows"


def _open(label: str, url: str | None) -> tuple[CardButton, ...]:
    return () if url is None else (CardButton(label=label, style="link", url=url),)


def _press(label: str, custom_id: str, *, primary: bool = False) -> CardButton:
    return CardButton(label=label, style="primary" if primary else "secondary", custom_id=custom_id)


# ---------- what a card says ----------


def _triggers(spec: Mapping[str, Any]) -> str:
    words: list[str] = []
    for trigger in spec.get("triggers") or [{"kind": "manual"}]:
        kind = str(trigger.get("kind"))
        if kind == "schedule":
            zone = f" ({trigger['timezone']})" if trigger.get("timezone") else ""
            words.append(f"on the schedule {trigger.get('cron')}{zone}")
        else:
            words.append(_TRIGGER_WORDS.get(kind, kind))
    return "Starts " + ", ".join(dict.fromkeys(words))


def _steps(spec: Mapping[str, Any]) -> tuple[str, ...]:
    """The nodes from `start`, in the order the flow first reaches them."""
    nodes: Mapping[str, Any] = spec.get("nodes") or {}
    order: list[str] = []
    queue = [str(spec.get("start") or next(iter(nodes), ""))]
    while queue:
        node_id = queue.pop(0)
        if node_id in order or node_id not in nodes:
            continue
        order.append(node_id)
        queue += _next(nodes[node_id])
    order += [n for n in nodes if n not in order]
    return tuple(f"{i}. {run_views.title_of(n, spec)}" for i, n in enumerate(order, 1))


def _next(node: Mapping[str, Any]) -> list[str]:
    nxt = node.get("next")
    out: list[object] = [nxt] if isinstance(nxt, str) else seq(nxt)
    out += [r if isinstance(r, str) else obj(r).get("to") for r in obj(node.get("routes")).values()]
    out += [obj(c).get("to") for c in seq(node.get("cases"))]
    out += [node.get(k) for k in ("else", "on_error", "body")]
    return [str(n) for n in out if n]


def draft_card(workflow_id: uuid.UUID, draft: views.Draft, web: str | None) -> PostedCard:
    """Name, how it starts, its steps one line each, what needs an admin, and the buttons."""
    spec = draft.spec
    facts = [_triggers(spec), *_steps(spec)]
    if draft.new_grants:
        grants = ", ".join(
            dict.fromkeys(_GRANT_WORDS.get(g.kind, g.kind) for g in draft.new_grants)
        )
        facts.append(f"An admin approves before it goes live: {grants}.")
    if errors := draft.validation.errors:
        facts.append(
            f"Fix {len(errors)} problem{'s' if len(errors) > 1 else ''} before publishing."
        )
    rev = f"{draft.revision:x}"
    return _card(
        f"📝 {spec.get('name') or 'Workflow'}",
        tuple(facts),
        (
            *_open("Open in web", f"{web}/{workflow_id}" if web else None),
            _press("Test run", _id(workflow_id, "test", rev)),
            _press("Publish", _id(workflow_id, "publish", rev), primary=True),
        ),
    )


def _shown(value: object) -> str:
    """One input as a card line shows it: plain text, or JSON when plain would read back wrong."""
    if isinstance(value, str) and "\n" not in value:
        try:
            json.loads(value)
        except ValueError:
            return value
    return json.dumps(value, ensure_ascii=False)


def _read(shown: str) -> object:
    try:
        return json.loads(shown)
    except ValueError:
        return shown


def inputs_digest(workflow_id: uuid.UUID, version: int | None, inputs: Mapping[str, Any]) -> str:
    raw = json.dumps([str(workflow_id), version, inputs], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def confirm_card(
    workflow_id: uuid.UUID,
    name: str,
    version: int | None,
    inputs: Mapping[str, Any],
    web: str | None,
) -> PostedCard:
    """The inputs the agent filled in, and Run for the person to start it with them."""
    return _card(
        f"▶️ Run {name}?",
        tuple(f"{key}: {_shown(value)}" for key, value in inputs.items()) or ("No inputs.",),
        (
            _press(
                "Run",
                _id(workflow_id, "go", inputs_digest(workflow_id, version, inputs)),
                primary=True,
            ),
            *_open("Open in web", f"{web}/{workflow_id}" if web else None),
        ),
    )


def confirmed_inputs(card_text: str, keys: frozenset[str]) -> dict[str, Any]:
    """The inputs a confirmation card shows, read back from its text as the platform drew it."""
    out: dict[str, Any] = {}
    for line in html.unescape(card_text).splitlines():
        match = re.fullmatch(r"(?:-# )?([\w-]+): (.*)", line.strip())
        if match and match[1] in keys and match[1] not in out:
            out[match[1]] = _read(match[2])
    return out


def run_card(run: run_views.RunDetail, web: str | None) -> PostedCard:
    """Each step with its status, the current step, the cost; the result or failure at the end."""
    steps = [
        f"{_STEP_ICON.get(s.status, '▫️')} {run_views.title_of(s.node, run.spec)}" for s in run.steps
    ]
    facts = steps[:_STEP_LINES] + (
        [f"and {len(steps) - _STEP_LINES} more"] if len(steps) > _STEP_LINES else []
    )
    active = run.status in run_views.ACTIVE
    if active and run.step_title:
        facts.append(f"Now: {run.step_title}")
    facts.append(f"Cost so far: ${run.cost_usd:.2f}" if active else f"Cost: ${run.cost_usd:.2f}")
    if run.failure is not None:
        facts.append(run.failure.message)
    elif run.outcome:
        facts.append(f"Result: {run.outcome}")
    kind = "Test run" if run.test else "Run"
    name = run.workflow.get("name", "Workflow")
    words = _RUN_WORDS.get(run.status, run.status)
    icon = "🔄" if active else _STEP_ICON.get(run.status, "❌")
    return _card(
        f"{icon} {name}: {kind} {run.number} {words}",
        tuple(facts),
        (
            *_open("Open run", f"{web}/{run.workflow['id']}/runs/{run.number}" if web else None),
            *((_press("Cancel", _id(run.id, "cancel")),) if active else ()),
        ),
        bad=run.status in ("failed", "timed_out"),
    )


def starting_card(name: str) -> PostedCard:
    return _card(f"🔄 {name}: starting")


def workflow_card(workflow: views.WorkflowSummary, web: str | None) -> PostedCard:
    """One workflow on the panel: its status and Run, Pause or Resume, Open in web."""
    facts = [_STATUS_WORDS.get(workflow.status, workflow.status)]
    if workflow.next_run:
        facts.append(f"Next run {workflow.next_run[:16].replace('T', ' ')} UTC")
    live = workflow.live_version is not None
    buttons: list[CardButton] = []
    if live and workflow.status == "live":
        buttons += [
            _press("Run", _id(workflow.id, "run"), primary=True),
            _press("Pause", _id(workflow.id, "pause")),
        ]
    elif live and workflow.status == "paused":
        buttons.append(_press("Resume", _id(workflow.id, "resume"), primary=True))
    return _card(
        workflow.name,
        tuple(facts),
        (*buttons, *_open("Open in web", f"{web}/{workflow.id}" if web else None)),
    )


def panel_rows(workflows: list[views.WorkflowSummary]) -> list[tuple[str, str, str]]:
    """The picker's options, at most `PANEL_LIMIT`: (id, name, status)."""
    return [
        (w.id, w.name[:100], _STATUS_WORDS.get(w.status, w.status)[:100])
        for w in workflows[:PANEL_LIMIT]
    ]


def panel_headline(workflows: list[views.WorkflowSummary]) -> str:
    if not workflows:
        return NO_WORKFLOWS
    more = f" Showing {PANEL_LIMIT}; the web app lists all." if len(workflows) > PANEL_LIMIT else ""
    return f"Pick a workflow to run, pause or open.{more}"


# ---------- the run form ----------


def run_form(
    workflow_id: uuid.UUID, spec: Mapping[str, Any], *, limit: int, test: bool = False
) -> RunForm | None:
    """The workflow's inputs as text fields, or None when they don't fit (the web runs it)."""
    schema: Mapping[str, Any] = spec.get("inputs") or {}
    properties: Mapping[str, Any] = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    if len(properties) > limit:
        return None
    fields: list[FormField] = []
    for key, field in properties.items():
        kind = field.get("type") or "string"
        if kind not in ("string", "number", "integer", "boolean"):
            return None
        default = field.get("default")
        fields.append(
            FormField(
                key=key,
                label=str(field.get("title") or key)[:45],
                required=key in required,
                kind="text" if kind == "string" else kind,
                choices=tuple(field.get("enum") or ()),
                default="" if default is None else _shown(default),
                max_length=field.get("maxLength"),
            )
        )
    return RunForm(workflow_id, str(spec.get("name") or "Run")[:45], tuple(fields), test)


def form_inputs(form: RunForm, raw: Mapping[str, str]) -> dict[str, Any] | str:
    """Typed inputs from what the person typed, or what to fix."""
    out: dict[str, Any] = {}
    for field in form.fields:
        text = (raw.get(field.key) or "").strip()
        if not text:
            if field.required:
                return f"Fill in {field.label}."
            continue
        if field.choices and text not in field.choices:
            return f"{field.label} must be one of: {', '.join(field.choices)}."
        if field.kind == "boolean":
            if text.lower() not in ("yes", "no", "true", "false"):
                return f"Answer {field.label} with yes or no."
            out[field.key] = text.lower() in ("yes", "true")
        elif field.kind in ("number", "integer"):
            try:
                out[field.key] = int(text) if field.kind == "integer" else float(text)
            except ValueError:
                return f"{field.label} must be a number."
        else:
            out[field.key] = text
    return out


# ---------- presses ----------


async def env_of(
    sm: Sessions, settings: Settings, presser: Presser, now: datetime | None = None
) -> Env | None:
    """The presser as the workflow rules see them, or None when suspended."""
    async with sm.begin() as session:
        principal = await get_or_create_platform_principal(
            session,
            tenant_id=presser.tenant_id,
            platform=presser.platform,
            external_id=presser.user_id,
        )
        who = await who_of(session, presser.tenant_id, principal.account_id)
    if who is None:
        return None
    return Env(sm=sm, settings=settings, who=who, now=now or datetime.now(UTC))


async def run_card_of(sm: Sessions, settings: Settings, run_id: uuid.UUID) -> PostedCard | None:
    async with sm() as session:
        run = await runs_store.get_run(session, run_id)
        if run is None:
            return None
        workflow = await workflows_store.get_workflow(
            session, tenant_id=run.tenant_id, workflow_id=run.workflow_id
        )
        detail = await run_views.detail(session, run, workflow.name if workflow else "")
    return run_card(detail, web_root(settings, run.tenant_id))


async def show_on_card(sm: Sessions, run_id: uuid.UUID, place: ChatPlace) -> None:
    """Record that the card at `place` shows the run, so the follower edits it."""
    async with sm.begin() as session:
        await runs_store.show_on_card(session, run_id, place.ref())


async def start_on_card(
    env: Env,
    workflow_id: uuid.UUID,
    inputs: Mapping[str, Any],
    *,
    test: bool,
    place: ChatPlace,
) -> PostedCard:
    """Start a run that shows itself on the card at `place`; the card to show now."""
    try:
        run = await service.start(env, workflow_id, dict(inputs), test={} if test else None)
    except WorkflowError as err:
        return notice(str(err))
    await show_on_card(env.sm, uuid.UUID(run.id), place)
    return await run_card_of(env.sm, env.settings, uuid.UUID(run.id)) or notice(UNAVAILABLE)


async def press(
    sm: Sessions,
    settings: Settings,
    *,
    presser: Presser,
    custom_id: str,
    place: ChatPlace,
    card_text: str = "",
    form_limit: int = 5,
) -> Pressed:
    """One press on a workflow card, as the person who pressed."""
    parsed = parse_custom_id(custom_id)
    env = await env_of(sm, settings, presser)
    if parsed is None or env is None:
        return Pressed(say=UNAVAILABLE)
    target, action, arg = parsed
    try:
        await service.ensure_on(env)
        if action == "cancel":
            await service.cancel(env, target)
            return Pressed(card=await run_card_of(sm, settings, target))
        workflow = await service.get(env, target)
        web = web_root(settings, env.who.tenant_id)
        if action in ("pause", "resume"):
            changed = await service.patch(env, target, Patch(paused=action == "pause"))
            return Pressed(card=workflow_card(changed, web))
        if action == "run":
            return _form(workflow, web, form_limit, test=False)
        if action == "go":
            keys = frozenset(obj(obj(workflow.spec.get("inputs")).get("properties")))
            inputs = confirmed_inputs(card_text, keys)
            if arg != inputs_digest(target, workflow.live_version, inputs):
                return Pressed(say=CHANGED)
            return Pressed(card=await start_on_card(env, target, inputs, test=False, place=place))
        draft = await service.get_draft(env, target)
        if arg != f"{draft.revision:x}":
            return Pressed(say=STALE)
        if action == "test":
            if obj(draft.spec.get("inputs")).get("required"):
                return _form(workflow, web, form_limit, test=True, spec=draft.spec)
            return Pressed(start=(target, True))
        return await _publish(env, workflow, draft, web)
    except WorkflowError as err:
        return Pressed(say=str(err))


def _form(
    workflow: views.Workflow,
    web: str | None,
    limit: int,
    *,
    test: bool,
    spec: Mapping[str, Any] | None = None,
) -> Pressed:
    form = run_form(uuid.UUID(workflow.id), spec or workflow.spec, limit=limit, test=test)
    if form is None:
        link = f"{web}/{workflow.id}" if web else None
        return Pressed(say="This form doesn't fit here. Run it from the web app.", link=link)
    if not form.fields:
        return Pressed(start=(uuid.UUID(workflow.id), test))
    return Pressed(form=form)


async def _publish(
    env: Env, workflow: views.Workflow, draft: views.Draft, web: str | None
) -> Pressed:
    """Publish as the presser; with grants only an admin gives, ask an admin as the web does."""
    wid = uuid.UUID(workflow.id)
    if draft.new_grants and not env.who.admin:
        await service.request_publish(env, wid, "")
        return Pressed(
            card=_card(f"⏳ {workflow.name}: sent to an admin to approve", _open_web(web, wid))
        )
    published = await service.publish(env, wid, "", source="chat")
    return Pressed(
        card=_card(
            f"✅ {published.name}: version {published.live_version} is live", _open_web(web, wid)
        )
    )


def _open_web(web: str | None, workflow_id: uuid.UUID) -> tuple[str, ...]:
    return () if web is None else (f"Open it in the web app: {web}/{workflow_id}",)


async def submit_form(
    sm: Sessions,
    settings: Settings,
    *,
    presser: Presser,
    form_id: str,
    raw: Mapping[str, str],
    limit: int = 5,
) -> tuple[Env, RunForm, dict[str, Any]] | str:
    """A run form's submit: who, which form and the typed inputs, or what to tell the person."""
    parsed = parse_form_id(form_id)
    env = await env_of(sm, settings, presser)
    if parsed is None or env is None:
        return UNAVAILABLE
    workflow_id, test = parsed
    try:
        await service.ensure_on(env)
        spec = (
            (await service.get_draft(env, workflow_id)).spec
            if test
            else (await service.get(env, workflow_id)).spec
        )
    except WorkflowError as err:
        return str(err)
    form = run_form(workflow_id, spec, limit=limit, test=test)
    if form is None:
        return UNAVAILABLE
    inputs = form_inputs(form, raw)
    return inputs if isinstance(inputs, str) else (env, form, inputs)


async def open_run(
    env: Env,
    workflow_id: uuid.UUID,
    inputs: Mapping[str, Any],
    *,
    test: bool,
    name: str,
    post: Callable[[PostedCard], Awaitable[ChatPlace]],
    edit: Callable[[ChatPlace, PostedCard], Awaitable[None]],
) -> None:
    """Post a run card, start the run on it, and show what happened."""
    place = await post(starting_card(name))
    await edit(place, await start_on_card(env, workflow_id, inputs, test=test, place=place))


async def open_pressed(
    sm: Sessions,
    settings: Settings,
    presser: Presser,
    workflow_id: uuid.UUID,
    *,
    test: bool,
    post: Callable[[PostedCard], Awaitable[ChatPlace]],
    edit: Callable[[ChatPlace, PostedCard], Awaitable[None]],
) -> str | None:
    """`Pressed.start`: open a run with no inputs as the presser; a refusal to tell them."""
    env = await env_of(sm, settings, presser)
    if env is None:
        return UNAVAILABLE
    try:
        workflow = await service.get(env, workflow_id)
    except WorkflowError as err:
        return str(err)
    await open_run(env, workflow_id, {}, test=test, name=workflow.name, post=post, edit=edit)
    return None


async def list_for(
    sm: Sessions, settings: Settings, presser: Presser
) -> list[views.WorkflowSummary] | str:
    """The workflows the presser can see, or the refusal to show."""
    env = await env_of(sm, settings, presser)
    if env is None:
        return UNAVAILABLE
    try:
        await service.ensure_on(env)
        return await service.list_workflows(env)
    except WorkflowError as err:
        return str(err)


async def pick(sm: Sessions, settings: Settings, presser: Presser, workflow_id: str) -> PostedCard:
    env = await env_of(sm, settings, presser)
    if env is None:
        return notice(UNAVAILABLE)
    try:
        await service.ensure_on(env)
        workflow = await service.get(env, uuid.UUID(workflow_id))
    except (WorkflowError, ValueError) as err:
        return notice(str(err) if isinstance(err, WorkflowError) else UNAVAILABLE)
    return workflow_card(workflow, web_root(settings, env.who.tenant_id))


# ---------- following run cards ----------


#: Edits a run card: the run's tenant, where the card is, and the card to show.
CardEditor = Callable[[uuid.UUID, ChatPlace, PostedCard], Awaitable[None]]


async def follow_cards(
    sm: Sessions,
    settings: Settings,
    *,
    platform: str,
    edit: CardEditor,
    seen: dict[uuid.UUID, tuple[str, datetime]],
    now: datetime,
) -> int:
    """Edit each chat-started run's card when it changed and was not edited in `EDIT_EVERY`.

    `seen` is the caller's memory between rounds; a fresh one edits every card once."""
    async with sm() as session:
        rows = await runs_store.carded_runs(
            session,
            platform=platform,
            ended_after=now - FOLLOW_FOR,
            started_after=now - timedelta(days=1),
        )
    edited = 0
    for row in rows:
        last = seen.get(row.id)
        if last is not None and now - last[1] < EDIT_EVERY:
            continue
        card = await run_card_of(sm, settings, row.id)
        if card is None:
            continue
        key = hashlib.sha256(card.model_dump_json().encode()).hexdigest()
        if last is not None and last[0] == key:
            continue
        try:
            await edit(row.tenant_id, ChatPlace.of(row.triggered_by["card"]), card)
        except Exception as err:
            # Boundary: a chat API call; the next round tries again.
            log.warning(
                "workflow.run_card_edit_failed", run_id=str(row.id), error=type(err).__name__
            )
            continue
        seen[row.id] = (key, now)
        edited += 1
    for gone in set(seen) - {row.id for row in rows}:
        del seen[gone]
    return edited


def follower(
    sm: Sessions, settings: Settings, *, platform: str, edit: CardEditor
) -> Callable[[], Awaitable[int]]:
    """One round of `follow_cards` with its own memory, for the delivery poller."""
    seen: dict[uuid.UUID, tuple[str, datetime]] = {}

    async def once() -> int:
        return await follow_cards(
            sm, settings, platform=platform, edit=edit, seen=seen, now=datetime.now(UTC)
        )

    return once
