"""One read answers which conversations of a page have picked a model."""

from __future__ import annotations

from daimon.core.stores.thread_models import get_thread_choice, get_thread_choices, set_thread_model
from daimon.testing.factories import make_tenant
from mux.contracts.ids import ModelRef
from sqlalchemy.ext.asyncio import AsyncSession


async def test_choices_for_a_page_match_one_by_one(db_session: AsyncSession) -> None:
    tenant = await make_tenant(db_session)
    t = tenant.id
    await set_thread_model(
        db_session,
        tenant_id=t,
        platform="web",
        thread_id="a",
        model="gpt-6-luna",
        provider="openai",
    )
    await set_thread_model(
        db_session, tenant_id=t, platform="web", thread_id="b", model="claude-opus-5-5"
    )
    await set_thread_model(db_session, tenant_id=t, platform="web", thread_id="c", effort="low")

    got = await get_thread_choices(
        db_session, tenant_id=t, platform="web", thread_ids=["a", "b", "c", "d"]
    )

    assert got == {
        "a": ModelRef(provider="openai", id="gpt-6-luna"),
        "b": ModelRef(provider="anthropic", id="claude-opus-5-5"),
    }, "an effort-only or unset chat has no pick"
    for thread_id, ref in got.items():
        assert (
            await get_thread_choice(db_session, tenant_id=t, platform="web", thread_id=thread_id)
            == ref
        )
    assert await get_thread_choices(db_session, tenant_id=t, platform="web", thread_ids=[]) == {}
