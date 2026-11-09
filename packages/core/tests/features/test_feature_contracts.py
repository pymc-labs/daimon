"""Contracts of the feature system: the registry, the resolver and who may write rows."""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest
from daimon.core.config import (
    AnthropicSettings,
    DatabaseSettings,
    DiscordSettings,
    ProgressPlansSettings,
    Settings,
)
from daimon.core.features.deployment import deployment_value
from daimon.core.features.model_input import SESSION_KEYS, TOOL_KEYS
from daimon.core.features.registry import (
    FEATURES,
    TIERS,
    WEB_MODULES,
    FeatureSpec,
    parse_deployment,
)
from daimon.core.features.resolver import TierValue, resolve_feature
from daimon.core.features.session_identity import IDENTITY_KEYS
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
    "core/stores/_scope_modes.py",
    "core/features/service.py",
}
_LEGACY_MODE = {"progress_plans", "thread_participation"}


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
    assert list(tiers) == sorted(tiers, key=TIERS.index), "tiers broadest first"
    assert not spec.legacy_env or "deployment" in tiers


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
    """Each turn's controls follow `turn_controls` and `web_awareness` at its place."""
    calls = [(p, c) for p in _PRODUCT for c in _named_calls(p, "render_turn_origin")]
    assert len(calls) >= 10, "the turn-origin renderers moved; point this test at them"
    for path, call in calls:
        spread = [k.value for k in call.keywords if k.arg is None]
        passes = any(k.arg == "features" for k in call.keywords) or any(
            isinstance(v, ast.Call) and getattr(v.func, "id", "") == "backend_facts" for v in spread
        )
        assert passes, f"{path.name}:{call.lineno} must pass features= or **backend_facts()"


def test_every_origin_note_follows_origin_fragments() -> None:
    """Each turn's origin note (`context_prompt`) obeys `origin_fragments` at its place."""
    calls = [(p, c) for p in _PRODUCT for c in _named_calls(p, "context_prompt")]
    assert len(calls) >= 2, "the origin-note callers moved; point this test at them"
    for path, call in calls:
        assert any(k.arg == "fragments" for k in call.keywords), (
            f"{path.name}:{call.lineno} must pass fragments="
        )


def test_every_session_creation_gets_the_place_toolset() -> None:
    """Every MA session, admitted or not (CLI, agent chat, routines), runs its place's tools."""
    calls = [
        (path, call)
        for path in _PRODUCT
        for call in _named_calls(path, "create_session")
        if isinstance(call.func, ast.Name)
    ]
    assert len(calls) >= 4, "the session creators moved; point this test at them"
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
    "switch_default": 1,
    "shown": 0,
    "_feature_on": 0,
    "place_feature": 3,
    "feature_in": 3,
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
        snapshot = name in _SNAPSHOT_READS and named.endswith("features")
        at = _KEY_READERS.get(name, 0 if snapshot else None)
        key = node.args[at] if at is not None and len(node.args) > at else None
    else:
        return None
    return key.value if isinstance(key, ast.Constant) and isinstance(key.value, str) else None


def test_every_feature_key_read_is_registered_and_every_registered_key_is_read() -> None:
    """A typo in a read fails here, and so does a registered control nothing obeys."""
    read: dict[str, str] = {}
    # Both are built from a list, so no literal names them.
    named: set[str] = set(TOOL_KEYS) | {f"web.{module}" for module in WEB_MODULES}
    for path in _PRODUCT:
        for node in ast.walk(ast.parse(path.read_text())):
            if (key := _read_key(node)) is not None:
                read.setdefault(key, f"{path.name}:{getattr(node, 'lineno', 0)}")
            if (
                path.name != "registry.py"
                and isinstance(node, ast.Constant)
                and node.value in FEATURES
            ):
                named.add(node.value)
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
    "McpSettings.require_routine_destination": _OPS,
    "McpOAuthClientSettings.send_resource": _OPS,
    "ProgressPlansSettings.mode": _LEGACY,
    "McpContextSettings.enabled": _LEGACY,
    "ThreadParticipationSettings.mode": _LEGACY,
    "RepoMemorySettings.enabled": _LEGACY,
    "DiscordSettings.writers_none_notice": _LEGACY,
    "DiscordSettings.credential_form_expiry": _ROLLOUT,
    "DiscordSettings.unmentioned_reply_hint": _LEGACY,
    "DiscordSettings.tenant_cap_waiting_card": _LEGACY,
    "TeamsSettings.enabled": _SERVICE,
    "WebSettings.manual_topup": _OPS,
    "WebSettings.dev": _OPS,
    "GithubSettings.mcp_router": _SERVICE,
    "CryptoSettings.allow_plaintext": _OPS,
    "HarnessAuthBinding.auth_mode": _SHAPE,
    "SelfHostedSettings.admission_enabled": _OPS,
    "SelfHostedSettings.single_tenant": _OPS,
    "NotebookSettings.allow_editable": _OPS,
    "TurnSettings.native_footer_cost": _LEGACY,
    "TurnSettings.channel_backends": _ROLLOUT,
    "TurnSettings.agent_backends": _ROLLOUT,
    "TurnSettings.status_log": _ROLLOUT,
    "ThreadNamingSettings.enabled": _LEGACY,
    "OutboundSettings.reach_budget_enabled": _OPS,
    "PrivateChannelMessagesSettings.enabled": _LEGACY,
    "PrivacySettings.delete_enabled": _OPS,
    "ArtifactsSettings.embed_images": _LEGACY,
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
