"""Inside the offline MCP pod: verify production CLI admission/metering persisted."""

import asyncio
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


async def main():
    engine = create_async_engine(os.environ["DAIMON_DATABASE__URL"])
    async with engine.connect() as connection:
        # Fixture reports one measured request per turn, exactly 10 input / 4 output.
        rows = (
            await connection.execute(
                text(
                    "SELECT input_tokens, output_tokens FROM usage_events WHERE channel_id = 'selfhosted-operator'"
                )
            )
        ).all()
        assert len(rows) == 2 and all(tuple(row) == (10, 4) for row in rows), rows
        count = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM tenant_ledger WHERE delta_usd < 0 AND channel_id = 'selfhosted-operator'"
                )
            )
        ).scalar_one()
        assert count == 2, count
    await engine.dispose()
    print("Two measured CLI usage rows and two ledger debits persisted")


asyncio.run(main())
