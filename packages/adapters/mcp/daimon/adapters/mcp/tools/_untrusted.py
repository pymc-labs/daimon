"""The trust marker on daimon's own read and search tool results.

These tools return what other people wrote. The rows stay plain JSON (callers
page and filter on them), so instead of an XML envelope every result carries
the same `trust="untrusted"` marker and note the envelope uses
(`daimon.core.untrusted`), and the default prompt clause says what it means.
"""

from __future__ import annotations

from typing import Literal

from daimon.core.untrusted import UNTRUSTED_NOTE
from pydantic import BaseModel

UntrustedMarker = Literal["untrusted"]


class UntrustedResult(BaseModel):
    """Mixin for a result whose rows are other people's text."""

    trust: UntrustedMarker = "untrusted"
    trust_note: str = UNTRUSTED_NOTE
