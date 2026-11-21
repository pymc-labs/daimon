"""Contracts of the feature system: the registry, the resolver and who may write rows."""

from __future__ import annotations

import ast
import uuid
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import get_args

import pytest
from daimon.core.capability_tools import CAPABILITY_KEYS
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    DiscordSettings,
    LongJobsSettings,
    OutboundSettings,
    ProgressPlansSettings,
    Settings,
)
from daimon.core.features.deployment import deployment_value
from daimon.core.features.model_input import HIDING_KEYS, SESSION_KEYS, TOOL_KEYS, SessionToolset
from daimon.core.features.registry import (
    FEATURES,
    TIER_ORDER,
    TIERS,
    WEB_MODULES,
    FeatureSpec,
    Requirement,
    Tier,
    parse_deployment,
    resolution_order,
)
from daimon.core.features.resolver import ResolvedFeature, TierValue, resolve_feature
from daimon.core.features.session_identity import IDENTITY_KEYS
from daimon.core.features.snapshot import FeaturePlace, FeatureSnapshot, may_turn_on
from daimon.core.features.tool_gate import WEB_APP_TOOLS, server_gate_key
from daimon.core.long_job_models import LongJobPolicy
from pydantic import PostgresDsn, SecretStr

_ROOT = Path(__file__).resolve().parents[4]
_PRODUCT = [
    path
    for base in (_ROOT / "packages/core/daimon", *(_ROOT / "packages/adapters").glob("*/daimon"))
    for path in base.rglob("*.py")
]
_WRITERS = {"set_feature_value", "clear_feature_value", "replace_agent_values"}
_MAY_WRITE = {
    "core/stores/feature_values.py",
    "core/channel_settings.py",
    "core/features/service.py",
}
_LEGACY_MODE = {"progress_plans", "thread_participation"}
_PLACE = FeaturePlace(uuid.UUID(int=1), "slack", "C1", "T1")


def _settings(**extra: object) -> Settings:
    return Settings.model_validate(
        {
            "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
            "anthropic": AnthropicSettings(api_key=SecretStr("test")),
            "discord": DiscordSettings(bot_token=SecretStr("test")),
            **extra,
        }
    )


@pytest.mark.parametrize("spec", FEATURES.values(), ids=list(FEATURES))
def test_every_feature_is_well_formed(spec: FeatureSpec) -> None:
    assert spec is FEATURES[spec.key]
    assert spec.default in spec.values
    tiers = spec.tiers
    assert list(tiers) == sorted(tiers, key=TIER_ORDER.index), "tiers broadest first"
    assert not spec.legacy_env or "deployment" in tiers


_PRE_AGENT = "read before the turn's agent is chosen, so it stops at the channel"
_NARROWED: dict[str, str] = {
    "approvals": "set in the access policy; only a live native platform admin changes it",
    "guidance": "provision: applied when an agent is saved or synced, with no channel or thread",
    "participation": _PRE_AGENT,
    "thread_naming": "read when a thread opens, before an agent or thread exists",
    "plain_text_mentions": _PRE_AGENT,
    "display_name": "a name belongs to one agent, so only the agent tier sets it",
    "thread_title_style": "read when a thread opens for its agent, before the thread exists",
    "thread_auto_archive": "read when a thread opens for its agent, before the thread exists",
    "status_reactions": "read per message and agent, before a thread setting could apply",
    "protected_notice_reaction": "read when a mention is refused, before a thread is chosen",
    "budget_notices": "read when a turn is refused for budget, before an agent or thread resolves",
    "cost_footer": "read by the finished-turn footer, which has no deployment settings or agent",
    "support_escalation": "set where credits apply: the workspace, an agent or a channel",
    "support_feedback": "set where credits apply: the workspace, an agent or a channel",
    "support_own_channel": "operator-only: whether a workspace may pick its own channel",
    "private_channel_messages": "operator-only: read once at startup",
    "direct_messages": "a workspace or agent policy for DMs, which have no channel or thread to choose",
    "dm_memory_read_only": "a workspace policy for DMs, which have no agent or thread to choose",
    "dm_approvals": "a workspace policy for DMs, which have no agent, channel or thread to choose",
    "dm_completion_pings": "a workspace policy for DMs, which have no channel or thread to choose",
    "thread_new": "read when /new runs, before an agent is chosen",
    "audience_hold": "a server admin's choice, read at admission once for the whole server",
    "dm_history_depth": "DMs have no channel or thread; an agent and its workspace set the memory",
    "dm_debounce_seconds": "read before any agent is chosen, so only the workspace sets it",
    "tool_safety": "operator-only security switch",
    "host_call_gate": "operator-only rollback switch",
    "mcp_connector_relay": "operator-only: sends connector data to another provider",
    "unattended_capabilities": "read for unattended runs, which have no channel or thread",
    "require_routine_destination": "read when a routine is created, at the workspace",
    "watch_approval": "an operator's choice for the team or one agent; a channel or thread cannot loosen it",
    "secret_sharing": "a workspace or agent choice: a share has no channel or thread",
    "long_jobs_max_usd": "checked when a job starts, from its workspace and agent",
    "long_jobs_max_deadline": "checked when a job starts, from its workspace and agent",
    "callback_watch_lifetime": "a server admin's limit, read when a callback watch is made",
    "daily_model_cap": "counts the team's spend, so a workspace or agent limit",
    "thread_providers": "an agent owner's consent, checked when a conversation's model is picked",
    "monitor_direct_messages": "kept per agent: a DM or web chat has no channel to set",
    "monitor_mcp": "kept per agent: an MCP call has no channel or thread",
    "routine_sandbox_tools": "an admin's opt-in for routine runs, so a workspace, agent or routine",
    "routine_result_header": "read when a routine posts, which has no thread; the routine sets its own",
    "model_reactions": "a workspace or agent display choice, read when the turn starts",
    "tool_server_names": "a workspace or agent display choice, read when the turn starts",
    "turn_time_limit": "a workspace or agent limit, read once when the turn starts",
    "unattended_time_limit": "a workspace or agent limit, read once when the run starts",
    **{f"web.{module}": "workspace-only web app surface" for module in WEB_MODULES},
}


def test_no_backend_narrows_what_hides_daimon_tools() -> None:
    """A switch preview reads these before it knows the target backend."""
    order = resolution_order(list(HIDING_KEYS))
    assert not [spec.key for spec in order if spec.backend_tiers or spec.backend_fixed]


def test_every_feature_has_all_five_tiers_unless_narrowed_for_a_reason() -> None:
    narrowed = {key for key, spec in FEATURES.items() if spec.tiers != TIERS}
    assert narrowed == set(_NARROWED), (
        f"narrowed without a reason: {sorted(narrowed - set(_NARROWED))}; "
        f"listed but at every tier: {sorted(set(_NARROWED) - narrowed)}"
    )


def test_most_specific_wins_and_says_which_tier_decided() -> None:
    plans = FEATURES["progress_plans"]
    got = resolve_feature(plans, [TierValue("workspace", "on"), TierValue("thread", "off")])
    assert (got.value, got.tier) == ("off", "thread")
    assert resolve_feature(plans, []).tier == "default"


def test_a_capability_off_above_is_a_ceiling() -> None:
    capability = replace(FEATURES["participation"], combine_override=None)
    values = [TierValue("workspace", "off"), TierValue("channel", "on")]
    got = resolve_feature(capability, values)
    assert (got.value, got.tier) == ("off", "workspace")
    kept = resolve_feature(FEATURES["participation"], values)
    assert kept.value == "on", "participation keeps its most-specific behaviour"


def test_a_lock_is_final_below() -> None:
    values = [TierValue("channel", "off", locked=True), TierValue("thread", "on")]
    got = resolve_feature(FEATURES["progress_plans"], values)
    assert (got.value, got.tier, got.locked) == ("off", "channel", True)


def test_stricter_and_minimum_combine_by_effect() -> None:
    guard = replace(FEATURES["cost_footer"], effect="guardrail", values={"a": "A", "b": "B"})
    picks = [TierValue("workspace", "b"), TierValue("channel", "a")]
    assert resolve_feature(replace(guard, default="a"), picks).value == "b", "later is stricter"
    limit = replace(guard, effect="limit", values={"5": "5", "9": "9"}, default="9")
    got = resolve_feature(limit, [TierValue("workspace", "5"), TierValue("channel", "9")])
    assert (got.value, got.tier) == ("5", "workspace")


def test_deployment_env_and_its_legacy_variable() -> None:
    assert parse_deployment("participation", "disabled") == ("off", True)
    assert parse_deployment("progress_plans", "on:locked") == ("on", True)
    with pytest.raises(ValueError, match="cost_footer"):
        parse_deployment("cost_footer", "hidden")
    plans = FEATURES["progress_plans"]
    new = deployment_value(_settings(features={"progress_plans": "on"}), plans)
    assert new == TierValue("deployment", "on")
    legacy = _settings(progress_plans=ProgressPlansSettings.model_validate({"mode": "disabled"}))
    assert deployment_value(legacy, plans) == TierValue("deployment", "off", True)
    assert deployment_value(_settings(), plans) is None


def test_a_limit_takes_the_operators_number_and_lower_tiers_only_its_steps() -> None:
    usd = FEATURES["long_jobs_max_usd"]
    assert parse_deployment("long_jobs_max_usd", "35.5") == ("35.5", False)
    for bad in ("-1", "nan", "lots", "0"):
        with pytest.raises(ValueError, match="long_jobs_max_usd"):
            parse_deployment("long_jobs_max_usd", bad)
    for bad in ("0", "0.5"):
        with pytest.raises(ValueError, match="long_jobs_max_deadline"):
            parse_deployment("long_jobs_max_deadline", bad)
    with pytest.raises(ValueError, match="takes"):
        parse_deployment("outbound_reach", "7")
    got = resolve_feature(usd, [TierValue("deployment", "35.5"), TierValue("workspace", "5")])
    assert (got.value, got.tier) == ("5", "workspace")
    assert resolve_feature(usd, [TierValue("workspace", "35")]).value == usd.default
    legacy = _settings(long_jobs=LongJobsSettings(max_usd=Decimal("7")))
    assert deployment_value(legacy, usd) == TierValue("deployment", "7")
    assert deployment_value(_settings(), usd) is None
    length = FEATURES["outbound_length"]
    off = _settings(outbound=OutboundSettings(max_length=0))
    assert deployment_value(off, length) is None, "0 means no limit, which a limit cannot say"


def test_a_limit_defaults_are_the_built_in_values() -> None:
    outbound, jobs = OutboundSettings(), LongJobsSettings()
    built_in = {
        "outbound_threads": outbound.max_threads,
        "outbound_places": outbound.max_places,
        "outbound_people": outbound.max_people,
        "outbound_length": outbound.max_length,
        "long_jobs_max_usd": LongJobPolicy().max_usd,
        "long_jobs_max_deadline": jobs.max_deadline_minutes,
    }
    for key, value in built_in.items():
        assert Decimal(FEATURES[key].default) == value, key


def _calls(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {
        getattr(node.func, "id", None) or getattr(node.func, "attr", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }


def test_rows_are_written_only_through_the_feature_service() -> None:
    found = {
        path.relative_to(path.parents[len(path.relative_to(_ROOT).parts) - 4]).as_posix()
        for path in _PRODUCT
        if _calls(path) & _WRITERS
    }
    assert {p.split("daimon/", 1)[-1] for p in found} <= _MAY_WRITE, "write via change_feature"


def test_legacy_mode_fields_are_read_only_by_the_deployment_tier() -> None:
    readers: set[str] = set()
    for path in _PRODUCT:
        for node in ast.walk(ast.parse(path.read_text())):
            inner = node.value if isinstance(node, ast.Attribute) else None
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "mode"
                and isinstance(inner, ast.Attribute)
                and inner.attr in _LEGACY_MODE
            ):
                readers.add(path.name)
    assert readers <= {"deployment.py"}


def _named_calls(path: Path, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call)
        and (getattr(node.func, "id", None) or getattr(node.func, "attr", "")) == name
    ]


_FULL_LISTINGS = {
    "adapters/web/features.py": "the web app hides the rows itself (Settings.tsx), same rule",
    "adapters/cli/commands/features.py": "the operator CLI lists every registered feature",
}


def _single_key(call: ast.Call) -> bool:
    keys = next((k.value for k in call.keywords if k.arg == "keys"), None)
    return isinstance(keys, ast.Tuple | ast.List) and len(keys.elts) == 1


def test_every_chat_listing_of_features_hides_the_not_set_up_ones() -> None:
    """A listing that reads many features passes them through `surface.listed`.

    A feature that is unavailable here with no saved value at any tier has nothing to show
    or set, so chat leaves it out as the web does. Single-key reads are not listings."""
    seen = 0
    for path in _PRODUCT:
        tree = ast.parse(path.read_text())
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            reads = [
                c
                for c in ast.walk(func)
                if isinstance(c, ast.Call)
                and getattr(c.func, "id", "") == "read_features"
                and not _single_key(c)
            ]
            if not reads or any(path.as_posix().endswith(rel) for rel in _FULL_LISTINGS):
                continue
            seen += 1
            filters = [
                n
                for n in ast.walk(func)
                if isinstance(n, ast.Call)
                and getattr(n.func, "id", "") in {"listed", "agent_listed"}
            ]
            assert filters, (
                f"{path.name}:{func.name} lists features without `listed()`; use it or add the "
                "file to _FULL_LISTINGS with a reason"
            )
    assert seen >= 3, "the chat listings moved; point this test at them"
    for rel in _FULL_LISTINGS:
        assert any(p.as_posix().endswith(rel) for p in _PRODUCT), f"{rel} moved: update the list"


def test_every_guidance_writer_asks_the_one_predicate() -> None:
    """Each writer of the guidance block decides with `guidance_applies`, never alone."""
    writers = {
        path: calls
        for path in _PRODUCT
        if (calls := _named_calls(path, "apply_credential_guidance"))
    }
    assert writers, "the guidance writers moved; point this test at them"
    for path, calls in writers.items():
        assert "guidance_applies" in _calls(path), f"{path.name} must use guidance_applies"
        for call in calls:
            assert any(k.arg == "guidance" for k in call.keywords), (
                f"{path.name}:{call.lineno} must pass guidance="
            )


def test_every_turn_origin_render_gets_the_turn_features() -> None:
    """Each turn's controls follow `web_awareness` at its place."""
    calls = [(p, c) for p in _PRODUCT for c in _named_calls(p, "render_turn_origin")]
    assert len(calls) >= 10, "the turn-origin renderers moved; point this test at them"
    for path, call in calls:
        spread = [k.value for k in call.keywords if k.arg is None]
        passes = any(k.arg == "features" for k in call.keywords) or any(
            isinstance(v, ast.Call) and getattr(v.func, "id", "") == "backend_facts" for v in spread
        )
        assert passes, f"{path.name}:{call.lineno} must pass features= or **backend_facts()"


_LINK_BUILDERS = {"panel_links", "for_tenant", "TenantLinks"}
_UNGATED_LINKS = {
    "core/web_links.py::panel_links": "a builder",
    "core/web_links.py::for_tenant": "a builder",
    "core/turn/deps.py::build_turn_deps": "a turn's links; `render_turn_origin` drops them",
    "core/turn/admission.py::_web_links": "same: the test above checks every render's features",
    "core/features/eval_key.py::sample_controls": "a fixed example turn for the eval, never sent",
}
# `aware_at` is `links_at` without links; `load_here_card` asks `links_at` at its answering
# agent; `_where_aware` asks `aware_at` at the MCP caller's places.
_LINK_GATES = {"links_at", "aware_at", "load_here_card", "_where_aware"}
# Tools in the web app's modules that stay with it Off, and why.
_WEB_KEPT = {
    "list_web_people": "allowances, owners and turn rules take the person ids it returns",
    "get_web_person": "same",
    "list_web_groups": "same, for groups",
    "change_workspace_settings": "turn rules aren't web-only; it refuses modules and the agent",
}


def _link_builders() -> list[str]:
    """`path::function` of each function that builds web links without asking a gate."""
    found = []
    for path in _PRODUCT:
        for fn in ast.walk(ast.parse(path.read_text())):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            calls = [node for node in ast.walk(fn) if isinstance(node, ast.Call)]
            names = {getattr(c.func, "id", None) or getattr(c.func, "attr", "") for c in calls}
            built = names & _LINK_BUILDERS or any(
                ast.unparse(c.func) == "WebLinks.from_settings" for c in calls
            )
            if built and not names & _LINK_GATES:
                found.append(f"{path.relative_to(_ROOT / 'packages').as_posix()}::{fn.name}")
    return found


def test_web_awareness_off_leaves_out_every_web_link_and_web_app_tool() -> None:
    """Every function that builds web links asks a gate (`links_at`: Off at its place, no
    link), and the web app's own tools, found from their modules, are hidden and refused while
    it is Off."""
    ungated = {item.split("/daimon/", 1)[-1] for item in _link_builders()}
    assert ungated == _UNGATED_LINKS.keys(), (
        f"web links built without `links_at`: {sorted(ungated - _UNGATED_LINKS.keys())}; gate "
        "them or list them with a reason"
    )
    modules = ("web_invites.py", "web_people.py", "web_workspace_settings.py")
    registered = {
        kw.value.value
        for path in _PRODUCT
        if path.name in modules
        for call in _named_calls(path, "tool")
        for kw in call.keywords
        if kw.arg == "name" and isinstance(kw.value, ast.Constant)
    }
    assert registered - _WEB_KEPT.keys() == WEB_APP_TOOLS, (
        "WEB_APP_TOOLS must be exactly the web app's own tools"
    )
    settings = next(p for p in _PRODUCT if p.as_posix().endswith("tools/web_workspace_settings.py"))
    assert "web_aware" in _calls(settings), "it refuses its web-only fields while Off"
    off = ResolvedFeature("web_awareness", "off", "thread")
    toolset = SessionToolset.of(FeatureSnapshot(_PLACE, 0, {"web_awareness": off}), {})
    assert toolset.hidden_daimon_tools >= WEB_APP_TOOLS, "a session is not offered them"
    assert all(server_gate_key(tool) == "web_awareness" for tool in WEB_APP_TOOLS), (
        "and the server refuses them"
    )


def test_every_origin_note_follows_origin_fragments() -> None:
    """Each turn's origin note (`context_prompt`) obeys `origin_fragments` at its place."""
    calls = [(p, c) for p in _PRODUCT for c in _named_calls(p, "context_prompt")]
    assert len(calls) >= 2, "the origin-note callers moved; point this test at them"
    for path, call in calls:
        assert any(k.arg == "fragments" for k in call.keywords), (
            f"{path.name}:{call.lineno} must pass fragments="
        )


_TURN_PATH = "core/turn/prepare.py"
# Built once and reused without the turn path's drift check, so their tools stay as built;
# the server gate still refuses a tool turned Off since, call by call.
_KEPT_AS_BUILT = {
    "core/headless_runner.py": "one session per routine fire, never reused",
    "adapters/cli/commands/sessions.py": "a CLI session its operator keeps",
    "adapters/mcp/tools/agent_chat.py": "`send_message` continues the `start_turn` session",
}


def test_every_session_creation_gets_the_place_toolset() -> None:
    """Every MA session, admitted or not (CLI, agent chat, routines), runs its place's tools."""
    calls = [
        (path, call)
        for path in _PRODUCT
        for call in _named_calls(path, "create_session")
        if isinstance(call.func, ast.Name)
    ]
    assert len(calls) >= 4, "the session creators moved; point this test at them"
    as_built = {
        p.relative_to(_ROOT / "packages").as_posix().split("/daimon/", 1)[-1] for p, _ in calls
    } - {_TURN_PATH}
    assert as_built == _KEPT_AS_BUILT.keys(), (
        "a session reused outside the turn path's drift check (`session_compat`) keeps its "
        "tools as built: list it with a reason and say so in docs/features.md"
    )
    for path, call in calls:
        assert any(k.arg == "toolset" for k in call.keywords), (
            f"{path.name}:{call.lineno} must pass toolset="
        )


def test_every_session_creation_gets_the_place_identity() -> None:
    """Every MA session is built with its place's prompt, skills and memory features."""
    calls = [
        (path, call)
        for path in _PRODUCT
        for call in _named_calls(path, "create_session")
        if isinstance(call.func, ast.Name)
    ]
    assert len(calls) >= 4, "the session creators moved; point this test at them"
    for path, call in calls:
        assert any(k.arg == "identity" for k in call.keywords), (
            f"{path.name}:{call.lineno} must pass identity="
        )


def test_session_bound_features_land_where_their_binding_says() -> None:
    """A session_mutable feature lands in the tools or `.env` hash, updated in place; a
    session_identity one is frozen in the session's snapshot (`SessionIdentity`)."""
    mutable = {key for key, spec in FEATURES.items() if spec.binds == "session_mutable"}
    assert mutable == set(SESSION_KEYS), "session_mutable features must shape SessionToolset"
    identity = {key for key, spec in FEATURES.items() if spec.binds == "session_identity"}
    assert identity == set(IDENTITY_KEYS), "session_identity features must shape SessionIdentity"


def test_a_dotted_feature_has_an_env_name() -> None:
    """`DAIMON_FEATURES__TOOL_BASH` sets `tool.bash`; shells allow no dots in names."""
    settings = _settings(features={"TOOL_BASH": "off:locked"})
    assert settings.features == {"tool.bash": "off:locked"}, "env name maps to the dotted key"


_KEY_READERS = {
    "feature_on": 0,
    "feature": 0,
    "may_turn_on": 1,
    "shown": 0,
    "_feature_on": 0,
    "place_feature": 3,
    "place_guardrail": 3,
    "feature_in": 3,
    "place_feature_or_none": 3,
    "feature_in_or_none": 3,
    "_chosen": 2,
    "deployment_on": 1,
}
"""Calls that read a feature key, and the position of the key among their arguments."""
_SNAPSHOT_READS = {"on", "value", "get"}


def _read_key(node: ast.AST) -> str | None:
    """The feature key `node` reads, when it is a known reader called with a literal key."""
    if isinstance(node, ast.Subscript) and getattr(node.value, "id", "") == "FEATURES":
        key = node.slice
    elif isinstance(node, ast.Call):
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", "")
        receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
        named = getattr(receiver, "id", None) or getattr(receiver, "attr", "") or ""
        snapshot = (name in _SNAPSHOT_READS and named.endswith("features")) or (
            name in {"on", "value"} and named == "found"
        )
        at = _KEY_READERS.get(name, 0 if snapshot else None)
        key = node.args[at] if at is not None and len(node.args) > at else None
    else:
        return None
    return key.value if isinstance(key, ast.Constant) and isinstance(key.value, str) else None


def test_every_feature_key_read_is_registered_and_every_registered_key_is_read() -> None:
    """A typo in a read fails here, and so does a registered control nothing obeys."""
    read: dict[str, str] = {}
    # Read through a list or a lookup, so no reader call names them.
    named: set[str] = (
        set(TOOL_KEYS)
        | set(IDENTITY_KEYS)
        | set(CAPABILITY_KEYS)
        | {f"web.{module}" for module in WEB_MODULES}
        | {"daily_model_cap"}  # request.admission.resolved("daily_model_cap")
        | {"display_name"}  # Admission.display_name resolves it
    )
    for path in _PRODUCT:
        constants: set[str] = set()
        has_reader = False
        for node in ast.walk(ast.parse(path.read_text())):
            if (key := _read_key(node)) is not None:
                read.setdefault(key, f"{path.name}:{getattr(node, 'lineno', 0)}")
                has_reader = True
            if (
                path.name != "registry.py"
                and isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in FEATURES
            ):
                constants.add(node.value)
        # A bare string only counts in a file that reads some feature.
        if has_reader:
            named |= constants
    assert len(read) >= 10, "the feature readers moved; point this test at them"
    unknown = {key: where for key, where in read.items() if key not in FEATURES}
    assert not unknown, f"feature keys read but not registered: {unknown}"
    assert set(FEATURES) <= named, f"registered but never read: {set(FEATURES) - named}"


_OPS = "operator switch for security, billing or a deployment, not a per-place control"
_SERVICE = "turns a whole service on in this deployment; per-place control is a feature"
_LEGACY = "legacy variable the deployment tier reads in place of DAIMON_FEATURES__*"
_ROLLOUT = "staged rollout of one behaviour, removed once it is the only behaviour"
_SHAPE = "describes a configured object, not something a place turns on or off"
_CONFIG_SWITCHES: dict[str, str] = {
    "McpSettings.allow_legacy_tokens": _OPS,
    "McpSettings.require_routine_destination": _LEGACY,
    "McpOAuthClientSettings.send_resource": _OPS,
    "ProgressPlansSettings.mode": _LEGACY,
    "McpContextSettings.enabled": _LEGACY,
    "ThreadParticipationSettings.mode": _LEGACY,
    "RepoMemorySettings.enabled": _LEGACY,
    "DiscordSettings.credential_form_expiry": _ROLLOUT,
    "TeamsSettings.enabled": _SERVICE,
    "WebSettings.manual_topup": _OPS,
    "WebSettings.dev": _OPS,
    "GithubSettings.mcp_router": _SERVICE,
    "CryptoSettings.allow_plaintext": _OPS,
    "HarnessAuthBinding.auth_mode": _SHAPE,
    "SignedIdentityServer.requester_meta": _SHAPE,
    "SignedIdentityServer.channel_scoped": _OPS,  # how a server scopes its answers, a per-server trust setting
    "SelfHostedSettings.admission_enabled": _OPS,
    "SelfHostedSettings.single_tenant": _OPS,
    "NotebookSettings.allow_editable": _OPS,
    "TurnSettings.channel_backends": _ROLLOUT,
    "TurnSettings.agent_backends": _ROLLOUT,
    "ThreadNamingSettings.enabled": _LEGACY,
    "OutboundSettings.reach_budget_enabled": _LEGACY,
    "PrivateChannelMessagesSettings.enabled": _LEGACY,
    "PrivacySettings.delete_enabled": _OPS,
    "DirectMessagePolicy.mode": _OPS,
    "AgentIdentitySettings.enabled": _SERVICE,
    "TurnQueueSettings.enabled": _SERVICE,
    "PollableTool.returns_chat": _SHAPE,
    "WatchesSettings.enabled": _LEGACY,
    "ComputeSettings.enabled": _LEGACY,
}
"""Every on/off or mode field `config.py` already has, and why it is not a feature."""


def test_config_grows_no_new_product_switch() -> None:
    """A new behaviour a place might turn on or off is a registry feature, not a setting."""
    tree = ast.parse((_ROOT / "packages/core/daimon/core/config.py").read_text())
    switches = {
        f"{cls.name}.{field.target.id}"
        for cls in tree.body
        if isinstance(cls, ast.ClassDef)
        for field in cls.body
        if isinstance(field, ast.AnnAssign)
        and isinstance(field.target, ast.Name)
        and (
            ast.unparse(field.annotation) in ("bool", "bool | None")
            or field.target.id == "mode"
            or field.target.id.endswith("_mode")
            or ast.unparse(field.annotation).endswith("Mode")
        )
    }
    assert switches - set(_CONFIG_SWITCHES) == set(), (
        "new on/off or mode setting: register a feature instead, or allow-list it with a reason"
    )
    assert set(_CONFIG_SWITCHES) - switches == set(), "allow-listed settings that no longer exist"


def test_may_turn_on_is_false_only_when_nothing_narrower_could_turn_it_on() -> None:
    assert may_turn_on(_settings(), "participation", "discord"), (
        "default Off can still be turned on"
    )
    locked = _settings(features={"participation": "off:locked"})
    assert not may_turn_on(locked, "participation", "discord"), "a deployment lock keeps it Off"
    assert may_turn_on(
        _settings(features={"participation": "on:locked"}), "participation", "discord"
    ), "a lock On is not a bar"
    assert not may_turn_on(_settings(), "participation", "slack"), "no adapter, no feature"


def test_repo_memory_needs_the_setup_and_the_setting_does_not_read_the_feature() -> None:
    import uuid

    from daimon.core.config import RepoMemorySettings, get_repo_memory_settings
    from daimon.core.features.snapshot import FeaturePlace, resolve_one

    place = FeaturePlace(uuid.UUID(int=0), "discord")
    spec = FEATURES["repo_memory"]
    bare = _settings(features={"repo_memory": "on"})
    assert resolve_one(spec, bare, place, ()).unavailable == "requires", "on without the setup"
    assert not get_repo_memory_settings(bare).enabled, "the setup ignores the feature"
    setup = _settings(repo_memory=RepoMemorySettings(enabled=True))
    assert resolve_one(spec, setup, place, ()).on, "set up: on by default"
    off = _settings(repo_memory=RepoMemorySettings(enabled=True), features={"repo_memory": "off"})
    assert get_repo_memory_settings(off).enabled, "the feature does not undo the setup"
    assert not resolve_one(spec, off, place, ()).on, "but turns the feature off"


def test_a_zero_post_length_means_no_limit_and_never_overrides_a_narrower_tier() -> None:
    """0 is accepted from the operator, as the legacy variable was, and reads as the loosest
    value: it lifts no workspace limit, and two limits combine to the smaller real one."""
    spec = FEATURES["outbound_length"]
    assert parse_deployment("outbound_length", "0") == ("0", False), "boot takes 0"
    assert deployment_value(_settings(features={"outbound_length": "0"}), spec) == TierValue(
        "deployment", "0"
    ), "a deployment that sets 0 starts"
    assert spec.accepts("0", "deployment"), "the operator's tier takes 0"
    for tier in ("workspace", "channel"):
        assert not spec.accepts("0", tier), f"a step, not 0, at {tier}"
    alone = resolve_feature(spec, [TierValue("deployment", "0")])
    assert (alone.value, alone.tier) == ("0", "deployment"), "0 alone stays no limit"
    narrowing: list[tuple[str, Tier]] = [("500", "workspace"), ("250", "channel")]
    for narrower, tier in narrowing:
        got = resolve_feature(spec, [TierValue("deployment", "0"), TierValue(tier, narrower)])
        assert (got.value, got.tier) == (narrower, tier), f"0 does not override {tier}"
    both = resolve_feature(spec, [TierValue("deployment", "1000"), TierValue("workspace", "500")])
    assert both.value == "500", "real limits still take the smallest"


@pytest.mark.parametrize(
    ("key", "bad", "minimum"),
    [
        ("long_jobs_max_usd", "0", "0.01"),
        ("long_jobs_max_usd", "0.001", "0.01"),
        ("long_jobs_max_deadline", "0", "1"),
        ("long_jobs_max_deadline", "0.5", "1"),
    ],
)
def test_a_limit_below_its_minimum_is_refused_naming_the_minimum(
    key: str, bad: str, minimum: str
) -> None:
    with pytest.raises(ValueError, match=f"at least {minimum}"):
        parse_deployment(key, bad)


@pytest.mark.parametrize("key", ["outbound_threads", "outbound_places", "outbound_people"])
def test_a_reach_limit_applies_only_while_the_reach_check_is_on(key: str) -> None:
    """The limits are read only by the reach check, so with it Off they are unavailable."""
    from daimon.core.features.resolver import ResolvedFeature
    from daimon.core.features.snapshot import FeaturePlace, resolve_one

    place = FeaturePlace(uuid.UUID(int=0), "discord")
    spec = FEATURES[key]
    for value, reason in (("on", None), ("off", "depends_on")):
        check = {"reach_budget": ResolvedFeature("reach_budget", value, "workspace")}
        found = resolve_one(spec, None, place, (), check)
        assert found.unavailable == reason, f"{key} with the reach check {value}"


def test_resolution_order_puts_every_dependency_before_its_dependents() -> None:
    """The readers resolve in this order; a dependency read later would never apply."""
    for spec in FEATURES.values():
        assert all(dep in FEATURES for dep in spec.depends_on), f"{spec.key} names a real feature"
    every = [spec.key for spec in resolution_order()]
    assert sorted(every) == sorted(FEATURES), "every feature is resolved once"
    for spec in resolution_order():
        for dep in spec.depends_on:
            assert every.index(dep) < every.index(spec.key), f"{dep} before {spec.key}"
    alone = [spec.key for spec in resolution_order(["outbound_threads"])]
    assert alone == ["reach_budget", "outbound_threads"], "a read of one pulls in its dependency"


def test_every_dependency_and_requirement_names_something_real() -> None:
    known = set(get_args(Requirement))
    for spec in FEATURES.values():
        for dep in spec.depends_on:
            assert dep in FEATURES, f"{spec.key} depends on {dep}, which is no feature"
            assert dep != spec.key, f"{spec.key} depends on itself"
        for need in spec.requires:
            assert need in known, f"{spec.key} requires {need}, which is no known requirement"
    used = {need for spec in FEATURES.values() for need in spec.requires}
    assert used <= known, "every requirement in use is declared"


def test_the_dependency_graph_has_no_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    state: dict[str, str] = {}

    def visit(key: str, path: tuple[str, ...]) -> None:
        assert state.get(key) != "open", f"cycle: {' -> '.join((*path, key))}"
        if state.get(key) == "done":
            return
        state[key] = "open"
        for dep in FEATURES[key].depends_on:
            visit(dep, (*path, key))
        state[key] = "done"

    for key in FEATURES:
        visit(key, ())
    spec = FEATURES["reach_budget"]
    monkeypatch.setitem(
        FEATURES,  # type: ignore[arg-type]
        "reach_budget",
        replace(spec, depends_on=("outbound_threads",)),
    )
    with pytest.raises(ValueError, match="dependency cycle"):
        resolution_order()


@pytest.mark.parametrize("key", ["turn_time_limit", "unattended_time_limit"])
def test_time_limit_text_matches_its_longest_choice(key: str) -> None:
    spec = FEATURES[key]
    assert "45 minutes" in spec.values.values(), "the longest choice the text names"
    assert "hours" not in spec.doc, "the text names no limit the choices lack"
