"""A running eval item with a given world, for fake-world tests."""

from __future__ import annotations

import uuid
from typing import Any

from daimon.core._models import EvalCase, EvalCaseRevision, EvalRun, EvalRunItem, EvalSuite
from daimon.core.evals.format import Case, case_hash, to_document
from sqlalchemy.ext.asyncio import AsyncSession


def eval_case(world: dict[str, Any], **extra: Any) -> Case:  # noqa: ANN401 - case fields
    """A one-message case around `world`."""
    return Case.model_validate({
        "id": "world-case", "revision": 1, "title": "World test", "fidelity": {"level": "full"},
        "provenance": {"kind": "authored"},
        "surface": {
            "platform": "web", "conversation": "dm",
            "participants": [{"id": "u1", "display_name": "Alex", "is_requester": True}],
            "bot": {"id": "b1", "display_name": "Daimon", "is_bot": True},
        },
        "clock": {"now": "2026-01-05T09:15:00Z"}, "history": [],
        "turn": {"messages": [{"id": "m1", "author": "u1", "at": "2026-01-05T09:15:00Z",
                               "text": "Hi"}]},
        "world": world, "expect": {}, **extra,
    })  # fmt: skip


async def make_running_item(session: AsyncSession, tenant_id: uuid.UUID, case: Case) -> uuid.UUID:
    """Suite, case revision, run and one running item of `case`, flushed; the item's id."""
    document, key = to_document(case), uuid.uuid4().hex[:12]
    suite = EvalSuite(tenant_id=tenant_id, key=f"suite-{key}", name="World",
                      owner_key="web:alex", current_version=1)  # fmt: skip
    session.add(suite)
    await session.flush()
    row = EvalCase(tenant_id=tenant_id, suite_id=suite.id, key=case.id, current_revision=1)
    session.add(row)
    await session.flush()
    session.add(EvalCaseRevision(case_id=row.id, revision=case.revision, tenant_id=tenant_id,
                                 document=document, sha256=case_hash(document),
                                 fidelity=case.fidelity.level))  # fmt: skip
    run = EvalRun(tenant_id=tenant_id, key=f"run-{key}", suite_id=suite.id, suite_version=1,
                  status="running", origin="local", document={})  # fmt: skip
    session.add(run)
    await session.flush()
    item = EvalRunItem(tenant_id=tenant_id, run_id=run.id, key="item", case_key=case.id,
                       case_revision=case.revision, arm_key="a", repeat=0, status="running",
                       document={})  # fmt: skip
    session.add(item)
    await session.flush()
    return item.id
