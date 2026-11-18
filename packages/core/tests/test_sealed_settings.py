"""A sealed channel is inside-only: every admin settings path goes through one rule.

The rule is `daimon.core.permissions.sealed_outside`. `authorize` applies it to each
action in `SEALED_SETTINGS_ACTIONS`; funnels that do not call `authorize` call it
directly. These tests fail when an action, or a call that decides one, is left out.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from daimon.core.access_policy import ChannelRule, TenantAccessPolicy
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
