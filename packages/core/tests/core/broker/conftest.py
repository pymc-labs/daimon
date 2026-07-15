"""Shared fixtures for the broker test package.

The DB fixtures (`db_engine`, `db_session`, `db_session_factory`) live in
`packages/core/tests/conftest.py` and are automatically discovered by pytest
via the rootdir conftest hierarchy. This module only adds broker-specific
fixtures (currently: `google_sa_info`).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The test-tree-level sys.path hack was removed when daimon.testing absorbed
# the shared factories.  The broker package still has a core-specific factory
# (google_sa) that lives under packages/core/tests/factories/.
_tests_dir = str(Path(__file__).resolve().parents[2])
if _tests_dir not in sys.path:
    sys.path.insert(0, _tests_dir)

from factories.google_sa import make_test_service_account_info  # noqa: E402


@pytest.fixture
def google_sa_info() -> dict[str, str]:
    """Fresh ephemeral RSA-keyed Service Account JSON dict."""
    return make_test_service_account_info()
