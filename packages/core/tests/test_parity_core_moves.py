"""Logic moved from the web adapter to core: owners by reference, a pinned skill, credit
rows and the feedback report. All pure or with the stores stubbed."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from daimon.core import agent_admin, agent_skill_files, credit_report, credit_views
from daimon.core.agent_editing import AgentActor, AgentEditDeps, AgentEditError
from daimon.core.agent_view import AgentConfigDoc, ModelChoice, SkillDoc
from daimon.core.feedback_summary import (
    MIN_SHOWN,
    UNKNOWN_AGENT,
    AgentFeedback,
    feedback_report,
    shown,
)
from daimon.core.message_feedback import FEEDBACK_REASONS

_CODE = next(iter(FEEDBACK_REASONS))


def _skill(skill_id: str, version: str) -> SkillDoc:
    return SkillDoc(type="custom", skill_id=skill_id, title=skill_id, version=version)


def _config(*skills: SkillDoc) -> AgentConfigDoc:
    return AgentConfigDoc(
        name="helper",
        purpose="",
        avatar="",
        model=ModelChoice(
            id="claude-sonnet-4-6", backend="anthropic", speed="standard", effort="high"
        ),
        system=None,
        skills=list(skills),
        tools=[],
        mcp_servers=[],
        places=None,
    )


def test_with_skill_when_held_then_replaced_in_place_else_added() -> None:
    config = _config(_skill("a", "1"), _skill("b", "1"))
    replaced = agent_skill_files.with_skill(config, _skill("a", "2"))
    assert [(s.skill_id, s.version) for s in replaced.skills] == [("a", "2"), ("b", "1")]
    added = agent_skill_files.with_skill(config, _skill("c", "1"))
    assert [s.skill_id for s in added.skills] == ["a", "b", "c"], "a new skill goes last"
    assert [s.version for s in config.skills] == ["1", "1"], "the original is untouched"


async def test_set_owners_by_ref_when_person_id_or_account_id_then_account(monkeypatch) -> None:
    person_id, person_account, account = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    seen: dict[str, Any] = {}

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    async def people(*_a: Any, **_k: Any):
        return [SimpleNamespace(id=person_id, account_id=person_account)]

    async def fake_set(_deps: object, **kw: Any) -> bool:
        seen.update(kw)
        return True

    monkeypatch.setattr(agent_admin.web_people, "list_people", people)
    monkeypatch.setattr(agent_admin, "set_owners", fake_set)
    deps = cast(AgentEditDeps, SimpleNamespace(session_factory=Session))
    actor = cast(AgentActor, SimpleNamespace(tenant_id=uuid.uuid4()))
    changed = await agent_admin.set_owners_by_ref(
        deps, actor=actor, ma_agent_id="ag", owner=str(person_id), co_owners=[str(account)]
    )
    assert changed is True
    assert seen["owner_account_id"] == person_account, "a web person id maps to their account"
    assert seen["co_owner_account_ids"] == [account], "an account id passes through"
    with pytest.raises(AgentEditError, match="not in this workspace"):
        await agent_admin.set_owners_by_ref(
            deps, actor=actor, ma_agent_id="ag", owner="nobody", co_owners=[]
        )


def test_feedback_report_when_few_votes_then_withheld_and_unknown_agent_named() -> None:
    found = [
        AgentFeedback("a1", up=MIN_SHOWN, down=1, reasons={_CODE: 2}),
        AgentFeedback(None, up=5),
    ]
    report = feedback_report(found, names={"a1": "Ada"}, days=30)
    first, second = report.agents
    assert (first.name, first.up, first.down) == ("Ada", MIN_SHOWN, None), "one vote is withheld"
    assert [r.count for r in first.reasons] == [None], "a count of two is withheld"
    assert (second.name, second.up) == (UNKNOWN_AGENT, 5)
    assert shown(0) == 0 and shown(MIN_SHOWN - 1) is None, "zero is exact; one or two is not"


def test_ledger_wording_when_admin_grant_then_actor_else_named_reason() -> None:
    def line(reason: str, actor: uuid.UUID | None) -> credit_views.LedgerLine:
        return credit_views.LedgerLine(
            uuid.uuid4(), datetime(2026, 1, 1, tzinfo=UTC), "topup", reason, Decimal(1), actor
        )

    assert credit_report.ledger_wording(line("x", uuid.uuid4()), "Ada") == ("Ada", "Manual grant")
    assert credit_report.ledger_wording(line("topup", None), None) == ("Stripe", "Card payment")
    assert credit_report.ledger_wording(line("odd_thing", None), None) == ("Daimon", "Odd thing")


def test_merged_when_same_id_then_summed_biggest_first() -> None:
    rows = [
        credit_report.SpendEntry("a", "A", 1, 1.0),
        credit_report.SpendEntry("b", "B", 1, 5.0),
        credit_report.SpendEntry("a", "A", 2, 2.0),
    ]
    out = credit_report.merged(rows)
    assert [(r.id, r.turns, r.usd) for r in out] == [("b", 1, 5.0), ("a", 3, 3.0)]
