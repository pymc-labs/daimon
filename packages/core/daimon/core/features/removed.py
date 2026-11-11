"""Settings an upgrade may still carry: removed features and fields start with a warning.

The one list of what was removed. Anything else unknown still stops startup.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, cast

import structlog

log = structlog.get_logger(__name__)

REMOVED_FEATURE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "daimon_tools",
        "turn_controls",
        "checkpoint_prompt",
        "artifact_images",
        "status_log",
        "native_footer_cost",
        "unmentioned_reply_hint",
        "writers_none_notice",
        "waiting_card",
    }
)
"""Features dropped from the registry; `DAIMON_FEATURES__<KEY>` for one is ignored."""

REMOVED_FIELDS: Final[Mapping[str, frozenset[str]]] = {
    "tool_safety": frozenset({"allow_gate_overrides"}),
}
"""Fields dropped from a strict settings block, by block name; the block rejects extras."""


def without_removed_features(features: Mapping[str, str]) -> dict[str, str]:
    """The deployment features minus removed keys, each one logged by its variable name."""
    kept: dict[str, str] = {}
    for key, raw in features.items():
        if key.lower() in REMOVED_FEATURE_KEYS:
            log.warning(
                "settings.removed_variable_ignored", variable=f"DAIMON_FEATURES__{key.upper()}"
            )
        else:
            kept[key] = raw
    return kept


def without_removed_fields(values: Mapping[str, object]) -> dict[str, object]:
    """Raw settings input minus removed fields of strict blocks, each one logged by its variable."""
    cleaned = dict(values)
    for block, names in REMOVED_FIELDS.items():
        found = cleaned.get(block)
        if not isinstance(found, dict):
            continue
        fields = cast("dict[str, object]", found)
        for name in sorted(names.intersection(key.lower() for key in fields)):
            log.warning(
                "settings.removed_variable_ignored", variable=f"DAIMON_{block}__{name}".upper()
            )
        cleaned[block] = {key: v for key, v in fields.items() if key.lower() not in names}
    return cleaned
