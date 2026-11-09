"""The session toolset reaches every session path: MA tools and servers, the drift hash,
self-hosted turn tools, and the agent tier written through the agent's own config."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.core.capability_tools import CAPABILITY_KEYS, CAPABILITY_TOOLS
from daimon.core.channel_changes import Expected
from daimon.core.config import AnthropicSettings, DatabaseSettings, DiscordSettings, Settings
from daimon.core.errors import StaleChannelValue
from daimon.core.features.model_input import (
    DEFAULT_TOOLSET,
    SessionToolset,
    agent_toolset,
    builtin_enabled,
)
from daimon.core.features.registry import FEATURES
from daimon.core.features.resolver import ResolvedFeature
from daimon.core.features.service import (
    FeatureActor,
    change_feature,
    feature_refusal,
    read_features,
)
from daimon.core.features.snapshot import FeaturePlace, FeatureSnapshot, resolve_one
from daimon.core.repo_memory_tools import REPO_MEMORY_TOOLS
from daimon.core.session_snapshot import desired_snapshot, session_mcp_servers, session_tools
from daimon.core.stores.feature_values import place_values
from daimon.core.tool_safety import OPEN_TOOL_SAFETY
from daimon.core.turn.selfhosted_tools import build_turn_tools
from daimon.testing.factories import make_tenant
from daimon.testing.ma_models import ma_agent
from pydantic import PostgresDsn, SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SETTINGS = Settings.model_validate(
    {
        "database": DatabaseSettings(url=PostgresDsn("postgresql+asyncpg://u:p@localhost/d")),
        "anthropic": AnthropicSettings(api_key=SecretStr("test")),
        "discord": DiscordSettings(bot_token=SecretStr("test")),
    }
)
PUBLIC = "https://daimon.example/mcp"


def _agent(*, isolated: bool = False) -> BetaManagedAgentsAgent:
    allow = {"enabled": True, "permission_policy": {"type": "always_allow"}}
    tools: list[Any] = [
        {
            "type": "agent_toolset_20260401",
            "default_config": allow,
            "configs": [{"name": "bash", **allow}],
        },
        {
            "type": "mcp_toolset",
            "mcp_server_name": "daimon-mcp",
            "default_config": allow,
            "configs": [],
        },
    ]
    return ma_agent(
        id="agent_tools",
        tools=tools,
        mcp_servers=[{"name": "daimon-mcp", "type": "url", "url": PUBLIC}],
        metadata={"daimon_isolated": "true"} if isolated else None,
    )


def _dicts(tools: Any) -> list[Any]:
    return [tool.model_dump(mode="json") for tool in tools]


def _snapshot(**values: str) -> FeatureSnapshot:
    place = FeaturePlace(uuid.uuid4(), "web", agent_id="agent_tools", backend="anthropic")
    features = {key: resolve_one(spec, None, place, []) for key, spec in FEATURES.items()}
    for key, value in values.items():
        name = key.replace("__", ".")
        features[name] = features[name].__class__(name, value, "channel")
    return FeatureSnapshot(place, 1, features)


def test_daimon_tools_off_drops_the_server_and_its_toolset() -> None:
    """Off leaves `daimon-mcp` out of both arrays, as a hidden server is."""
    agent, off = _agent(), SessionToolset(daimon_tools=False)
    tools = session_tools(
        agent, frozenset(), tool_safety=OPEN_TOOL_SAFETY, public_url=PUBLIC, toolset=off
    )
    servers = session_mcp_servers(
        agent, frozenset(), tool_safety=OPEN_TOOL_SAFETY, public_url=PUBLIC, toolset=off
    )
    assert all(t.get("mcp_server_name") != "daimon-mcp" for t in _dicts(tools)), "toolset dropped"
    assert [s.name for s in servers] == [], "the Daimon server is dropped"


def test_a_place_override_rewrites_the_builtin_config() -> None:
    """A channel turning bash off reaches the session's toolset config."""
    agent = _agent()
    toolset = agent_toolset(_snapshot(tool__bash="off"), agent)
    assert toolset == SessionToolset(builtins={"bash": False}), "the channel decides bash"
    tools = session_tools(
        agent, frozenset(), tool_safety=OPEN_TOOL_SAFETY, public_url=PUBLIC, toolset=toolset
    )
    assert builtin_enabled(_dicts(tools), "bash") is False, "bash is off in the session"  # type: ignore[arg-type]
    grep_off = SessionToolset(builtins={"grep": False})
    tools = session_tools(
        agent, frozenset(), tool_safety=OPEN_TOOL_SAFETY, public_url=PUBLIC, toolset=grep_off
    )
    assert builtin_enabled(_dicts(tools), "grep") is False, "an unlisted tool gains a config"  # type: ignore[arg-type]


def test_turning_off_a_tool_the_agent_lacks_adds_nothing() -> None:
    """An agent with no toolset keeps none when a place turns a builtin off."""
    assert SessionToolset(builtins={"bash": False}).apply_builtins([]) == [], "nothing added"


def test_isolated_agents_never_get_daimon_tools() -> None:
    """Isolation is fixed: Daimon tools stay off whatever the places say."""
    assert not agent_toolset(_snapshot(), _agent(isolated=True)).daimon_tools, "isolated: none"
    assert agent_toolset(None, _agent(isolated=True)) == DEFAULT_TOOLSET, "hand-built: default"


def test_the_drift_hash_follows_the_toolset() -> None:
    """A changed toolset reads as drift, so the session is updated in place next turn."""

    def snapshot(toolset: SessionToolset) -> Any:
        return desired_snapshot(
            _agent(),
            hidden_mcp_server_names=frozenset(),
            environment_id="env",
            env_sha256=None,
            repo_url=None,
            repo_branch=None,
            memory_store_id=None,
            vault_id=None,
            tool_safety=OPEN_TOOL_SAFETY,
            public_url=PUBLIC,
            toolset=toolset,
        )

    before, after = snapshot(DEFAULT_TOOLSET), snapshot(SessionToolset(builtins={"bash": False}))
    assert before.tools_sha256 != after.tools_sha256, "tools drift with the toolset"
    assert before.system_sha256 == after.system_sha256, "the system is untouched"
    off = snapshot(SessionToolset(daimon_tools=False))
    assert before.mcp_servers_sha256 != off.mcp_servers_sha256, "servers drift with Daimon tools"


def test_repo_memory_off_turns_its_tools_off() -> None:
    """A place turning repository memory off turns its four tools off on `daimon-mcp`."""
    agent = _agent()
    toolset = agent_toolset(_snapshot(repo_memory="off"), agent)
    assert toolset.hidden_daimon_tools == REPO_MEMORY_TOOLS, "the place hides the memory tools"
    tools = session_tools(
        agent, frozenset(), tool_safety=OPEN_TOOL_SAFETY, public_url=PUBLIC, toolset=toolset
    )
    daimon = next(t for t in _dicts(tools) if t.get("mcp_server_name") == "daimon-mcp")
    off = {c["name"] for c in daimon["configs"] if c.get("enabled") is False}
    assert off == REPO_MEMORY_TOOLS, "each memory tool is turned off in the session"


def test_agent_env_file_off_leaves_the_keys_file_out() -> None:
    assert agent_toolset(_snapshot(), _agent()).env_file
    toolset = agent_toolset(_snapshot(agent_env_file="off"), _agent())
    assert not toolset.env_file and toolset.changes


def test_repo_memory_off_at_the_deployment_hides_nothing() -> None:
    """Off at the deployment registers no memory tools, so the session's tools are unchanged."""
    base = _snapshot()
    features = {**base.features, "repo_memory": ResolvedFeature("repo_memory", "off", "deployment")}
    snapshot = FeatureSnapshot(base.place, 1, features)
    assert agent_toolset(snapshot, _agent()) == DEFAULT_TOOLSET, "nothing to hide"


def test_capabilities_change_nothing_at_their_defaults() -> None:
    """No tier set: every capability's tools stay offered, set up or not."""
    assert agent_toolset(_snapshot(), _agent()) == DEFAULT_TOOLSET, "byte-identical by default"


@pytest.mark.parametrize("key", CAPABILITY_KEYS)
def test_a_capability_off_turns_exactly_its_tools_off(key: str) -> None:
    agent = _agent()
    toolset = agent_toolset(_snapshot(**{key: "off"}), agent)
    assert toolset.hidden_daimon_tools == CAPABILITY_TOOLS[key]
    tools = session_tools(
        agent, frozenset(), tool_safety=OPEN_TOOL_SAFETY, public_url=PUBLIC, toolset=toolset
    )
    daimon = next(t for t in _dicts(tools) if t.get("mcp_server_name") == "daimon-mcp")
    assert {c["name"] for c in daimon["configs"] if c.get("enabled") is False} == (
        CAPABILITY_TOOLS[key]
    ), "each tool is off in the session"


def test_a_capability_off_at_the_deployment_hides_its_tools_too() -> None:
    """The server registers these tools whatever the operator chose, so Off must hide them."""
    base = _snapshot()
    features = {**base.features, "lead_intake": ResolvedFeature("lead_intake", "off", "deployment")}
    toolset = agent_toolset(FeatureSnapshot(base.place, 1, features), _agent())
    assert toolset.hidden_daimon_tools == CAPABILITY_TOOLS["lead_intake"]


def test_a_capability_without_setup_is_unavailable_with_a_reason() -> None:
    place = FeaturePlace(uuid.uuid4(), "web")
    off = resolve_one(FEATURES["notebooks"], SETTINGS, place, [])
    assert (off.on, off.unavailable) == (False, "requires"), "no notebook host set up"
    on = resolve_one(FEATURES["lead_intake"], SETTINGS, place, [])
    assert on.on, "lead intake needs no setup"
    ready = SETTINGS.model_copy(
        update={
            "notebook": SETTINGS.notebook.model_copy(
                update={"host_url": "http://nb:8001", "admin_secret": SecretStr("s" * 32)}
            )
        }
    )
    assert resolve_one(FEATURES["notebooks"], ready, place, []).on


def test_a_capability_without_setup_hides_its_tools_and_only_those() -> None:
    """Display and behaviour agree: unmet setup resolves Off and the tools are not offered."""
    place = FeaturePlace(uuid.uuid4(), "web", agent_id="agent_tools", backend="anthropic")
    features = {key: resolve_one(spec, SETTINGS, place, []) for key, spec in FEATURES.items()}
    toolset = agent_toolset(FeatureSnapshot(place, 1, features), _agent())
    unset = {"notebooks", "report_publish", "media", "microsoft_365", "compute"}
    assert toolset.off_capabilities == unset, "exactly the features whose setup is missing"
    for key in ("watches", "github", "routines", "long_jobs", "lead_intake"):
        assert features[key].on, f"{key} has no setup condition its tools share"


def test_support_escalation_needs_the_platforms_own_channel() -> None:
    support = SETTINGS.support.model_copy(
        update={"slack_escalation_channel_id": "C123", "credits_per_user": 5}
    )
    ready = SETTINGS.model_copy(update={"support": support})
    tenant = uuid.uuid4()
    slack = resolve_one(FEATURES["support_escalation"], ready, FeaturePlace(tenant, "slack"), [])
    teams = resolve_one(FEATURES["support_escalation"], ready, FeaturePlace(tenant, "teams"), [])
    assert slack.on, "Slack has its channel"
    assert (teams.on, teams.unavailable) == (False, "requires"), "Teams has none"


def test_the_old_enabled_variables_are_the_deployment_tier() -> None:
    place = FeaturePlace(uuid.uuid4(), "web")
    untouched = resolve_one(FEATURES["watches"], SETTINGS, place, [])
    assert untouched.tier == "default", "an unset variable decides nothing"
    for enabled in (False, True):
        set_to = SETTINGS.model_copy(update={"watches": type(SETTINGS.watches)(enabled=enabled)})
        got = resolve_one(FEATURES["watches"], set_to, place, [])
        assert (got.on, got.tier) == (enabled, "deployment"), "the variable is the deployment tier"


def test_selfhosted_turn_tools_leave_out_the_daimon_server() -> None:
    """Self-hosted turns get no Daimon server while Daimon tools are off."""
    common: dict[str, Any] = {
        "daimon_mcp_url": "https://daimon.example/mcp",
        "tenant_id": uuid.uuid4(),
        "platform": "web",
        "channel_id": "c1",
        "agent_names": ("a",),
        "skill_bundles": {},
    }
    assert [s.name for s in build_turn_tools(**common).mcp_servers] == ["daimon-mcp"], "default"
    assert build_turn_tools(**common, daimon_tools=False).mcp_servers == (), "off: no server"


async def test_agent_tier_tool_writes_the_agent_config_not_a_row(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """`change_feature` routes a `tool.*` agent tier to the agent's own config."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    written: list[tuple[str, bool]] = []

    async def write(tool: str, enabled: bool) -> bool | None:
        written.append((tool, enabled))
        return True

    async def admin(_: object) -> bool:
        return True

    actor = FeatureActor(tenant.id, None, "web", None, admin, write_builtin=write)
    place = FeaturePlace(tenant.id, "web", agent_id="agent_tools")
    change = await change_feature(
        db_session_factory, SETTINGS, actor, key="tool.bash", tier="agent", value="off", place=place
    )
    assert written == [("bash", False)], "the agent's own config is written"
    assert change.before.value == "on", "before is what the agent had"
    async with db_session_factory() as session:
        rows = await place_values(
            session, tenant_id=tenant.id, agent_id="agent_tools", channel_id=None, thread_key=None
        )
    assert rows == [], "no second copy is stored"
    with pytest.raises(Exception, match="edited"):
        await change_feature(
            db_session_factory,
            SETTINGS,
            FeatureActor(tenant.id, None, "web", None, admin),
            key="tool.bash",
            tier="agent",
            value="off",
            place=place,
        )


def test_daimon_tools_is_fixed_on_for_gemini_and_openai() -> None:
    """Gemini and OpenAI keep Daimon's tools, so the feature says so instead of reading Off."""
    spec = FEATURES["daimon_tools"]
    for backend in ("gemini", "openai"):
        place = FeaturePlace(uuid.uuid4(), "discord", "c1", backend=backend)
        got = resolve_one(spec, None, place, [])
        assert (got.value, got.unavailable) == ("on", "backend"), f"{backend} keeps the tools"
        refusal = feature_refusal(
            "daimon_tools", "channel", "off", locked=False, place=place, settings=SETTINGS
        )
        assert refusal is not None and "always On" in str(refusal), "the control says why"


async def test_tool_agent_tier_is_read_from_the_agent_and_checks_expected(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """`read_features` shows the agent's own tool config; a stale `expected` writes nothing."""
    tenant = await make_tenant(db_session)
    await db_session.commit()
    place = FeaturePlace(tenant.id, "web", agent_id="agent_tools")
    (view,) = await read_features(
        db_session_factory, SETTINGS, place, keys=("tool.bash",), agent_builtins={"bash": False}
    )
    agent_tier = view.own("agent")
    assert agent_tier is not None and agent_tier.value == "off", "agent tier from its config"
    assert (view.resolved.value, view.resolved.tier) == ("off", "agent")
    written: list[tuple[str, bool]] = []

    async def write(tool: str, enabled: bool) -> bool | None:
        written.append((tool, enabled))
        return False

    async def read() -> dict[str, bool | None]:
        return {"bash": False}

    async def admin(_: object) -> bool:
        return True

    actor = FeatureActor(
        tenant.id, None, "web", None, admin, write_builtin=write, read_builtins=read
    )
    with pytest.raises(StaleChannelValue):
        await change_feature(
            db_session_factory,
            SETTINGS,
            actor,
            key="tool.bash",
            tier="agent",
            value="on",
            place=place,
            expected=Expected("on"),
        )
    assert written == [], "a stale read changes nothing"
    await change_feature(
        db_session_factory,
        SETTINGS,
        actor,
        key="tool.bash",
        tier="agent",
        value="on",
        place=place,
        expected=Expected("off"),
    )
    assert written == [("bash", True)], "a current read writes"
