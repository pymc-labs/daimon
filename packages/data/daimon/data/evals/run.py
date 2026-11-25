"""Run a gold set through search as each caller, grade it and keep the spend in bounds.

Each question is searched once as its caller (`access.scoped` with their `ReadContext`); the
arms are read from the hits' ranks (`Hit.arms`, before any rerank) and the fused order is the
result's own, so an arm's list holds only what reached the fused candidates. Query spend bills
the caller's tenant through the usual meter; `Budget` stops the run once the metered cost
reaches the cap.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from daimon.core.config import DataSettings
from daimon.data import access, embed, identity
from daimon.data.access import ReadContext, person
from daimon.data.evals.gold import Caller, GoldSet, Question
from daimon.data.evals.grade import KS, Metrics, grade, summarize, text_summary
from daimon.data.metering import Meter, Payer
from daimon.data.schema import Schemas
from daimon.data.search import Hit, search
from daimon.data.search import rerank as rerank_model
from psycopg import AsyncConnection
from psycopg.rows import DictRow

type Price = Callable[[str, int], float]
"""USD for `units` of a model; 0 for one the rate table does not price."""

TOKENS_PER_CHAR: float = 1 / 3
"""A ceiling for a query's embedding tokens."""


def estimate(questions: Sequence[Question], settings: DataSettings, price: Price) -> float:
    """The most a run can spend: one embedding and one rerank request per question."""
    usd = 0.0
    for q in questions:
        if embed.configured(settings):
            usd += price(settings.embed_model, math.ceil(len(q.q) * TOKENS_PER_CHAR) + 1)
        if settings.rerank != "off":
            usd += price(rerank_model.MODEL, rerank_model.UNITS)
    return usd


@dataclass
class Budget:
    max_usd: float
    price: Price
    spent: float = 0.0

    @property
    def over(self) -> bool:
        return self.spent >= self.max_usd


class SpendMeter:
    """A meter that bills through `inner` and counts the cost; once the budget is spent it
    answers no, so a query in flight falls back to full text."""

    def __init__(self, inner: Meter, budget: Budget) -> None:
        self._inner, self._budget = inner, budget

    async def allowed(self, tenant_id: uuid.UUID) -> bool:
        return not self._budget.over and await self._inner.allowed(tenant_id)

    def priced(self, model: str) -> bool:
        return self._inner.priced(model)

    async def record(
        self,
        purpose: str,
        *,
        tenant_id: uuid.UUID,
        platform_user_id: str | None,
        model: str,
        units: int,
    ) -> None:
        self._budget.spent += self._budget.price(model, units)
        await self._inner.record(
            purpose,
            tenant_id=tenant_id,
            platform_user_id=platform_user_id,
            model=model,
            units=units,
        )


async def read_context(
    conn: AsyncConnection[DictRow], schemas: Schemas, caller: Caller
) -> ReadContext:
    """What the caller reads, as the `data_*` tools would resolve it."""
    scopes, readers = set(caller.scopes), set(caller.readers)
    if caller.account_id is not None:
        holders = await identity.list_holders(conn, schemas, caller.tenant_id, caller.account_id)
        scopes |= set(await access.effective_scopes(conn, schemas, caller.tenant_id, holders))
        readers.add(person(caller.account_id))
    return ReadContext(
        (caller.tenant_id,),
        tuple(sorted(scopes)),
        tuple(sorted(readers)),
        salt=f"eval:{caller.label}",
    )


async def expected_ids(
    conn: AsyncConnection[DictRow], schemas: Schemas, question: Question
) -> tuple[dict[uuid.UUID, int], list[str]]:
    """The expected records' ids with their grades, and the keys no record has."""
    gains: dict[uuid.UUID, int] = {}
    missing: list[str] = []
    for e in question.expected:
        cur = await conn.execute(
            f"select r.id from {schemas.data}.records r join {schemas.data}.sources s"
            " on s.id = r.source_id where s.tenant_id = %s and (s.name = %s or s.id::text = %s)"
            " and r.external_id = %s".encode(),
            (question.caller.tenant_id, e.source, e.source, e.external_id),
        )
        found = await cur.fetchall()
        gains |= {r["id"]: e.grade for r in found}
        if not found:
            missing.append(f"{e.source}:{e.external_id}")
    return gains, missing


async def visible_ids(
    conn: AsyncConnection[DictRow], schemas: Schemas, ctx: ReadContext, ids: Sequence[uuid.UUID]
) -> set[uuid.UUID]:
    """Which of `ids` the context can read."""
    async with access.scoped(conn, schemas, ctx):
        cur = await conn.execute(
            f"select id from {schemas.data}.records where id = any(%s)".encode(), (list(ids),)
        )
        return {r["id"] for r in await cur.fetchall()}


def arm_lists(hits: Sequence[Hit]) -> dict[str, list[uuid.UUID]]:
    """Each arm's ranked records, and the fused order. Full text always ran."""
    names = {"fts"} | {a for h in hits for a in h.arms}
    lists = {
        arm: [
            h.record_id
            for h in sorted((h for h in hits if arm in h.arms), key=lambda h: h.arms[arm])
        ]
        for arm in sorted(names)
    }
    return {"fused": [h.record_id for h in hits], **lists}


async def evaluate(
    conn: AsyncConnection[DictRow],
    schemas: Schemas,
    settings: DataSettings,
    gold: GoldSet,
    *,
    meter: Meter,
    budget: Budget,
    estimated: float,
) -> dict[str, Any]:
    """Run, grade and summarise; the report holds a row per question and a text summary."""
    spend = SpendMeter(meter, budget)
    rows: list[dict[str, Any]] = []
    graded: list[dict[str, Metrics]] = []
    halted: str | None = None
    for q in gold.questions:
        if budget.over:
            halted = f"spend ${budget.spent:.6f} reached the cap ${budget.max_usd:.2f}"
            break
        ctx = await read_context(conn, schemas, q.caller)
        wanted, missing = await expected_ids(conn, schemas, q)
        readable: set[uuid.UUID] = (
            await visible_ids(conn, schemas, ctx, list(wanted)) if wanted else set()
        )
        gains = {i: g for i, g in wanted.items() if i in readable}
        row: dict[str, Any] = {
            "id": q.id,
            "caller": q.caller.label,
            "expected": len(wanted),
            "visible": len(gains),
            "missing": missing,
        }
        if gains:
            result = await search(
                conn,
                schemas,
                ctx,
                q.q,
                settings=settings,
                meter=spend,
                payer=Payer(q.caller.tenant_id),
                limit=max(KS),
            )
            metrics = {arm: grade(ids, gains) for arm, ids in arm_lists(result.hits).items()}
            graded.append(metrics)
            row |= {"metrics": metrics, "notes": result.notes, "rerank": result.rerank}
        rows.append(row)
    summary = summarize(graded)
    report: dict[str, Any] = {
        "questions": len(gold.questions),
        "ran": len(rows),
        "graded": len(graded),
        "ungraded": len(rows) - len(graded),
        "arms": summary,
        "rows": rows,
        "halted": halted,
        "spend": {
            "estimated_usd": estimated,
            "metered_usd": budget.spent,
            "cap_usd": budget.max_usd,
        },
    }
    report["text"] = text_summary(report, summary)
    return report
