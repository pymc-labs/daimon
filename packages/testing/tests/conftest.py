from datetime import UTC, datetime

import pytest
from daimon.testing.db import (  # noqa: F401
    db_clean,  # pyright: ignore[reportUnusedImport]
    db_committing_engine,  # pyright: ignore[reportUnusedImport]
    db_committing_pool,  # pyright: ignore[reportUnusedImport]
    db_engine,  # pyright: ignore[reportUnusedImport]
    db_nullpool_engine,  # pyright: ignore[reportUnusedImport]
    db_schema,  # pyright: ignore[reportUnusedImport]
    db_session,  # pyright: ignore[reportUnusedImport]
    db_session_factory,  # pyright: ignore[reportUnusedImport]
)

# Recorded Gemini interactions are stamped 2026-10-10 to 2026-10-12, and the driver
# expires a workspace seven days after its last activity: replay them at their own time.
GEMINI_RECORDED_AT = datetime(2026, 10, 13, tzinfo=UTC)


@pytest.fixture
def gemini_recorded_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("mux.drivers.gemini.core.now", lambda: GEMINI_RECORDED_AT)
