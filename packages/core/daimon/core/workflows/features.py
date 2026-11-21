"""The workflow features of one place, read once from its `FeatureSnapshot`.

Every workflow path (runs, webhooks, code steps, the web module, the sweep) reads these
here, so a feature means the same everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from daimon.core.features.snapshot import FeatureSnapshot

KEYS = (
    "workflows",
    "workflow_webhooks",
    "workflow_code",
    "workflow_mentions",
    "workflow_polls",
    "workflow_scoped_writes",
    "workflow_run_budget",
    "workflow_retention_days",
)


@dataclass(frozen=True)
class WorkflowFeatures:
    enabled: bool
    webhooks: bool
    code: bool
    mentions: bool
    polls: bool
    scoped_writes: bool
    run_budget_usd: Decimal
    retention_days: int


def workflow_features(features: FeatureSnapshot) -> WorkflowFeatures:
    """`features` must hold `KEYS`."""
    return WorkflowFeatures(
        enabled=features.on("workflows"),
        webhooks=features.on("workflow_webhooks"),
        code=features.on("workflow_code"),
        mentions=features.on("workflow_mentions"),
        polls=features.on("workflow_polls"),
        scoped_writes=features.on("workflow_scoped_writes"),
        run_budget_usd=Decimal(features.value("workflow_run_budget")),
        retention_days=int(features.value("workflow_retention_days")),
    )
