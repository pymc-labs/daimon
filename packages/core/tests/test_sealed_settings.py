"""A sealed channel is inside-only: every admin settings path goes through one rule.

The rule is `daimon.core.permissions.sealed_outside`. `authorize` applies it to each
action in `SEALED_SETTINGS_ACTIONS`; funnels that do not call `authorize` call it
directly. These tests fail when an action, or a call that decides one, is left out.
"""

from __future__ import annotations

import ast
import datetime as dt
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from daimon.core.access_policy import AgentRule, ChannelRule, TenantAccessPolicy
from daimon.core.authz import (
    SEALED_SETTINGS_ACTIONS,
    Action,
    AgentRef,
    Place,
    Subject,
    authorize,
    origin_ids,
)
from daimon.core.permissions import SEALED_REFUSAL, sealed_outside
from daimon.testing.shared_helpers import Session as _Session

PACKAGES = Path(__file__).resolve().parents[2]

#: Actions that change nothing in a channel or its agent, with the reason each is left out.
EXEMPT = {
    Action.START_TURN: "runs a turn; reading is the content side",
    Action.RUN_AGENT: "runs an agent; the content side owns it",
    Action.SAVE_ROUTINE: "routines fail closed on their own (no origin)",
    Action.POST: "posting is not a settings change",
    Action.ACT_FOR_CREATOR: "acts as a person, not a setting",
    Action.DIRECT_MESSAGE: "messaging is not a settings change",
    Action.CREATE_AGENT: "a new agent has no channel yet",
    Action.PUBLISH: "publishing is not a settings change",
    Action.FORK: "a copy starts open",
    Action.BIND_CHANNEL_DEFAULT: "rule views call it with no subject",
    Action.SET_WORKSPACE_SETTINGS: "workspace level, no channel",
    Action.MANAGE_INTEGRATIONS: "workspace level, no channel",
    Action.SET_SUPPORT: "workspace level, no channel",
    Action.SET_ROUTINE_SANDBOX_TOOLS: "workspace level, no channel",
    Action.SET_CONTEXT_PROVIDER: "workspace level, no channel",
    Action.HAND_OFF: "a hand-over is not a settings change",
    Action.READ_CHANNEL: "content side",
    Action.READ_SESSION: "content side",
    Action.CONTINUE_SESSION: "content side",
}

_SEALED_ACTIONS = sorted(SEALED_SETTINGS_ACTIONS, key=str)
SEALED = TenantAccessPolicy(channel_rules={"C_ACME": ChannelRule(readers="own", writers="own")})
AGENT = AgentRef.of("acme", "acme")
PLACE = Place(channel_id="C_ACME", parent_channel_id="C_ACME")
OUTSIDE = Subject(is_admin=True, platform_user_id="U1")
INSIDE = Subject(is_admin=True, platform_user_id="U1", origin_channel_ids=origin_ids("C_ACME"))
OPERATOR = Subject(is_admin=True, platform_user_id="U1", operator=True)
MEMBER = Subject(is_admin=False, platform_user_id="U2")


def test_every_action_is_sealed_or_exempt_with_a_reason() -> None:
    assert not SEALED_SETTINGS_ACTIONS & set(EXEMPT)
    assert SEALED_SETTINGS_ACTIONS | set(EXEMPT) == set(Action), (
        "a new Action must join SEALED_SETTINGS_ACTIONS or EXEMPT, with its reason"
    )


def _decide(policy: TenantAccessPolicy, subject: Subject, action: Action, **kw: object):
    return authorize(policy, subject=subject, action=action, agent=AGENT, place=PLACE, **kw)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize("action", _SEALED_ACTIONS)
def test_a_sealed_setting_is_inside_only_for_admins(action: Action) -> None:
    operator = _decide(SEALED, OPERATOR, action)
    if not operator:
        pytest.skip("this action needs more facts than the table gives; the path tests cover it")
    outside = _decide(SEALED, OUTSIDE, action)
    assert not outside and outside.reason == "sealed_channel"
    assert _decide(SEALED, INSIDE, action).allowed
    # no origin at all (web, hub, DM, retained key) is outside
    assert _decide(SEALED, Subject(is_admin=True), action).reason == "sealed_channel"


@pytest.mark.parametrize("action", _SEALED_ACTIONS)
def test_an_open_channel_and_a_member_are_unchanged(action: Action) -> None:
    open_policy = TenantAccessPolicy()
    # open channel: admin outside is decided as before
    assert _decide(open_policy, OUTSIDE, action).reason != "sealed_channel"
    # a member never gets "sealed_channel": the older refusal still comes first
    assert _decide(SEALED, MEMBER, action).reason != "sealed_channel"


def test_the_helper_names_the_channel_it_refuses() -> None:
    assert sealed_outside(SEALED, frozenset(), channel_ids=("C_ACME",)) == "C_ACME"
    assert sealed_outside(SEALED, frozenset({"C_ACME"}), channel_ids=("C_ACME",)) is None
    assert sealed_outside(SEALED, frozenset(), channel_ids=("C_ACME",), operator=True) is None
    assert sealed_outside(SEALED, frozenset(), channel_ids=("C_OPEN",)) is None
    assert "inside the channel" in SEALED_REFUSAL


#: `authorize` calls on a sealed action that pass no real policy. Each is checked by a
#: named guard elsewhere; a call not listed here fails the scan below.
NO_POLICY_CALLS = {
    (
        "core/daimon/core/agent_admin.py",
        "require_admin",
    ): "load_admin_target refuses a sealed target",
    (
        "core/daimon/core/operation_policy.py",
        "_decide_operation",
    ): "table only; edits go through check_may_edit",
    (
        "core/daimon/core/channel_backend.py",
        "_check_actor",
    ): "cheap pre-check; _authorize_edit refuses a sealed agent",
    ("core/daimon/core/approval_gate.py", "set_approval_gate"): "calls sealed_outside right after",
}
EMPTY = {"TenantAccessPolicy()", "OPEN_ACCESS_POLICY", "_NO_POLICY"}


def _scan() -> list[tuple[str, str, str, int]]:
    found: list[tuple[str, str, str, int]] = []
    for path in PACKAGES.rglob("*.py"):
        rel = path.relative_to(PACKAGES).as_posix()
        if "/tests/" in rel or rel.endswith("core/authz.py"):
            continue
        tree = ast.parse(path.read_text())
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for call in ast.walk(fn):
                if not isinstance(call, ast.Call) or getattr(call.func, "id", "") != "authorize":
                    continue
                names = {
                    a.attr
                    for a in ast.walk(call)
                    if isinstance(a, ast.Attribute)
                    and isinstance(a.value, ast.Name)
                    and a.value.id == "Action"
                }
                if not names & {a.name for a in SEALED_SETTINGS_ACTIONS}:
                    continue
                first = (
                    call.args[0]
                    if call.args
                    else next(k.value for k in call.keywords if k.arg == "policy")
                )
                found.append((rel, fn.name, ast.unparse(first), call.lineno))
    return found


def test_no_sealed_action_is_decided_against_an_empty_policy_unlisted() -> None:
    calls = _scan()
    assert len(calls) >= 20, "the scan lost the authorize calls"
    bad = [c for c in calls if c[2] in EMPTY and c[:2] not in NO_POLICY_CALLS]
    assert not bad, f"decided with no policy, so sealed_outside cannot see the seal: {bad}"
    stale = set(NO_POLICY_CALLS) - {c[:2] for c in calls}
    assert not stale, f"listed but no longer there: {stale}"


#: Funnels that do not call `authorize`. Each must call the helper (or a function that does).
DIRECT_GUARDS = {
    "core/daimon/core/permissions.py": "sealed_outside",
    "core/daimon/core/channel_changes.py": "sealed_outside",
    "core/daimon/core/channel_rules.py": "sealed_outside",
    "core/daimon/core/channel_backend.py": "sealed_outside",
    "core/daimon/core/agent_target.py": "sealed_outside",
    "core/daimon/core/agent_knowledge.py": "sealed_outside",
    "core/daimon/core/approval_gate.py": "sealed_outside",
    "core/daimon/core/features/service.py": "sealed_outside",
    "adapters/mcp/daimon/adapters/mcp/tools/_pin_guard.py": "authorize",
}


@pytest.mark.parametrize("rel", sorted(DIRECT_GUARDS))
def test_each_listed_funnel_still_calls_the_rule(rel: str) -> None:
    assert DIRECT_GUARDS[rel] in (PACKAGES / rel).read_text()


def test_a_funnel_with_no_origin_is_refused_by_default() -> None:
    assert Subject(is_admin=True).inside == frozenset()
    assert not Subject(is_admin=True).operator


# --- the fixes the review asked for ---------------------------------------------------


def _sessions() -> _Session:
    return _Session()


@pytest.mark.asyncio
async def test_a_member_owner_changes_their_agents_backend_as_before(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uuid

    from daimon.core import channel_backend as cb
    from daimon.core.stores import access_policy as policy_store
    from daimon.core.stores import agent_backends
    from daimon.core.stores.domain import BackendAgent

    tenant = uuid.uuid4()
    owned = [True]

    async def actor_exists(*_: object) -> bool:
        return True

    async def load(*_a: object, **_k: object) -> TenantAccessPolicy:
        return SEALED_HOME

    async def owner(*_: object) -> bool:
        return owned[0]

    monkeypatch.setattr(agent_backends, "actor_exists", actor_exists)
    monkeypatch.setattr(policy_store, "load_access_policy", load)
    monkeypatch.setattr(cb, "may_edit_as_owner", owner)
    service = cb.AgentBackendService(_sessions, enabled=True)  # pyright: ignore[reportArgumentType]
    agent = BackendAgent(
        tenant_id=tenant, agent_id=uuid.uuid4(), name="acme", configuration_digest="d"
    )
    member = cb.BackendActor(tenant, uuid.uuid4(), MEMBER)
    admin = cb.BackendActor(tenant, uuid.uuid4(), OUTSIDE)
    inside = cb.BackendActor(tenant, uuid.uuid4(), INSIDE)

    assert await service._authorize_edit(_Session(), agent, member) is False  # pyright: ignore[reportPrivateUsage,reportArgumentType]
    assert await service._authorize_edit(_Session(), agent, inside) is True  # pyright: ignore[reportPrivateUsage,reportArgumentType]
    with pytest.raises(cb.AgentBackendRefused) as refused:
        await service._authorize_edit(_Session(), agent, admin)  # pyright: ignore[reportPrivateUsage,reportArgumentType]
    assert refused.value.reason == "sealed_channel"


SEALED_HOME = TenantAccessPolicy(
    channel_rules={"C_ACME": ChannelRule(readers="own", writers="own")},
    agent_rules={"acme": AgentRule(runs_in=("C_ACME",))},
)


@pytest.mark.asyncio
async def test_a_member_keeps_a_feature_change_an_admin_outside_loses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uuid

    from daimon.core.features import service as fs
    from daimon.core.features.registry import FEATURES
    from daimon.core.features.snapshot import FeaturePlace

    async def load(*_a: object, **_k: object) -> TenantAccessPolicy:
        return SEALED

    monkeypatch.setattr(fs, "load_access_policy", load)
    spec = FEATURES["memory_channel"]
    place = FeaturePlace(tenant_id=uuid.uuid4(), platform="discord", channel_id="C_ACME")

    def actor(admin: bool, inside: frozenset[str] = frozenset()) -> fs.FeatureActor:
        async def admin_for(_: object) -> bool:
            return admin

        return fs.FeatureActor(place.tenant_id, None, "web", None, admin_for, inside=inside)

    await fs._require_inside(_sessions, actor(False), spec, "channel", place)  # pyright: ignore[reportPrivateUsage,reportArgumentType]
    await fs._require_inside(_sessions, actor(True, frozenset({"C_ACME"})), spec, "channel", place)  # pyright: ignore[reportPrivateUsage,reportArgumentType]
    with pytest.raises(Exception, match="private"):
        await fs._require_inside(_sessions, actor(True), spec, "channel", place)  # pyright: ignore[reportPrivateUsage,reportArgumentType]


@pytest.mark.asyncio
async def test_memory_text_is_refused_by_the_stores_channel_not_the_agents_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shared agent has no home, yet its store for a private channel stays inside-only."""
    import uuid

    from daimon.core import agent_knowledge as ak
    from daimon.core.agent_editing import AgentActor, AgentEditError
    from daimon.core.authz import Surface

    async def load(*_a: object, **_k: object) -> TenantAccessPolicy:
        return TenantAccessPolicy(channel_rules={"C_ACME": ChannelRule(readers="inside")})

    async def store_id(*_a: object, **_k: object) -> str:
        return "mem_1"

    monkeypatch.setattr(ak, "load_access_policy", load)
    monkeypatch.setattr(ak, "get_channel_memory_store_id", store_id)
    deps = type("D", (), {"session_factory": staticmethod(_sessions)})()
    target = type("T", (), {"agent_id": uuid.uuid4()})()
    tenant = uuid.uuid4()

    def actor(subject: Subject) -> AgentActor:
        return AgentActor(tenant, uuid.uuid4(), subject, Surface.CONFIG)

    with pytest.raises(AgentEditError, match="private"):
        await ak._store_id(deps, target, actor(OUTSIDE), "discord/C_ACME")  # pyright: ignore[reportPrivateUsage,reportArgumentType]
    assert await ak._store_id(deps, target, actor(INSIDE), "discord/C_ACME") == "mem_1"  # pyright: ignore[reportPrivateUsage,reportArgumentType]
    assert await ak._store_id(deps, target, actor(OUTSIDE), "discord/C_OPEN") == "mem_1"  # pyright: ignore[reportPrivateUsage,reportArgumentType]


def test_only_listed_read_only_functions_use_the_view_only_setup_property() -> None:
    """`views_setup` and `require_setup(read_only=True)` stay out of every write path."""
    from daimon.core.agent_target import VIEW_ONLY_READERS

    used: set[tuple[str, str]] = set()
    for path in (PACKAGES / "core/daimon/core").rglob("*.py"):
        if path.name == "agent_target.py":
            continue
        for fn in ast.walk(ast.parse(path.read_text())):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Attribute) and node.attr == "views_setup":
                    used.add((path.name, fn.name))
                if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "require_setup":
                    for kw in node.keywords:
                        if (
                            kw.arg == "read_only"
                            and isinstance(kw.value, ast.Constant)
                            and kw.value.value is True
                        ):
                            used.add((path.name, fn.name))
    assert used == set(VIEW_ONLY_READERS), (
        f"unlisted view-only reader, or a listed one is gone: {used ^ set(VIEW_ONLY_READERS)}"
    )


# Callers of `load_target` that change nothing, or whose change is refused further down.
_LOAD_TARGET_UNGUARDED = {
    ("agent_tool_rule_admin.py", "list_agent_rules"): "read only",
    (
        "agent_tool_rule_admin.py",
        "set_agent_rule",
    ): "the rule store refuses it (stores/agent_tool_rules.set_rule)",
    ("follows.py", "agent_follows"): "read only",
}


def test_every_load_target_caller_guards_its_change() -> None:
    """A function that loads an agent target refuses a private channel's own agent before it
    changes anything: `require_setup`, a `.sealed` or `sees_setup` check, or a listed reason."""
    from daimon.core.agent_target import VIEW_ONLY_READERS

    missed: set[tuple[str, str]] = set()
    for path in (PACKAGES / "core/daimon/core").rglob("*.py"):
        if path.name == "agent_target.py":
            continue
        for fn in ast.walk(ast.parse(path.read_text())):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            calls = {getattr(n.func, "id", "") for n in ast.walk(fn) if isinstance(n, ast.Call)}
            if "load_target" not in calls:
                continue
            attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
            key = (path.name, fn.name)
            if "require_setup" in calls or attrs & {"sealed", "sees_setup"}:
                continue
            if key in VIEW_ONLY_READERS or key in _LOAD_TARGET_UNGUARDED:
                continue
            missed.add(key)
    assert not missed, f"guard these, or list them with a reason: {sorted(missed)}"


async def test_an_admin_outside_revokes_no_coding_key_of_a_private_channels_agent() -> None:
    from unittest.mock import AsyncMock, patch

    from daimon.core import coding_keys
    from daimon.core.agent_editing import AgentEditError as Refused

    target = SimpleNamespace(sealed=True, owned=False, agent_id=uuid.uuid4())
    revoke = AsyncMock()
    with (
        patch.object(coding_keys, "load_target", AsyncMock(return_value=target)),
        patch.object(coding_keys, "revoke_coding_key", revoke),
        pytest.raises(Refused, match="private"),
    ):
        await coding_keys.revoke_agent_coding_key(
            SimpleNamespace(),  # pyright: ignore[reportArgumentType]
            actor=SimpleNamespace(subject=OUTSIDE, tenant_id=uuid.uuid4()),  # pyright: ignore[reportArgumentType]
            ma_agent_id="ag",
            jti=uuid.uuid4(),
            now=dt.datetime.now(dt.UTC),
        )
    revoke.assert_not_awaited()


# Archive paths that clear channel picks without the seal check, each with its reason.
_ARCHIVE_UNGUARDED = {
    ("adapters/cli/daimon/adapters/cli/commands/agents.py", "agents_archive"): "operator CLI",
    ("adapters/cli/daimon/adapters/cli/commands/environments.py", "_clear_picks"): "operator CLI",
    ("adapters/slack/daimon/adapters/slack/agent_setup/write.py", "delete_agent"): (
        "no caller: the Slack panel archives nothing"
    ),
}


def test_every_archive_path_checks_the_channels_that_pick_it() -> None:
    """Archive clears every channel's pick (`forget_archived_agent`,
    `clear_environment_references`), so each path asks `archive_sealed_refusal` first."""
    clears = {"forget_archived_agent", "clear_environment_references"}
    missed: set[tuple[str, str]] = set()
    for path in PACKAGES.rglob("*.py"):
        if "/tests/" in str(path) or path.name in {"agent_lifecycle.py", "scoped_config_write.py"}:
            continue
        for fn in ast.walk(ast.parse(path.read_text())):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            called = {
                getattr(n.func, "id", None) or getattr(n.func, "attr", "")
                for n in ast.walk(fn)
                if isinstance(n, ast.Call)
            }
            if not called & clears or "archive_sealed_refusal" in called:
                continue
            key = (str(path.relative_to(PACKAGES)), fn.name)
            if key not in _ARCHIVE_UNGUARDED:
                missed.add(key)
    assert not missed, f"check the seal first, or list the path with a reason: {sorted(missed)}"


# A call that refuses a private channel's pick from outside it, before the pick is committed.
_PICK_GUARDS = {
    "check_places": "agent_editing: `sealed_outside` on every pick it adds or clears",
    "require_channel_admin": "channel_changes: `sealed_outside` on the channel",
    "_write_rule": "channel_rules: `authorize(SET_CHANNEL_RULE)` in the same transaction",
}


def test_every_channel_pick_writer_checks_the_seal() -> None:
    """A channel's default agent is its setting. Every function that writes it
    (`write_channel_default`) calls a guard that refuses a private channel from outside."""
    missed: set[tuple[str, str]] = set()
    for path in PACKAGES.rglob("*.py"):
        if "/tests/" in str(path) or path.name == "channel_changes.py" and "stores" in str(path):
            continue
        for fn in ast.walk(ast.parse(path.read_text())):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            called = {
                getattr(n.func, "id", None) or getattr(n.func, "attr", "")
                for n in ast.walk(fn)
                if isinstance(n, ast.Call)
            }
            if "write_channel_default" in called and not called & _PICK_GUARDS.keys():
                missed.add((str(path.relative_to(PACKAGES)), fn.name))
    assert not missed, f"guard these channel pick writes: {sorted(missed)}"
