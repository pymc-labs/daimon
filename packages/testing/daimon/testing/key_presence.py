"""Let credential spies tell the one bool-only key-presence check from a real read.

Tests that forbid reading a provider credential (an `os.environ.get` spy) still
let `daimon.core.provider_keys.provider_key_present` look: it answers only
whether the deployment's key is set and never keeps or uses the value.
"""

from __future__ import annotations

import sys

from daimon.core.provider_keys import provider_key_present


def called_by_key_presence() -> bool:
    """Whether the spied `os.environ.get` call came straight from `provider_key_present`."""
    # 0: this function, 1: the spy, 2: its caller.
    return sys._getframe(2).f_code is provider_key_present.__code__  # pyright: ignore[reportPrivateUsage]
