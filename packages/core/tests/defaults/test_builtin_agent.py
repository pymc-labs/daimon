"""Every path resolves the built-in name by one rule: seeded first, never a workspace's own."""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

import pytest
from anthropic import AsyncAnthropic
from daimon.core.channel_backend import AgentBackendRefused, load_backend_agent
from daimon.core.defaults.builtin_agent import resolve_builtin_agent_name
from daimon.core.defaults.metadata import MA_METADATA_KEY_NAME
from daimon.core.ma_identity import derive_agent_uuid
from daimon.core.ma_resolver import MAResolverMissError, new_resolver_cache, resolve_agent
from daimon.core.setup_conversations import resolve_setup_agents
from daimon.core.stores.selfhosted_resources import SelfHostedAgents, SelfHostedEnvironments
from daimon.core.turn.selfhosted_records import resolve_records
from daimon.testing.factories import make_account, make_tenant
from daimon.testing.ma_models import ma_agent
from mux.contracts.ids import Scope
from mux.testing.drivers.anthropic.ma import MARouter, build_fake_anthropic, list_response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from structlog.testing import capture_logs

from ..stores.test_selfhosted_resources import agent_spec, environment_spec, scope
from ..test_neutral_resource_host import SDKCase, raw_agent, reply
from ..test_neutral_resource_host import spec as catalog_spec

_MANAGED = {"daimon_managed": "true"}


@pytest.mark.parametrize(
    ("requested", "holders", "expected", "event"),
    [
        ("specialist", {"daimon": True}, "specialist", None),
        ("default", {"default": True, "daimon": True}, "default", None),
        ("default", {"daimon": True}, "daimon", "builtin_agent.fallback_used"),
        ("Default", {"default": False, "daimon": True}, "daimon", "builtin_agent.fallback_used"),
        ("default", {"default": False}, None, "builtin_agent.unmanaged_refused"),
        ("default", {"daimon": False}, "default", None),
        ("default", {}, "default", None),
    ],
    ids=["other", "seeded", "missing", "collision", "own-only", "own-daimon", "none"],
)
def test_resolve_builtin_name_when_holders_vary_then_follows_one_rule(
    requested: str, holders: dict[str, bool], expected: str | None, event: str | None
) -> None:
    with capture_logs() as logs:
        target = resolve_builtin_agent_name(
            requested, holders, tenant_id=uuid.uuid4(), surface="test"
        )
    assert target == expected, f"{requested!r} with {holders} resolves to {expected!r}"
    assert [log["event"] for log in logs] == ([event] if event else []), f"logged: {logs}"


def _client(tenant_id: uuid.UUID, *agents: tuple[str, str, bool]) -> AsyncAnthropic:
    payloads = [
        ma_agent(
            id=agent_id, name=name, tenant_id=tenant_id, metadata=_MANAGED if managed else None
        ).model_dump(mode="json")
        for agent_id, name, managed in agents
    ]
    router = MARouter()
    router.add("GET", r"/v1/agents", lambda req, match: list_response(payloads))
    return build_fake_anthropic(router.dispatch)


async def _noop() -> object:
    return object()


_COLLISION = (("ag_daimon", "daimon", True), ("ag_own", "default", False))


async def test_resolve_agent_when_own_default_beside_seeded_daimon_then_reaches_seeded() -> None:
    tenant_id = uuid.uuid4()
    found = await resolve_agent(
        _client(tenant_id, *_COLLISION),
        tenant_id=tenant_id,
        daimon_tag="default",
        apply_callable=_noop,
        cache=new_resolver_cache(),
    )
    assert found == "ag_daimon", "turns, the scheduler and the CLI reach the seeded agent"


async def test_resolve_agent_when_only_own_default_then_misses() -> None:
    tenant_id = uuid.uuid4()
    with pytest.raises(MAResolverMissError):
        await resolve_agent(
            _client(tenant_id, ("ag_own", "default", False)),
            tenant_id=tenant_id,
            daimon_tag="default",
            apply_callable=_noop,
            cache=new_resolver_cache(),
        )


async def test_resolve_agent_when_seeded_and_own_default_then_reaches_seeded() -> None:
    tenant_id = uuid.uuid4()
    found = await resolve_agent(
        _client(tenant_id, ("ag_seeded", "default", True), ("ag_own", "default", False)),
        tenant_id=tenant_id,
        daimon_tag="default",
        apply_callable=_noop,
        cache=new_resolver_cache(),
    )
    assert found == "ag_seeded", "the seeded default wins over a newer namesake"


async def test_setup_agents_when_own_default_beside_seeded_daimon_then_seeded_answers() -> None:
    tenant_id = uuid.uuid4()
    responder, _ = await resolve_setup_agents(
        _client(tenant_id, *_COLLISION),
        tenant_id=tenant_id,
    )
    assert responder.id == "ag_daimon", "setup conversations reach the seeded agent"


@pytest.mark.parametrize(
    ("agents", "expected"),
    [
        ((("ag_daimon", "daimon", True),), "ag_daimon"),
        (_COLLISION, "ag_own"),
        ((("ag_daimon", "daimon", False),), None),
    ],
    ids=["missing", "explicit-own", "unmanaged-daimon"],
)
async def test_load_backend_agent_when_default_misses_then_falls_back_to_seeded_only(
    agents: tuple[tuple[str, str, bool], ...],
    expected: str | None,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = uuid.uuid4()
    client = _client(tenant_id, *agents)
    if expected is None:
        with pytest.raises(AgentBackendRefused):
            await load_backend_agent(
                client,
                db_session_factory,
                tenant_id=tenant_id,
                agent_name="default",
            )
        return
    loaded = await load_backend_agent(
        client,
        db_session_factory,
        tenant_id=tenant_id,
        agent_name="default",
    )
    assert loaded.agent_id == derive_agent_uuid(tenant_id=tenant_id, ma_agent_id=expected), (
        "an explicit edit reaches the agent holding the name, else the seeded one"
    )


@pytest.mark.parametrize(
    ("names", "expected"),
    [(["default"], "default"), (["daimon"], "daimon"), (["daimon", "default"], "daimon")],
    ids=["migrated", "missing", "collision"],
)
async def test_resolve_records_when_builtin_requested_then_follows_the_rule(
    names: list[str],
    expected: str,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    caller = scope(str(tenant.id), str(account.id))
    for name in names:
        await SelfHostedAgents(db_session_factory).create(caller, agent_spec(name), key=name)
    await SelfHostedEnvironments(db_session_factory).create(
        caller, environment_spec("default"), key="env"
    )

    agent, _ = await resolve_records(
        db_session_factory,
        caller,
        agent_name="default",
        environment_name="default",
        responder_id=None,
    )

    assert agent.name == expected, f"records {names} route default to {expected!r}"


@pytest.mark.parametrize(
    ("catalog", "expected"),
    [
        ((("ag_daimon", "daimon", True),), "ag_daimon"),
        ((("ag_daimon", "daimon", True), ("ag_default", "default", True)), "ag_default"),
        ((("ag_daimon", "daimon", False),), None),
        ((("ag_daimon", "daimon", True), ("ag_own", "default", False)), "ag_own"),
    ],
    ids=["legacy-only", "migrated", "unmanaged-daimon", "explicit-own"],
)
async def test_load_backend_agent_when_catalog_host_keeps_daimon_then_falls_back_to_managed_only(
    catalog: tuple[tuple[str, str, bool], ...],
    expected: str | None,
    db_session: AsyncSession,
    db_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """OpenAI and Gemini catalog agents are not renamed, so the fallback never ends there.

    A catalog counterpart keeps the source agent's name and `daimon_managed` tag from
    when it was materialized; only the tag tells a seeded `daimon` from a workspace's own.
    """
    tenant = await make_tenant(db_session)
    account = await make_account(db_session, tenant=tenant)
    await db_session.commit()
    host_scope = Scope(
        tenant_id=str(tenant.id),
        account_id=str(account.id),
        principal_id="caller",
        authorization_id="authorized-host",
    )
    case = SDKCase((db_session_factory, host_scope, host_scope))
    specs = {
        agent_id: catalog_spec(name).model_copy(
            update={
                "metadata": {MA_METADATA_KEY_NAME: name, **(_MANAGED if managed else {})},
            }
        )
        for agent_id, name, managed in catalog
    }
    for agent_id, requested in specs.items():
        await case.seed_agent(requested, id_=agent_id)
    if expected is not None:
        case.trace.queue(
            reply("GET", f"/agents/{expected}", raw_agent(specs[expected], host_scope, expected))
        )

    async with case.sdk() as sdk:
        host = case.host(sdk)
        if expected is None:
            with pytest.raises(AgentBackendRefused):
                await load_backend_agent(
                    _client(tenant.id),
                    db_session_factory,
                    tenant_id=tenant.id,
                    agent_name="default",
                    resource_host=host,
                )
            return
        loaded = await load_backend_agent(
            _client(tenant.id),
            db_session_factory,
            tenant_id=tenant.id,
            agent_name="default",
            resource_host=host,
        )

    case.trace.assert_consumed()
    assert loaded.source is not None and loaded.source.ref.id == expected, (
        f"default on a catalog host reaches {expected!r}"
    )


# Functions that resolve an agent by name for a turn, a schedule, setup or an
# edit. Each must apply `builtin_agent`'s rule.
_RESOLVERS = {
    "ma_resolver.py": "_route_builtin",
    "setup_conversations.py": "resolve_setup_agents",
    "turn/selfhosted_records.py": "_builtin_target",
    "channel_backend.py": "load_backend_agent",
}
# Direct by-name lookups of the configured default that bypass those resolvers,
# with why each is safe. Deliberate limit: lookups by a stored agent name
# (memory view, routine delivery, channel rules) are not listed and miss while a
# provider rename keeps failing, since the database moves first. That is
# transient, the next apply retries it, and turns still route through the resolver.
_ALLOWED_DIRECT = {
    "adapters/discord/daimon/adapters/discord/bot.py": "roster check after a passing reconcile; "
    "a blocked rename fails the reconcile first",
    "adapters/slack/daimon/adapters/slack/boot_sweep.py": "same roster check",
    "adapters/teams/daimon/adapters/teams/provisioning.py": "same roster check",
}
_CORE = Path(__file__).resolve().parents[2] / "daimon" / "core"
_PACKAGES = _CORE.parents[2]
_HELPERS = {"route_builtin_agent", "resolve_builtin_agent_name", "_find_legacy_builtin"}


def _calls(node: ast.AST) -> set[str]:
    return {
        call.func.id if isinstance(call.func, ast.Name) else call.func.attr
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name | ast.Attribute)
    }


@pytest.mark.parametrize(("path", "function"), sorted(_RESOLVERS.items()))
def test_builtin_resolvers_when_parsed_then_apply_the_rule(path: str, function: str) -> None:
    tree = ast.parse((_CORE / path).read_text())
    body = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == function
    )
    assert _calls(body) & _HELPERS, f"{path}:{function} must route the built-in name"


def _names_default(node: ast.expr) -> bool:
    text = ast.unparse(node)
    return "BUILTIN_AGENT_NAME" in text or "deployment_default" in text


def test_direct_default_lookups_when_discovered_then_are_listed_with_a_reason() -> None:
    """Lookups that name the configured default must be listed; stored-name ones are out of scope."""
    found: set[str] = set()
    for source in _PACKAGES.rglob("daimon/**/*.py"):
        if "tests" in source.parts:
            continue
        tree = ast.parse(source.read_text())
        aliases = {
            target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign) and _names_default(node.value)
            for target in node.targets
            if isinstance(target, ast.Name)
        }
        for call in ast.walk(tree):
            if not (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id in {"find_agent_by_daimon_tag", "find_agents_by_daimon_tag"}
            ):
                continue
            name = next((k.value for k in call.keywords if k.arg == "name"), None)
            if name is not None and (
                _names_default(name) or (isinstance(name, ast.Name) and name.id in aliases)
            ):
                found.add(str(source.relative_to(_PACKAGES)))
    assert found == set(_ALLOWED_DIRECT), (
        f"route new lookups through builtin_agent or list them; drop stale entries: {found}"
    )
