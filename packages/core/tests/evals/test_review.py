"""Blind review through `ReviewService` against Postgres: nothing in a blind task or its
payload names an arm, a model or a model reaction."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

from daimon.core.evals.access import Share, Subject
from daimon.core.evals.bundle import DirSource, to_zip
from daimon.core.evals.format import ScoreValue
from daimon.core.evals.review import ReviewService, ScoreDraft
from daimon.core.evals.service import EvalService, Page
from daimon.core.model_reactions import MODEL_MARKS
from daimon.testing.factories import make_tenant
from daimon.testing.settings_mock import bare_settings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_SOURCE = DirSource(Path(__file__).parent / "example_bundle")
_RUN = "baseline-a-2026-01"
_REACTION = sorted(MODEL_MARKS)[0]
_SECRET_WORDS = ("Candidate Zed", "candidate-z", "zed-agent", "Baseline A")


def _bundle() -> bytes:
    """The example run with a second arm whose item names its agent and carries a model mark."""
    files = {name: _SOURCE.read(name) for name in _SOURCE.names()}
    base = "runs/baseline-a-2026-01/"
    run = json.loads(files[base + "run.json"])
    run["arms"].append(
        {
            "id": "candidate-z", "label": "Candidate Zed", "kind": "baseline",
            "baseline": {"source_system": "zed-agent"},
        }
    )  # fmt: skip
    first = json.loads(files[base + "items.jsonl"].splitlines()[0])
    second = json.loads(json.dumps(first))
    second["id"] = first["id"] + ".z"
    second["arm_id"] = "candidate-z"
    second["chat"] += [
        {"seq": 2, "kind": "reaction_add", "actor": "300000000000000001", "emoji": _REACTION},
        {"seq": 3, "kind": "message", "actor": "300000000000000001", "message_id": "r2",
         "text": "Reply from zed-agent."},
    ]  # fmt: skip
    queue = {
        "schema": "daimon.eval.review/1",
        "kind": "queue",
        "id": "q2",
        "name": "Compare",
        "layout": "side_by_side",
        "blind": True,
    }
    return to_zip(
        files
        | {
            base + "run.json": json.dumps(run).encode(),
            base + "items.jsonl": "\n".join(json.dumps(i) for i in (first, second)).encode(),
            base + "reviews.jsonl": json.dumps(queue).encode(),
        }
    )


async def _chunks(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def test_blind_task_when_served_then_payload_names_no_arm_model_or_reaction(
    db_session: AsyncSession, db_session_factory: async_sessionmaker[AsyncSession]
) -> None:
    settings = bare_settings().model_copy(update={"features": {"evals": "on"}})
    tenant = await make_tenant(db_session, platform="web", workspace_id="team-a")
    owner = Subject(tenant_id=tenant.id, platform="web", key="web:alex", role="admin")
    reader = Subject(tenant_id=tenant.id, platform="web", key="web:rae", role="member")
    evals = EvalService(db_session_factory, settings)
    review = ReviewService(db_session_factory, settings)
    await evals.import_bundle(owner, _chunks(_bundle()))
    await evals.share(
        owner, "support-basics", [Share(kind="person", id="web:rae", role="reviewer")]
    )

    task = await review.next(reader, f"{_RUN}~q2")
    assert task is not None and len(task.columns) == 2, "both arms come as columns"
    payload = task.model_dump_json(by_alias=True)
    for word in (*_SECRET_WORDS, _REACTION):
        assert word not in payload, f"the task leaks {word!r}"
    assert sorted(c.label for c in task.columns) == ["A", "B"], "columns carry neutral letters"
    assert all(c.arm is None for c in task.columns), "no column names its arm"

    shown = await evals.items(reader, _RUN, page=Page(offset=0, limit=50))
    assert not {i.arm_id for i in shown} & {"baseline-a", "candidate-z"}, (
        "the item list is blind too"
    )

    drafts = [ScoreDraft(criterion="preference", value=ScoreValue(preference="a"))]
    await review.submit(reader, task.id, drafts)
    assert await review.next(reader, f"{_RUN}~q2") is None, "the one task is done"
