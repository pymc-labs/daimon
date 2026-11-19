"""Simulated turns: the real entry points and pipeline, provider HTTP from strict fakes."""

from __future__ import annotations

import pytest
from daimon.testing.db import db_clean as db_clean
from daimon.testing.db import db_engine as db_engine
from daimon.testing.db import db_nullpool_engine as db_nullpool_engine
from daimon.testing.db import db_schema as db_schema
from daimon.testing.pytest_plugin import db_preflight as db_preflight


@pytest.fixture(autouse=True)
def _fresh_database(db_clean: None) -> None:
    """Every simulated install starts from an empty database: a fresh tenant."""
