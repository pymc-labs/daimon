"""Web drafts of an agent: what changed, when it reaches conversations, apply and revert.

`diff_config` and `change_effect` are pure. The rest reads the draft and version
stores, and writes through the agent edit service.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal, cast

import structlog
from anthropic.types.beta import BetaManagedAgentsAgent
from anthropic.types.beta.agent_create_params import Tool
from anthropic.types.beta.beta_managed_agents_skill_params import BetaManagedAgentsSkillParams
from anthropic.types.beta.beta_managed_agents_url_mcp_server_params import (
    BetaManagedAgentsURLMCPServerParams,
)
from daimon.core import agent_editing
from daimon.core.agent_editing import (
    AgentActor,
    AgentEditDeps,
    AgentEditError,
    AgentEditRefusal,
    AgentPatch,
    PlaceEdit,
)
from daimon.core.agent_model_names import model_name
from daimon.core.agent_view import (
    AgentConfigDoc,
    AgentFacts,
    AgentViewDeps,
    AgentViewer,
    Backend,
    McpServer,
    ModelChoice,
    PlaceDoc,
    config_of,
    load_agent_facts,
    redact_config,
    sees_details,
)
from daimon.core.channel_backend import (
    AgentBackendRefused,
    AgentBackendService,
    BackendActor,
    BackendUnsupported,
    load_backend_agent,
)
from daimon.core.constants import MODEL_DISPLAY_NAMES
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.model_presets import ModelConfig
from daimon.core.permissions import WEB_PLACE_ID
from daimon.core.stores import agent_drafts as drafts_store
from daimon.core.stores import agent_tool_rules
from daimon.core.stores.agent_tool_rules import Rule
from daimon.core.stores.domain import BackendAgent, CompatibilityPreview
from daimon.core.turn.notices import provider_name
from mux.contracts.config import DEFAULT_BACKEND
from mux.contracts.ids import ModelRef, Provider
from pydantic import BaseModel, ConfigDict, Field

log = structlog.get_logger(__name__)

Effect = Literal["live", "session", "threads"]
Axis = Literal["identity", "mutable", "route"]

# What each change key feeds in a running session's snapshot (`session_snapshot`):
# an identity field needs a new session, a mutable one reaches it live, and a route
# change (who the agent is to people, where it answers) touches no session.
EFFECT_FIELDS: dict[str, tuple[Axis, tuple[str, ...]]] = {
    "system": ("identity", ("system_sha256",)),
    "model": ("identity", ("model_id", "model_speed")),
    "skill": ("identity", ("skills_sha256",)),
    "tool": ("mutable", ("tools_sha256",)),
    "mcp": ("mutable", ("mcp_servers_sha256", "tools_sha256")),
    "name": ("route", ()),
    "purpose": ("route", ()),
    "avatar": ("route", ()),
    "place": ("route", ()),
}


def _omit_none(value: object) -> bool:
    return value is None


class Change(BaseModel):
    """One difference between two configs, as the review dialog and history show it."""

    model_config = ConfigDict(frozen=True)

    key: str
    node: str
    label: str
    kind: Literal["add", "remove", "edit"]
    effect: Effect
    before: str | None = Field(default=None, exclude_if=_omit_none)
    after: str | None = Field(default=None, exclude_if=_omit_none)
    text: bool | None = Field(default=None, exclude_if=_omit_none)


class Draft(BaseModel):
    model_config = ConfigDict(frozen=True)

    base_version: int
    config: AgentConfigDoc
    changes: list[Change]
    stale: bool


def change_effect(key: str, *, backend: Backend) -> Effect:
    """When a change to ``key`` reaches conversations on ``backend``. Pure."""
    axis, _fields = EFFECT_FIELDS[key.split(":", 1)[0]]
    if axis == "route":
        return "live"
    # A neutral backend refuses changes on bound threads; new threads get them.
    if backend != DEFAULT_BACKEND:
        return "threads"
    return "session" if axis == "identity" else "live"


_RULE_LABELS = {"allow": "Allow", "ask": "Ask", "deny": "Deny"}
_AUTH_LABELS = {"oauth": "OAuth", "token": "Token", "none": "None"}


def _model_label(config: AgentConfigDoc) -> str:
    model = config.model
    name = MODEL_DISPLAY_NAMES.get(model.id) or model_name(
        ModelRef(provider=model.backend, id=model.id)
    )
    return f"{name} fast" if model.speed == "fast" else name


def _place_label(place: PlaceDoc) -> str:
    return f"#{place.name}" if place.kind == "channel" else place.name


def _diff_servers(a: list[McpServer], b: list[McpServer], add: Callable[..., None]) -> None:
    before = {server.key: server for server in a}
    after = {server.key: server for server in b}
    for key, server in after.items():
        node = f"mcp:{key}"
        old = before.get(key)
        if old is None:
            add(f"mcp:{key}", node, f"{server.name} server", "add", after=server.url)
            continue
        if old.url != server.url:
            add(f"mcp:{key}/url", node, f"{server.name} URL", "edit", old.url, server.url)
        if old.auth != server.auth:
            add(
                f"mcp:{key}/auth",
                node,
                f"{server.name} sign-in",
                "edit",
                _AUTH_LABELS[old.auth],
                _AUTH_LABELS[server.auth],
            )
        old_rules = {tool.name: tool.rule for tool in old.tools}
        for tool in server.tools:
            # A tool the old config did not list ran under the default, allow.
            was = old_rules.get(tool.name, "allow")
            if was != tool.rule:
                add(
                    f"mcp:{key}/{tool.name}",
                    node,
                    f"{server.name} {tool.name}",
                    "edit",
                    _RULE_LABELS[was],
                    _RULE_LABELS[tool.rule],
                )
    for key, server in before.items():
        if key not in after:
            add(f"mcp:{key}", f"mcp:{key}", f"{server.name} server", "remove", before=server.url)


def diff_config(a: AgentConfigDoc, b: AgentConfigDoc) -> list[Change]:
    """Every difference from ``a`` to ``b``, tagged with when it reaches conversations. Pure."""
    backend = a.model.backend
    out: list[Change] = []

    def add(
        key: str,
        node: str,
        label: str,
        kind: Literal["add", "remove", "edit"],
        before: str | None = None,
        after: str | None = None,
        *,
        text: bool | None = None,
        effect: Effect | None = None,
    ) -> None:
        out.append(
            Change(
                key=key,
                node=node,
                label=label,
                kind=kind,
                effect=effect or change_effect(key, backend=backend),
                before=before,
                after=after,
                text=text,
            )
        )

    if a.name != b.name:
        add("name", "agent", "Name", "edit", a.name, b.name)
    if a.purpose != b.purpose:
        add("purpose", "agent", "Description", "edit", a.purpose, b.purpose)
    if a.avatar != b.avatar:
        add("avatar", "agent", "Avatar", "edit")
    if (a.system or "") != (b.system or ""):
        add(
            "system",
            "instructions",
            "Instructions",
            "edit",
            a.system or "",
            b.system or "",
            text=True,
        )
    if a.model != b.model:
        moved = a.model.backend != b.model.backend
        add(
            "model",
            "model",
            "Model",
            "edit",
            _model_label(a),
            _model_label(b),
            effect="threads" if moved else None,
        )
    skills_a = {skill.skill_id: skill for skill in a.skills}
    skills_b = {skill.skill_id: skill for skill in b.skills}
    for skill_id, skill in skills_b.items():
        old = skills_a.get(skill_id)
        label = f"{skill.title} skill"
        if old is None:
            add(f"skill:{skill_id}", "skills", label, "add")
        elif old.version != skill.version:
            add(f"skill:{skill_id}", "skills", label, "edit", old.version, skill.version)
    for skill_id, skill in skills_a.items():
        if skill_id not in skills_b:
            add(f"skill:{skill_id}", "skills", f"{skill.title} skill", "remove")
    tools_a = {tool.name: tool.enabled for tool in a.tools}
    for tool in b.tools:
        was = tools_a.get(tool.name)
        if was is not None and was != tool.enabled:
            add(
                f"tool:{tool.name}",
                "tools",
                f"{tool.name} tool",
                "add" if tool.enabled else "remove",
            )
    _diff_servers(a.mcp_servers, b.mcp_servers, add)
    places_a = {place.id: place for place in a.places or []}
    places_b = {place.id: place for place in b.places or []}
    for place_id, place in places_b.items():
        old = places_a.get(place_id)
        if old is None:
            add(f"place:{place_id}", "places", _place_label(place), "add")
        elif bool(old.default) != bool(place.default):
            add(
                f"place:{place_id}",
                "places",
                f"{_place_label(place)} default",
                "edit",
                "On" if old.default else "Off",
                "On" if place.default else "Off",
            )
    for place_id, place in places_a.items():
        if place_id not in places_b:
            add(f"place:{place_id}", "places", _place_label(place), "remove")
    return out


def unsupported(changes: list[Change], *, a: AgentConfigDoc, b: AgentConfigDoc) -> str | None:
    """Why these changes cannot be applied from the web yet, or None. Pure."""
    for change in changes:
        head = change.key.split(":", 1)[0]
        if head in ("name", "avatar") or change.key.endswith("/auth"):
            return f"{change.label} can't be changed here yet. Nothing was saved."
    if (
        a.model.backend == b.model.backend != DEFAULT_BACKEND
        and a.model.id == b.model.id
        and a.model != b.model
    ):
        return "Effort and speed can't be changed for this model here yet. Nothing was saved."
    return None


def provider_moved(a: AgentConfigDoc, b: AgentConfigDoc) -> bool:
    """Whether ``b`` runs on another provider, or another model off the default one. Pure.

    That is the agent's provider setting, not its Managed Agents model.
    """
    return a.model.backend != b.model.backend or (
        b.model.backend != DEFAULT_BACKEND and a.model.id != b.model.id
    )


def _tools_for(agent: BetaManagedAgentsAgent, target: AgentConfigDoc) -> list[Tool]:
    """The agent's tool list with the target's built-ins and one toolset per target server."""
    servers = {server.key for server in target.mcp_servers}
    tools: list[Tool] = []
    for tool in agent.tools:
        param = agent_editing.ma_tool_to_param(tool)
        if param.get("type") == "mcp_toolset" and param.get("mcp_server_name") not in servers:
            continue
        tools.append(param)
    named = {t.get("mcp_server_name") for t in tools if t.get("type") == "mcp_toolset"}
    for key in sorted(servers - named):
        tools.append({"type": "mcp_toolset", "mcp_server_name": key})
    for builtin in target.tools:
        if agent_editing.builtin_enabled(tools, builtin.name) != builtin.enabled:
            tools = agent_editing.with_builtin(tools, builtin.name, builtin.enabled)
    return tools


def patch_for(
    agent: BetaManagedAgentsAgent, changes: list[Change], target: AgentConfigDoc
) -> AgentPatch:
    """The MA write that turns ``agent`` into ``target``, for the changes it needs. Pure."""
    heads = {change.key.split(":", 1)[0] for change in changes}
    server_shape = any(
        change.key.startswith("mcp:")
        and "/" not in change.key.removeprefix("mcp:")
        or change.key.endswith("/url")
        for change in changes
    )
    model: ModelConfig | None = None
    # Another provider's model is the provider setting's (`switch_provider`), not Managed Agents'.
    if "model" in heads and target.model.backend == DEFAULT_BACKEND:
        model = {"id": target.model.id, "speed": target.model.speed, "effort": target.model.effort}
    skills: list[BetaManagedAgentsSkillParams] | None = None
    if "skill" in heads:
        skills = [
            cast(
                BetaManagedAgentsSkillParams,
                {"type": skill.type, "skill_id": skill.skill_id, "version": skill.version},
            )
            for skill in target.skills
        ]
    servers: list[BetaManagedAgentsURLMCPServerParams] | None = None
    if server_shape:
        servers = [
            {"type": "url", "name": server.key, "url": server.url} for server in target.mcp_servers
        ]
    return AgentPatch(
        model=model,
        description=target.purpose if "purpose" in heads else None,
        system=(target.system or "") if "system" in heads else None,
        tools=_tools_for(agent, target) if "tool" in heads or server_shape else None,
        mcp_servers=servers,
        skills=skills,
        merge="replace",
    )


def rule_changes(changes: list[Change]) -> list[tuple[str, str]]:
    """(server key, tool) for each tool rule change. Pure."""
    out: list[tuple[str, str]] = []
    for change in changes:
        if not change.key.startswith("mcp:"):
            continue
        server, _sep, tool = change.key.removeprefix("mcp:").partition("/")
        if tool and tool not in ("url", "auth"):
            out.append((server, tool))
    return out


def revert_target(current: AgentConfigDoc, old: AgentConfigDoc) -> AgentConfigDoc:
    """``old`` as a revert writes it: name, face, places and sign-in stay as they are. Pure."""
    auth = {server.key: server.auth for server in current.mcp_servers}
    return old.model_copy(
        update={
            "name": current.name,
            "avatar": current.avatar,
            "places": current.places,
            "mcp_servers": [
                server.model_copy(update={"auth": auth.get(server.key, server.auth)})
                for server in old.mcp_servers
            ],
        }
    )


def with_hidden_places(
    base: AgentConfigDoc, target: AgentConfigDoc, *, is_admin: bool
) -> AgentConfigDoc:
    """``target`` with the places its author could not see kept as ``base`` has them. Pure.

    A non-admin never reads confidential places, so their copy of the config lacks them;
    without this, saving it would read as removing them.
    """
    if target.places is None:
        return target.model_copy(update={"places": base.places})
    if is_admin or base.places is None:
        return target
    shown = {place.id for place in target.places}
    kept = [p for p in base.places if p.confidential and p.id not in shown]
    return target.model_copy(update={"places": [*target.places, *kept]})


def keep_unseen(
    base: AgentConfigDoc, config: AgentConfigDoc, *, details: bool, is_admin: bool
) -> AgentConfigDoc:
    """``config`` with what its author could not see taken from ``base``. Pure.

    Without details the prompt, the MCP servers and the places come back redacted, so a
    round trip carries nothing of them; reading that as an edit would erase the prompt
    and the servers' URLs. Confidential places are kept for a non-admin as in
    `with_hidden_places`.
    """
    if not details:
        config = config.model_copy(
            update={
                "system": base.system,
                "mcp_servers": base.mcp_servers,
                "places": base.places,
            }
        )
    return with_hidden_places(base, config, is_admin=is_admin)


def confidential_place_ids(configs: Iterable[AgentConfigDoc]) -> set[str]:
    """The ids of the confidential places any of ``configs`` lists. Pure."""
    return {place.id for config in configs for place in config.places or [] if place.confidential}


def without_hidden_place_changes(changes: list[Change], hidden: set[str]) -> list[Change]:
    """``changes`` minus those to a place in ``hidden``. Pure."""
    return [
        change
        for change in changes
        if not (change.key.startswith("place:") and change.key.removeprefix("place:") in hidden)
    ]


def redact_draft(draft: Draft, *, details: bool, is_admin: bool) -> Draft:
    """``draft`` as its author may read it: the config as the agent view redacts it, no
    values in the changes without details, and no change to a place they cannot see. Pure."""
    hidden = set[str]() if is_admin else confidential_place_ids([draft.config])
    changes = without_hidden_place_changes(draft.changes, hidden)
    if not details:
        changes = [change.model_copy(update={"before": None, "after": None}) for change in changes]
    return draft.model_copy(
        update={
            "config": redact_config(draft.config, details=details, is_admin=is_admin),
            "changes": changes,
        }
    )


def _provider_state(model: ModelChoice) -> list[str | None]:
    """The provider setting kept beside the agent; Anthropic's model lives in its version."""
    return [model.backend, None if model.backend == DEFAULT_BACKEND else model.id]


def _state_hash(
    places: Iterable[PlaceDoc], rules: Iterable[tuple[str, str, str]], model: ModelChoice
) -> str:
    return hashlib.sha256(
        json.dumps(
            [
                sorted((place.id, bool(place.default)) for place in places),
                sorted(rules),
                _provider_state(model),
            ]
        ).encode()
    ).hexdigest()


def db_state(config: AgentConfigDoc) -> str:
    """A fingerprint of what the config keeps outside the provider's agent: its places, its
    tool rules and its model provider. They change without a new provider version. Pure."""
    rules = (
        (server.key, tool.name, tool.rule)
        for server in config.mcp_servers
        for tool in server.tools
        if tool.rule != "allow"
    )
    return _state_hash(config.places or [], rules, config.model)


def stored_db_state(base: AgentConfigDoc, facts: AgentFacts) -> str:
    """`db_state` of ``base``'s tools and places as they are stored now.

    Read from the stored places and rules, never a server's live tool listing, so an
    unreachable server cannot make a draft stale. Pure.
    """
    stored = {
        tuple(rule.tool_key.split("/", 1)): rule.rule
        for rule in facts.tool_rules
        if rule.channel_id is None and rule.rule is not None and "/" in rule.tool_key
    }
    rules = (
        (server.key, tool.name, stored_rule)
        for server in base.mcp_servers
        for tool in server.tools
        if (stored_rule := stored.get((server.key, tool.name), "allow")) != "allow"
    )
    return _state_hash(facts.places, rules, config_of(facts).model)


def place_edits(places: list[PlaceDoc] | None) -> list[PlaceEdit]:
    """The channels and web chat among ``places``, as the write takes them. Pure."""
    return [
        PlaceEdit(WEB_PLACE_ID if place.kind == "web" else place.id, bool(place.default))
        for place in places or []
        if place.kind != "dm"
    ]


# ---------- Shell ----------


@dataclass(frozen=True)
class AgentChangeDeps:
    edit: AgentEditDeps
    view: AgentViewDeps
    # Writes the agent's model provider; None where this deployment cannot switch it.
    backends: AgentBackendService | None = None


_PROVIDER_REFUSALS: dict[str, tuple[str, AgentEditRefusal]] = {
    "feature_disabled": ("Choosing a model provider is turned off here.", "forbidden"),
    "owner_or_admin_required": ("Only the agent's owner or an admin can do that.", "forbidden"),
    "admin_required": ("Only an admin can do that.", "forbidden"),
    "model_not_selectable": ("That model isn't available. Pick another.", "invalid"),
    "profile_not_selectable": ("That model isn't available. Pick another.", "invalid"),
    "stale_revision": ("The model changed since your draft began", "stale"),
    "drops_required": ("That model can't use some of this agent's skills or servers.", "invalid"),
    "stale_preview": ("The model changed since your draft began", "stale"),
}


def _provider_refusal(exc: AgentBackendRefused, provider: str) -> AgentEditError:
    if exc.reason == "backend_not_allowed":
        text = f"{provider_name(provider)} isn't turned on here. Ask an admin."
        return AgentEditError(f"{text} Nothing was saved.", kind="forbidden")
    text, kind = _PROVIDER_REFUSALS.get(exc.reason, ("That model can't be chosen here.", "invalid"))
    return AgentEditError(f"{text} Nothing was saved.", kind=kind)


@dataclass(frozen=True)
class ProviderSwitch:
    """A provider change checked and previewed, written by `write_provider_switch`."""

    source: BackendAgent
    actor: BackendActor
    provider: Provider
    backend: Provider | None
    model: str | None
    preview: CompatibilityPreview
    expected_revision: int


def _refused_switch(exc: Exception, *, agent_id: str, provider: Provider) -> AgentEditError:
    if isinstance(exc, AgentBackendRefused):
        log.info("agent_changes.provider_refused", agent_id=agent_id, reason=exc.reason)
        return _provider_refusal(exc, provider)
    log.info("agent_changes.provider_unsupported", agent_id=agent_id, provider=provider)
    return _provider_refusal(AgentBackendRefused("model_not_selectable"), provider)


async def check_provider_switch(
    deps: AgentChangeDeps,
    *,
    actor: AgentActor,
    agent: BetaManagedAgentsAgent,
    provider: Provider,
    model_id: str,
    expected_revision: int | None = None,
) -> ProviderSwitch:
    """Refuse now what pointing the agent at ``provider`` would refuse, writing nothing.

    ``expected_revision`` is the provider setting the change was made against; a setting
    that moved since is stale. None takes the current one.
    """
    if deps.backends is None or not deps.backends.enabled:
        raise _provider_refusal(AgentBackendRefused("feature_disabled"), provider)
    backend = None if provider == DEFAULT_BACKEND else provider
    model = None if backend is None else model_id
    backend_actor = BackendActor(
        tenant_id=actor.tenant_id, account_id=actor.account_id, subject=actor.subject
    )
    try:
        source = await load_backend_agent(
            deps.edit.client,
            deps.edit.session_factory,
            tenant_id=actor.tenant_id,
            agent_name=agent.metadata.get(MA_METADATA_KEY_NAME) or agent.name,
            actual_agent=agent,
        )
        if not await deps.backends.may_edit(source, backend_actor):
            raise AgentBackendRefused("owner_or_admin_required")
        preview = await deps.backends.preview(source, backend, model)
        if not preview.allowed:
            raise AgentBackendRefused("backend_not_allowed")
        if preview.declared_drops:
            raise AgentBackendRefused("drops_required")
        if expected_revision is not None and preview.setting_revision != expected_revision:
            raise AgentBackendRefused("stale_revision")
    except (AgentBackendRefused, BackendUnsupported) as exc:
        raise _refused_switch(exc, agent_id=agent.id, provider=provider) from exc
    return ProviderSwitch(
        source=source,
        actor=backend_actor,
        provider=provider,
        backend=backend,
        model=model,
        preview=preview,
        expected_revision=preview.setting_revision,
    )


async def write_provider_switch(deps: AgentChangeDeps, switch: ProviderSwitch) -> None:
    """Write a checked switch; refused as stale if the setting or its preview moved since."""
    if deps.backends is None:
        raise _provider_refusal(AgentBackendRefused("feature_disabled"), switch.provider)
    try:
        if switch.backend is None:
            await deps.backends.clear_backend(
                switch.source,
                switch.expected_revision,
                switch.actor,
                expected_preview_digest=switch.preview.digest,
            )
        else:
            await deps.backends.set_backend(
                switch.source,
                switch.backend,
                switch.expected_revision,
                switch.actor,
                expected_preview_digest=switch.preview.digest,
                model=switch.model,
            )
    except (AgentBackendRefused, BackendUnsupported) as exc:
        raise _refused_switch(exc, agent_id=switch.source.name, provider=switch.provider) from exc


async def switch_provider(
    deps: AgentChangeDeps,
    *,
    actor: AgentActor,
    agent: BetaManagedAgentsAgent,
    provider: Provider,
    model_id: str,
) -> None:
    """Point the agent at ``provider`` and its ``model_id``, as the chat switch does.

    The provider setting lives beside the agent, not in its Managed Agents version, and
    applies to new conversations. Refused with `AgentEditError` in the editor's words.
    """
    switch = await check_provider_switch(
        deps, actor=actor, agent=agent, provider=provider, model_id=model_id
    )
    await write_provider_switch(deps, switch)


def _provider_revision(facts: AgentFacts) -> int:
    """The agent's provider setting revision, as the backend service counts it."""
    return facts.backend.local if facts.backend is not None else 0


def _dump(config: AgentConfigDoc) -> dict[str, Any]:
    return config.model_dump(mode="json")


async def _facts(
    deps: AgentChangeDeps, *, tenant_id: uuid.UUID, agent: BetaManagedAgentsAgent
) -> AgentFacts:
    (facts,) = await load_agent_facts(
        deps.view, tenant_id=tenant_id, agents=[agent], spend_since=None
    )
    return facts


async def current_config(
    deps: AgentChangeDeps, *, tenant_id: uuid.UUID, agent: BetaManagedAgentsAgent
) -> AgentConfigDoc:
    return config_of(await _facts(deps, tenant_id=tenant_id, agent=agent))


@asynccontextmanager
async def _agent_edit_lock(
    deps: AgentChangeDeps, *, tenant_id: uuid.UUID, agent_id: str
) -> AsyncIterator[None]:
    """Hold the agent's edit lock until the block ends, so two applies on one agent run
    one after the other: the second then sees the first's places and rules in its check."""
    async with deps.edit.session_factory() as session, session.begin():
        await drafts_store.lock_agent_edits(session, tenant_id=tenant_id, agent_id=agent_id)
        yield


def _viewer(actor: AgentActor) -> AgentViewer:
    return AgentViewer(account_id=actor.account_id, is_admin=actor.subject.is_admin, may_edit=True)


def _stale(agent: BetaManagedAgentsAgent) -> AgentEditError:
    return AgentEditError(f"Changed to v{agent.version} since your draft began", kind="stale")


def _moved() -> AgentEditError:
    return AgentEditError(
        "This agent's model, where it answers or its tool rules changed since your draft began",
        kind="stale",
    )


async def _begin(
    deps: AgentChangeDeps,
    *,
    actor: AgentActor,
    agent: BetaManagedAgentsAgent,
    base_version: int,
    facts: AgentFacts,
) -> AgentConfigDoc | None:
    """The config a draft on ``base_version`` begins from, or None when that version is gone.

    Only the version the agent is at can begin one. Its config is read now and kept with
    the draft, since places and tool rules are not part of the provider's version.
    """
    if base_version != agent.version:
        return None
    config = config_of(facts)
    async with deps.edit.session_factory() as session:
        await drafts_store.record_base(
            session,
            tenant_id=actor.tenant_id,
            agent_id=agent.id,
            version=agent.version,
            config=_dump(config),
            at=agent.updated_at,
        )
        await session.commit()
    return config


async def _draft_row(
    deps: AgentChangeDeps, *, actor: AgentActor, agent_id: str
) -> drafts_store.AgentDraftRow | None:
    async with deps.edit.session_factory() as session:
        return await drafts_store.get_draft(
            session, tenant_id=actor.tenant_id, agent_id=agent_id, account_id=actor.account_id
        )


async def load_draft(
    deps: AgentChangeDeps,
    *,
    actor: AgentActor,
    agent: BetaManagedAgentsAgent,
    facts: AgentFacts | None = None,
) -> Draft | None:
    """The actor's draft of ``agent`` as they may read it, with its changes, or None.

    Refused with `AgentEditError` unless the actor may still change the agent.
    """
    await agent_editing.check_may_edit(deps.edit, actor=actor, agent=agent)
    row = await _draft_row(deps, actor=actor, agent_id=agent.id)
    if row is None:
        return None
    facts = facts or await _facts(deps, tenant_id=actor.tenant_id, agent=agent)
    config = AgentConfigDoc.model_validate(row.config)
    base = AgentConfigDoc.model_validate(row.base_config)
    draft = Draft(
        base_version=row.base_version,
        config=config,
        changes=diff_config(base, config),
        stale=row.base_version != agent.version or db_state(base) != stored_db_state(base, facts),
    )
    return redact_draft(
        draft, details=sees_details(facts, _viewer(actor)), is_admin=actor.subject.is_admin
    )


async def save_draft(
    deps: AgentChangeDeps,
    *,
    actor: AgentActor,
    ma_agent_id: str,
    base_version: int,
    config: AgentConfigDoc,
) -> Draft | None:
    """Keep the actor's draft; a draft with no changes is dropped (None).

    ``config`` is what the actor was shown: whatever they could not see is taken from
    the draft's base, so saving a redacted view changes nothing but what they edited.
    """
    agent = await agent_editing.load_editable_agent(deps.edit, actor=actor, ma_agent_id=ma_agent_id)
    facts = await _facts(deps, tenant_id=actor.tenant_id, agent=agent)
    row = await _draft_row(deps, actor=actor, agent_id=agent.id)
    if row is not None and row.base_version == base_version:
        base = AgentConfigDoc.model_validate(row.base_config)
    else:
        base = await _begin(deps, actor=actor, agent=agent, base_version=base_version, facts=facts)
        if base is None:
            raise _stale(agent)
    details = sees_details(facts, _viewer(actor))
    config = keep_unseen(base, config, details=details, is_admin=actor.subject.is_admin)
    changes = diff_config(base, config)
    if not changes:
        await _drop_draft(deps, actor=actor, agent_id=agent.id)
        return None
    async with deps.edit.session_factory() as session:
        await drafts_store.save_draft(
            session,
            tenant_id=actor.tenant_id,
            agent_id=agent.id,
            account_id=actor.account_id,
            base_version=base_version,
            base_config=_dump(base),
            config=_dump(config),
        )
        await session.commit()
    draft = Draft(
        base_version=base_version,
        config=config,
        changes=changes,
        stale=base_version != agent.version or db_state(base) != stored_db_state(base, facts),
    )
    return redact_draft(draft, details=details, is_admin=actor.subject.is_admin)


async def _drop_draft(deps: AgentChangeDeps, *, actor: AgentActor, agent_id: str) -> None:
    async with deps.edit.session_factory() as session:
        await drafts_store.delete_draft(
            session, tenant_id=actor.tenant_id, agent_id=agent_id, account_id=actor.account_id
        )
        await session.commit()


async def discard_draft(deps: AgentChangeDeps, *, actor: AgentActor, ma_agent_id: str) -> None:
    """Drop the actor's draft; refused unless they may still change the agent."""
    agent = await agent_editing.load_editable_agent(deps.edit, actor=actor, ma_agent_id=ma_agent_id)
    await _drop_draft(deps, actor=actor, agent_id=agent.id)


async def _write(
    deps: AgentChangeDeps,
    *,
    actor: AgentActor,
    agent: BetaManagedAgentsAgent,
    base: AgentConfigDoc,
    target: AgentConfigDoc,
    note: str | None,
    provider_revision: int,
) -> BetaManagedAgentsAgent:
    target = with_hidden_places(base, target, is_admin=actor.subject.is_admin)
    changes = diff_config(base, target)
    if not changes:
        raise AgentEditError("Nothing to apply", kind="conflict")
    refusal = unsupported(changes, a=base, b=target)
    if refusal is not None:
        raise AgentEditError(refusal, kind="invalid")
    rules = rule_changes(changes)
    if rules and not actor.subject.is_admin:
        raise AgentEditError("Only an admin can change tool rules.", kind="forbidden")
    patch = patch_for(agent, changes, target)
    before, after = place_edits(base.places), place_edits(target.places)
    # Checked before the first write; a rule or a missing right stops the whole change.
    await agent_editing.check_places(
        deps.edit, actor=actor, agent=agent, before=before, after=after
    )
    # Checked before the first write, written after the agent: nothing half-switched.
    switch = (
        await check_provider_switch(
            deps,
            actor=actor,
            agent=agent,
            provider=target.model.backend,
            model_id=target.model.id,
            expected_revision=provider_revision,
        )
        if provider_moved(base, target)
        else None
    )
    written = agent
    if patch.touched():
        written = await agent_editing.update_agent(
            deps.edit,
            actor=actor,
            ma_agent_id=agent.id,
            patch=patch,
            expected_version=agent.version,
        )
    await agent_editing.set_places(
        deps.edit, actor=actor, agent=written, before=before, after=after
    )
    if switch is not None:
        await write_provider_switch(deps, switch)
    servers = {server.key: server for server in target.mcp_servers}
    async with deps.edit.session_factory() as session:
        for server_key, tool in rules:
            server = servers[server_key]
            rule: Rule = next(t for t in server.tools if t.name == tool).rule
            await agent_tool_rules.set_rule(
                session,
                tenant_id=actor.tenant_id,
                agent_id=agent.id,
                agent_name=agent.name,
                tool_key=f"{server_key}/{tool}",
                server_url=server.url,
                rule=rule,
                set_by=str(actor.account_id),
                actor=actor.subject,
            )
        await drafts_store.record_version(
            session,
            tenant_id=actor.tenant_id,
            agent_id=agent.id,
            version=written.version,
            account_id=actor.account_id,
            note=note,
            changes=[change.model_dump(mode="json") for change in changes],
            config=_dump(target),
        )
        await session.commit()
    return written


async def apply_draft(
    deps: AgentChangeDeps, *, actor: AgentActor, ma_agent_id: str, note: str | None
) -> BetaManagedAgentsAgent:
    """Write the actor's draft as the agent's next version; refused if it went stale.

    A draft is stale when the provider's version moved, and also when places or tool
    rules did, which move without a new version.
    """
    async with _agent_edit_lock(deps, tenant_id=actor.tenant_id, agent_id=ma_agent_id):
        agent = await agent_editing.load_editable_agent(
            deps.edit, actor=actor, ma_agent_id=ma_agent_id
        )
        row = await _draft_row(deps, actor=actor, agent_id=agent.id)
        if row is None:
            raise AgentEditError("Nothing to apply", kind="conflict")
        if row.base_version != agent.version:
            raise _stale(agent)
        base = AgentConfigDoc.model_validate(row.base_config)
        facts = await _facts(deps, tenant_id=actor.tenant_id, agent=agent)
        if db_state(base) != stored_db_state(base, facts):
            raise _moved()
        written = await _write(
            deps,
            actor=actor,
            agent=agent,
            base=base,
            target=AgentConfigDoc.model_validate(row.config),
            note=(note or "").strip() or None,
            provider_revision=_provider_revision(facts),
        )
        await _drop_draft(deps, actor=actor, agent_id=agent.id)
        return written


async def revert(
    deps: AgentChangeDeps, *, actor: AgentActor, ma_agent_id: str, version: int
) -> BetaManagedAgentsAgent:
    """Write a recorded version's config as the agent's next version."""
    async with _agent_edit_lock(deps, tenant_id=actor.tenant_id, agent_id=ma_agent_id):
        agent = await agent_editing.load_editable_agent(
            deps.edit, actor=actor, ma_agent_id=ma_agent_id
        )
        async with deps.edit.session_factory() as session:
            row = await drafts_store.get_version(
                session, tenant_id=actor.tenant_id, agent_id=agent.id, version=version
            )
        if row is None:
            raise AgentEditError("No such version.", kind="not_found")
        facts = await _facts(deps, tenant_id=actor.tenant_id, agent=agent)
        base = config_of(facts)
        return await _write(
            deps,
            actor=actor,
            agent=agent,
            base=base,
            target=revert_target(base, AgentConfigDoc.model_validate(row.config)),
            note=f"Reverted to v{version}",
            provider_revision=_provider_revision(facts),
        )


async def apply_change(
    deps: AgentChangeDeps,
    *,
    actor: AgentActor,
    ma_agent_id: str,
    change: Callable[[AgentConfigDoc], AgentConfigDoc],
    note: str,
) -> BetaManagedAgentsAgent:
    """Write `change` of the agent's current config as its next version, like an apply."""
    async with _agent_edit_lock(deps, tenant_id=actor.tenant_id, agent_id=ma_agent_id):
        agent = await agent_editing.load_editable_agent(
            deps.edit, actor=actor, ma_agent_id=ma_agent_id
        )
        facts = await _facts(deps, tenant_id=actor.tenant_id, agent=agent)
        base = config_of(facts)
        return await _write(
            deps,
            actor=actor,
            agent=agent,
            base=base,
            target=change(base),
            note=note,
            provider_revision=_provider_revision(facts),
        )
