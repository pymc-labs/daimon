"""Re-export of daimon.core.privacy types for adapter-side imports.

The adapter does not derive any state beyond the core PurgePreview snapshot;
this module exists for symmetry with billing_panel/state.py and so adapter tests
can import from a single adapter-local path.
"""

from __future__ import annotations

from daimon.core.privacy import PurgePreview, PurgePreviewRow

__all__ = ["PurgePreview", "PurgePreviewRow"]
