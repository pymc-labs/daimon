import asyncio
import json
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


async def main():
    engine = create_async_engine(os.environ["DAIMON_DATABASE__URL"])
    result = {}
    async with engine.connect() as db:
        for table in [
            "usage_events",
            "tenant_ledger",
            "usage_observation",
            "accounting_outbox",
            "journal",
            "provider_binding",
        ]:
            rows = (
                (
                    await db.execute(
                        text("SELECT to_jsonb(t) FROM " + table + " t WHERE tenant_id=:tenant"),
                        {"tenant": "91495665-ea07-561f-b769-d05d875385e2"},
                    )
                )
                .scalars()
                .all()
            )
            result[table] = rows
    print(json.dumps(result, default=str))
    await engine.dispose()


asyncio.run(main())
