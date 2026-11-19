"""The eval key: what a turn ran with, recorded on its outcome and its session row."""

from __future__ import annotations

import uuid

import pytest
from daimon.core import turn_origin
from daimon.core._models import ThreadSession
from daimon.core.features.eval_key import EvalKey, sample_controls
from daimon.core.features.registry import FEATURES
from daimon.core.features.resolver import ResolvedFeature
from daimon.core.features.snapshot import FeaturePlace, FeatureSnapshot, resolve_one
from daimon.core.session_snapshot import fingerprint_identity, snapshot_from_retrieved_session
from daimon.core.stores.thread_sessions import create_thread_session
from daimon.core.stores.turn_outcomes import list_for_tenant
from daimon.core.turn.outcomes import TurnObservation, drain_outcomes
from daimon.testing.factories import make_tenant
from mux.testing.drivers.anthropic.ma_models import ma_session
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

PLACE = FeaturePlace(uuid.uuid4(), "slack", agent_id="agent_1", backend="anthropic")


def _features(*found: ResolvedFeature) -> FeatureSnapshot:
    defaults = {key: resolve_one(spec, None, PLACE, []) for key, spec in FEATURES.items()}
    return FeatureSnapshot(PLACE, 1, {**defaults, **{f.key: f for f in found}})


def test_key_names_the_session_prompts_platform_and_deciding_tiers() -> None:
    snapshot = snapshot_from_retrieved_session(ma_session())
    key = EvalKey.of(snapshot, _features(ResolvedFeature("guidance", "off", "agent")), "slack")
    assert key.identity == fingerprint_identity(snapshot), "identity is the session fingerprint"
    assert key.tools_sha256 == snapshot.tools_sha256, "tools hash comes from the snapshot"
    assert key.framing == "slack", "framing records the platform"
    assert key.features["guidance"] == ("off", "agent"), "value with the tier that decided it"
    assert set(key.features) == set(FEATURES), "every resolved feature is recorded"
    body = key.to_json()
    assert body["features"]["guidance"] == {"value": "off", "tier": "agent"}  # type: ignore[index]
    assert EvalKey.of(snapshot, _features(), "slack") != key, "a feature change changes the key"


def test_key_without_snapshot_or_features_still_names_the_prompts() -> None:
    key = EvalKey.of(None, None, "web")
    assert key.identity is None and key.tools_sha256 is None, "no snapshot reads as unknown"
    assert key.features == {}, "no features recorded"
    assert len(key.safety_sha256) == len(key.turn_controls_sha256) == 64, "prompt hashes are set"


def test_the_controls_hash_follows_the_rendered_wording(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hash is of the text a model reads, not of the code that renders it."""
    levels = sample_controls()
    assert len(set(levels)) == 2, "the controls read differently with and without web links"
    monkeypatch.setattr(turn_origin, "WEB_SENTENCE", " Other web wording.")
    assert sample_controls() != levels, "a changed sentence changes what is hashed"


async def test_key_is_written_to_the_session_row_and_the_turn_outcome(
    db_engine: AsyncEngine, db_clean: None
) -> None:
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    body = EvalKey.of(None, _features(), "slack").to_json()
    async with sessions() as session, session.begin():
        tenant = await make_tenant(session)
        row = await create_thread_session(
            session,
            tenant_id=tenant.id,
            platform="slack",
            thread_id="t",
            account_id=uuid.uuid4(),
            ma_session_id="sess_a",
            eval_key=body,
        )
    async with sessions() as session:
        stored = await session.scalar(
            select(ThreadSession.eval_key).where(ThreadSession.id == row.id)
        )
    assert stored == body, "the session row keeps the key it was created with"

    observation = TurnObservation(sessions, tenant.id, "slack", eval_key=body)
    observation.finish()
    await drain_outcomes()
    async with sessions() as session:
        outcomes = await list_for_tenant(session, tenant.id)
    assert [o.eval_key for o in outcomes] == [body], "the outcome row keeps the turn's key"
