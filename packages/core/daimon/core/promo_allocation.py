"""How spend draws on timed promo credit. Pure: the shell supplies grants and spend.

A timed grant is credit that exists only in its window ``[starts_at, ends_at)``.
Spend inside a window draws on timed credit before anything else, the grant
that ends first before later ones, so a tenant loses as little as possible
when a window closes. The ledger balance is still ``SUM(delta_usd)``; this
only decides how much of a grant is unspent when it expires.

Windows that overlap, directly or through a chain, form a cluster; spend in a
cluster's span can only reach that cluster's grants, so a question about some
grants needs only their clusters' spend.
"""

from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Collection, Sequence
from datetime import datetime
from decimal import Decimal


@dataclasses.dataclass(frozen=True)
class TimedGrant:
    promo_code_id: uuid.UUID
    amount_usd: Decimal
    starts_at: datetime  # when the credit reached the ledger
    ends_at: datetime  # the code's credit end


def relevant_grants(
    grants: Sequence[TimedGrant], *, targets: Collection[uuid.UUID], horizon: datetime
) -> list[TimedGrant]:
    """Grants sharing a cluster with any target, among those started before ``horizon``."""
    started = sorted(
        (g for g in grants if g.starts_at < min(g.ends_at, horizon)),
        key=lambda g: g.starts_at,
    )
    clusters: list[list[TimedGrant]] = []
    cluster_end: datetime | None = None
    for grant in started:
        if cluster_end is None or grant.starts_at >= cluster_end:
            clusters.append([])
            cluster_end = grant.ends_at
        clusters[-1].append(grant)
        cluster_end = max(cluster_end, grant.ends_at)
    return [
        grant
        for cluster in clusters
        if any(g.promo_code_id in targets for g in cluster)
        for grant in cluster
    ]


def spend_bounds(grants: Sequence[TimedGrant], *, horizon: datetime) -> list[datetime]:
    """Sorted interval edges up to ``horizon``; each interval has one set of live grants."""
    edges = {g.starts_at for g in grants} | {min(g.ends_at, horizon) for g in grants}
    return sorted(edge for edge in edges if edge <= horizon)


def remaining_timed_credit(
    grants: Sequence[TimedGrant], *, bounds: Sequence[datetime], spend: Sequence[Decimal]
) -> dict[uuid.UUID, Decimal]:
    """Unspent credit per grant after ``spend[i]`` fell in ``[bounds[i], bounds[i+1])``."""
    if len(spend) != max(len(bounds) - 1, 0):
        raise ValueError("spend needs one amount per interval between bounds")
    remaining = {g.promo_code_id: g.amount_usd for g in grants}
    by_end = sorted(grants, key=lambda g: (g.ends_at, g.starts_at, str(g.promo_code_id)))
    for index, amount in enumerate(spend):
        lower, upper = bounds[index], bounds[index + 1]
        left = amount
        for grant in by_end:
            if left <= 0:
                break
            if grant.starts_at <= lower and upper <= grant.ends_at:
                taken = min(remaining[grant.promo_code_id], left)
                remaining[grant.promo_code_id] -= taken
                left -= taken
    return remaining
