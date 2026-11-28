"""Whether the data store runs in this process."""

from __future__ import annotations

from daimon.core.config import Settings
from daimon.core.features.snapshot import deployment_on


def data_sources_on(settings: Settings) -> bool:
    """The `data_sources` feature is on for the deployment; a workspace can still turn it off."""
    return deployment_on(settings, "data_sources", "web")
