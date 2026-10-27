"""Which build is running: the commit baked into the image, else the package version."""

from __future__ import annotations

import os
from importlib.metadata import version

__all__ = ["current_release"]


def current_release() -> str:
    """`DAIMON_RELEASE` (the full commit sha set at image build) when non-empty,
    otherwise the installed `daimon-core` version."""
    return os.environ.get("DAIMON_RELEASE", "").strip() or version("daimon-core")
