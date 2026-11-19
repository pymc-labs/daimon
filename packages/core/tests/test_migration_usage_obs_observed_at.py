"""The usage_observation observed_at backfill fills every old row across batches."""

import importlib.util
from pathlib import Path
from types import ModuleType

from sqlalchemy import text

from .native_usage_support import pending_usage
from .native_usage_support import sm as sm


def migration() -> ModuleType:
    path = Path(__file__).parents[1] / "alembic/versions/0216_usage_obs_observed_at.py"
    spec = importlib.util.spec_from_file_location("usage_obs_observed_at", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_backfill_fills_rows_older_code_wrote_in_every_batch(sm) -> None:
    for _ in range(5):
        await pending_usage(sm, provider="openai")
    async with sm.begin() as db:
        await db.execute(text("UPDATE usage_observation SET provider = NULL, observed_at = NULL"))
        connection = await db.connection()
        await connection.run_sync(lambda sync: migration().backfill(sync, size=2))
        unfilled = await db.scalar(
            text(
                "SELECT count(*) FROM usage_observation WHERE provider IS NULL OR observed_at IS NULL"
            )
        )
        filled = await db.scalar(
            text("SELECT count(*) FROM usage_observation WHERE provider = 'openai'")
        )
    assert unfilled == 0 and filled == 5, "each row is filled once, none skipped between batches"
