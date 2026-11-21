"""Workflows ship off: every workflow feature resolves off, or to its limit, by default."""

from __future__ import annotations

import uuid
from decimal import Decimal

from daimon.core.features.registry import FEATURES
from daimon.core.features.snapshot import FeaturePlace, FeatureSnapshot, resolve_one
from daimon.core.workflows.features import KEYS, WorkflowFeatures, workflow_features


def test_workflow_features_default_off() -> None:
    place = FeaturePlace(uuid.uuid4(), "web", backend="anthropic")
    keys = (*KEYS, "web.workflows")
    snapshot = FeatureSnapshot(
        place, 1, {k: resolve_one(FEATURES[k], None, place, []) for k in keys}
    )
    assert workflow_features(snapshot) == WorkflowFeatures(
        enabled=False,
        webhooks=False,
        code=False,
        mentions=False,
        polls=False,
        scoped_writes=False,
        run_budget_usd=Decimal(5),
        retention_days=90,
    )
    assert not snapshot.on("web.workflows"), "the web module is off too"
