"""Who may run a role-gated agent unattended: a freshly verified owner holding its roles."""

from __future__ import annotations

import re
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from daimon.core.access_policy import AgentRule, TenantAccessPolicy
from daimon.core.authz import (
    Action,
    AgentRef,
    Subject,
    Surface,
    authorize,
    build_subject,
    holds_agent_roles,
)
from daimon.core.turn import unattended
from daimon.core.turn.unattended import PRINCIPAL_RECHECK_SECONDS, owner_principal_check

POLICY = TenantAccessPolicy(agent_rules={"daimon": AgentRule(invoker_role_ids=("123",))})
AGENT = AgentRef.of("daimon")
OWNER = Subject(
    platform_user_id="u1", role_ids=("123",), verified_membership=True, membership_fresh=True
)


def test_fresh_owner_holding_the_role_qualifies() -> None:
    assert holds_agent_roles(POLICY, AGENT.names, OWNER)


@pytest.mark.parametrize(
    "change",
    [
        {"membership_fresh": False},
        {"verified_membership": None},
        {"verified_membership": False},
        {"role_ids": ("999",)},
        {"role_ids": ()},
        {"platform_user_id": None},
        {"client": True},
        {"is_admin": True, "role_ids": ()},
    ],
)
def test_forged_or_missing_facts_never_qualify(change: dict[str, object]) -> None:
    assert not holds_agent_roles(POLICY, AGENT.names, replace(OWNER, **change))


def test_build_subject_drops_freshness_without_verified_membership() -> None:
    assert not build_subject(
        is_admin=False, platform_user_id="u1", membership_fresh=True
    ).membership_fresh
    assert not build_subject(
        is_admin=False,
        platform_user_id="u1",
        verified_membership=True,
        client=True,
        membership_fresh=True,
    ).membership_fresh


def test_routine_run_and_save_follow_the_owner_not_an_admin() -> None:
    def decide(action: Action, subject: Subject) -> bool:
        return bool(
            authorize(POLICY, subject=subject, action=action, surface=Surface.ROUTINE, agent=AGENT)
        )

    for action in (Action.RUN_AGENT, Action.SAVE_ROUTINE):
        assert decide(action, OWNER)
        assert not decide(action, replace(OWNER, role_ids=(), is_admin=True))
        assert not decide(action, replace(OWNER, membership_fresh=False))
        assert not decide(action, Subject())


class _Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


def _check(monkeypatch: pytest.MonkeyPatch, reads: list[Subject | Exception], clock: _Clock):
    calls: list[int] = []

    async def fake_resolve(*_a: object, **kw: object) -> Subject:
        assert kw["require_fresh"] is True
        calls.append(1)
        out = reads[min(len(calls), len(reads)) - 1]
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(unattended, "resolve_subject", fake_resolve)
    check = owner_principal_check(
        object(),  # type: ignore[arg-type]
        None,
        tenant_id=uuid.uuid4(),
        owner_user_id="u1",
        clock=clock,
    )
    return check, calls


async def test_owner_check_memoizes_for_a_minute_then_rereads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    lapsed = replace(OWNER, role_ids=())
    check, calls = _check(monkeypatch, [OWNER, lapsed], clock)
    assert await check(POLICY, AGENT)
    clock.now = PRINCIPAL_RECHECK_SECONDS - 1
    assert await check(POLICY, AGENT)
    assert len(calls) == 1
    clock.now = PRINCIPAL_RECHECK_SECONDS
    assert not await check(POLICY, AGENT)
    assert len(calls) == 2


async def test_owner_check_denies_and_forgets_a_denial(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    check, calls = _check(monkeypatch, [replace(OWNER, role_ids=()), OWNER], clock)
    assert not await check(POLICY, AGENT)
    assert await check(POLICY, AGENT), "a denial is not remembered"
    assert len(calls) == 2


async def test_owner_check_raises_when_the_platform_cannot_say(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check, _ = _check(monkeypatch, [RuntimeError("down")], _Clock())
    with pytest.raises(RuntimeError):
        await check(POLICY, AGENT)


async def test_owner_check_without_an_owner_denies() -> None:
    check = owner_principal_check(None, None, tenant_id=None, owner_user_id=None)
    assert not await check(POLICY, AGENT)


def test_only_the_resolvers_and_the_subject_builder_mark_membership_fresh() -> None:
    """A fresh mark is a claim about a platform read; nothing else may make it."""
    root = Path(__file__).resolve().parents[3] / "packages"
    pattern = re.compile(r"membership_fresh\s*=\s*(?!False\b)")
    found = {
        str(p.relative_to(root))
        for p in root.rglob("*.py")
        if "/tests/" not in str(p) and pattern.search(p.read_text())
    }
    assert found == {
        "core/daimon/core/authz.py",
        "core/daimon/core/membership_subject.py",
        "adapters/mcp/daimon/adapters/mcp/tools/routines.py",
    }


@pytest.mark.parametrize(
    ("change", "role_mode", "allowed"),
    [
        ({}, True, True),
        ({"role_ids": ("9",)}, True, False),
        ({"role_ids": ()}, True, False),
        ({"verified_membership": None}, True, False),
        ({"verified_membership": False}, True, False),
        ({"role_ids": ()}, False, True),
    ],
)
async def test_dm_policy_allows_reads_roles_only_in_role_mode(
    monkeypatch: pytest.MonkeyPatch, change: dict[str, object], role_mode: bool, allowed: bool
) -> None:
    from daimon.core import membership_subject
    from daimon.core.config import DirectMessagePolicy

    subject = replace(OWNER, **change)

    async def fake_resolve(*_a: object, **_k: object) -> Subject:
        return subject

    monkeypatch.setattr(membership_subject, "resolve_subject", fake_resolve)
    policy = (
        DirectMessagePolicy(mode="role", role_ids=["123"]) if role_mode else DirectMessagePolicy()
    )
    got = await membership_subject.dm_policy_allows(
        policy,
        None,  # type: ignore[arg-type]
        None,
        tenant_id=uuid.uuid4(),
        user_id="u1",
    )
    assert got is allowed
