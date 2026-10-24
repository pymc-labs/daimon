"""Randomized parity: the SQL-bucketed spend reads equal pricing every row on its own."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest
from daimon.core._models import UsageEvent
from daimon.core.pricing import LONG_CONTEXT_PROMPT_TOKENS, MODEL_PRICING, cost_of
from daimon.core.stores import usage_events
from daimon.testing.factories import make_tenant, make_usage_event
from mux.testing.drivers.anthropic.ma_models import ma_model_usage
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

# Tiered, flat, snapshot-suffixed, tool, unpriced and empty model ids.
_MODELS = [
    "claude-haiku-5-5",
    "claude-haiku-5-5-20260901",
    "claude-opus-4-7",
    "claude-sonnet-5-5",
    "gemini-2.5-flash",
    "not-a-priced-model",
    "",
]
_USERS = ["u1", "u2", None]


def _tokens(rng: random.Random) -> tuple[int, int, int, int]:
    """(input, output, cache write, cache read); prompts cluster at the long-context threshold."""
    if rng.random() < 0.15:
        return 0, 0, 0, 0
    prompt = LONG_CONTEXT_PROMPT_TOKENS + rng.choice([-1, 0, 1, rng.randint(-99_000, 300_000)])
    cache_write = rng.choice([0, rng.randint(0, prompt)])
    cache_read = rng.choice([0, rng.randint(0, prompt - cache_write)])
    return (
        prompt - cache_write - cache_read,
        rng.choice([0, rng.randint(0, 50_000)]),
        cache_write,
        cache_read,
    )


@pytest.mark.parametrize("seed", [1, 7, 42])
async def test_cost_reads_equal_per_row_pricing_on_random_rows(
    db_session: AsyncSession, seed: int
) -> None:
    """Random rows over models, both tiers, the exact threshold, window edges, other users and tenants."""
    rng = random.Random(seed)
    tenant, other = await make_tenant(db_session), await make_tenant(db_session)
    since = datetime(2026, 10, 1, tzinfo=UTC)
    offsets = [
        timedelta(0),
        -timedelta(microseconds=1),
        timedelta(microseconds=1),
        timedelta(days=3),
    ]
    for _ in range(80):
        inp, out, cw, cr = _tokens(rng)
        row = await make_usage_event(
            db_session,
            tenant=rng.choice([tenant, tenant, other]),
            platform_user_id=rng.choice(_USERS),
            model=rng.choice(_MODELS),
            input_tokens=inp,
            output_tokens=out,
            cache_creation_input_tokens=cw,
            cache_read_input_tokens=cr,
        )
        await db_session.execute(
            update(UsageEvent)
            .where(UsageEvent.event_id == row.event_id)
            .values(occurred_at=since + rng.choice(offsets))
        )
    stored = (await db_session.execute(select(UsageEvent))).scalars().all()

    def per_row(user: str | None) -> float:
        """The former per-row sum, over the same filters."""
        total = 0.0
        for r in stored:
            if r.tenant_id != tenant.id or r.occurred_at < since or r.platform_user_id is None:
                continue
            if user is not None and r.platform_user_id != user:
                continue
            usage = ma_model_usage(
                input_tokens=r.input_tokens,
                output_tokens=r.output_tokens,
                cache_creation_input_tokens=r.cache_creation_input_tokens,
                cache_read_input_tokens=r.cache_read_input_tokens,
            )
            total += cost_of(usage, MODEL_PRICING.get(r.model)) or 0.0
        return total

    tenant_total = await usage_events.cost_for_tenant_since(
        db_session, tenant_id=tenant.id, since=since
    )
    assert tenant_total == pytest.approx(per_row(None), rel=1e-12, abs=1e-12), (
        "tenant aggregate must equal the per-row priced sum"
    )
    for user in ("u1", "u2"):
        spent = await usage_events.cost_for_user_in_tenant_since(
            db_session, tenant_id=tenant.id, platform_user_id=user, since=since
        )
        assert spent == pytest.approx(per_row(user), rel=1e-12, abs=1e-12), (
            f"{user} aggregate must equal that user's per-row priced sum"
        )
    assert tenant_total > 0, "fixture must exercise real costs"
