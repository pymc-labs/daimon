"""The rerank stage: the Vertex AI Ranking API orders the fused top candidates. It only orders;
on any failure, the timeout or a missing project the fused order stays, and the status says why."""

from __future__ import annotations

import asyncio
from typing import Final

import httpx
import structlog
from daimon.core.config import DataSettings
from daimon.data.metering import Meter, Payer, can_spend

log = structlog.get_logger()

MODEL: Final = "semantic-ranker-fast-005"
CHARS: Final = 2000
"""Of each candidate the ranker reads."""
TIMEOUT: Final = 4.0
UNITS: Final = 1000
"""Metered input units of one ranking request, which the rate table prices per thousand."""
URL: Final = (
    "https://us-discoveryengine.googleapis.com/v1/projects/{project}/locations/us"
    "/rankingConfigs/default_ranking_config:rank"
)
TOKEN_URL: Final = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
)

transport: httpx.AsyncBaseTransport | None = None
"""Tests answer for the metadata server and the API here."""


async def order(
    settings: DataSettings,
    query: str,
    texts: list[str],
    *,
    meter: Meter | None,
    payer: Payer | None,
    seconds: float,
) -> tuple[list[int] | None, str]:
    """The candidates' indexes best first, or None to keep the fused order, and the status:
    `off`, `vertex`, or `fallback: <why>`. A request is billed to the payer once it answered."""
    if settings.rerank == "off" or not texts:
        return None, "off"
    if not settings.rerank_project:
        return None, "fallback: no project"
    if meter is not None and not meter.priced(MODEL):
        return None, "fallback: unpriced"
    if not await can_spend(meter, payer):
        return None, "fallback: balance"
    try:
        async with asyncio.timeout(min(TIMEOUT, seconds)):
            async with httpx.AsyncClient(transport=transport) as http:
                t = await http.get(TOKEN_URL, headers={"Metadata-Flavor": "Google"})
                t.raise_for_status()
                r = await http.post(
                    URL.format(project=settings.rerank_project),
                    headers={"Authorization": f"Bearer {t.json()['access_token']}"},
                    json={
                        "model": MODEL,
                        "query": query,
                        "ignoreRecordDetailsInResponse": True,
                        "records": [
                            {"id": str(i), "content": s[:CHARS]} for i, s in enumerate(texts)
                        ],
                    },
                )
                r.raise_for_status()
                scores = {int(x["id"]): float(x.get("score", 0.0)) for x in r.json()["records"]}
    except TimeoutError:
        return None, "fallback: timeout"
    except Exception as e:  # a ranking stage fails open
        log.warning("data.rerank_failed", error=type(e).__name__)
        return None, "fallback: error"
    if meter is not None and payer is not None:
        try:
            await meter.record(
                "data_rerank",
                tenant_id=payer.tenant_id,
                platform_user_id=payer.platform_user_id,
                model=MODEL,
                units=UNITS,
            )
        except Exception:
            log.exception("data.rerank_unbilled", tenant=str(payer.tenant_id))
    ranked = sorted(range(len(texts)), key=lambda i: (-scores.get(i, float("-inf")), i))
    return ranked, "vertex"
