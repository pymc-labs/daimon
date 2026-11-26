"""The connect flow's offer to follow an org: who is offered it, and the proof a save needs.

The offer is for an admin of a GitHub org whose App installation covers all repos, when the
agent would hold every repo of it the person can see and none from another org. The editor
shows and hides it as repos are added and removed; `confirmed_offer` checks the same rule
again on the save, and `org_proof` asks GitHub live whether the person may hand the org over.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Sequence
from typing import Literal
from urllib.parse import quote

import httpx
from daimon.core.github_app_auth import (
    list_installation_repository_details,
    mint_installation_listing_token,
)
from daimon.core.github_connect_views import ConnectInstallation, FollowOffer
from daimon.core.stores.github_access import AgentRepo
from pydantic import TypeAdapter

type OrgProof = Literal["org_admin", "full_listing"]


def follow_offers(
    installations: Iterable[ConnectInstallation],
    grants: Collection[AgentRepo],
    *,
    may_follow: bool,
    following: bool,
) -> tuple[FollowOffer, ...]:
    """The orgs this save may offer to follow: whole-org installations only.

    An agent follows every whole-org installation it works in, so the offer is for an
    agent whose repos are all in one org. A list GitHub cut short cannot show that the
    agent holds every repo, so it gets no offer.
    """
    if not may_follow or following:
        return ()
    held = {repo.installation_id for repo in grants if repo.installation_id is not None}
    offers: list[FollowOffer] = []
    for installation in installations:
        if (
            installation.repository_selection != "all"
            or installation.owner_type.lower() != "organization"
            or installation.state != "active"
            or installation.fetch_state != "ok"
            or installation.truncated
            or held - {installation.id}
        ):
            continue
        ids = frozenset(repo.id for repo in installation.repos if repo.usable)
        if ids:
            offers.append(FollowOffer(installation.id, installation.owner_login, ids))
    return tuple(offers)


def confirmed_offer(
    offers: Iterable[FollowOffer],
    *,
    installation_id: int,
    held_after: Collection[int],
    installations_after: Collection[int],
) -> FollowOffer | None:
    """The offer the person ticked, if the save leaves the agent holding the whole org.

    `held_after` is the agent's repo ids after the save; `installations_after` the
    installations those repos sit in.
    """
    for offer in offers:
        if (
            offer.installation_id == installation_id
            and offer.repo_ids <= set(held_after)
            and set(installations_after) <= {installation_id}
        ):
            return offer
    return None


async def org_membership(
    client: httpx.AsyncClient, token: str, org: str
) -> Literal["admin", "other", "unknown"]:
    """The signer's standing in `org`, asked live: an active admin, anything else GitHub says
    about a member, or unknown when GitHub refuses the call. An App without the org members
    permission is refused with 403 ("Resource not accessible by integration"); a 404 may be
    that or a person who is no member, so both leave the answer unknown."""
    response = await client.get(
        f"https://api.github.com/user/memberships/orgs/{quote(org, safe='')}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    if response.status_code in (403, 404):
        return "unknown"
    response.raise_for_status()
    body = TypeAdapter(dict[str, object]).validate_python(response.json())
    return "admin" if body.get("state") == "active" and body.get("role") == "admin" else "other"


async def org_proof(
    client: httpx.AsyncClient, token: str, offer: FollowOffer, *, app_jwt: str
) -> OrgProof | None:
    """Which proof shows the signer may hand over the whole org, or None.

    First proof: GitHub says they are an active admin of the org. Second, when GitHub will
    not say: the repos GitHub shows the signer are every repo the App's installation lists
    (the listing the agent will follow), and neither listing was cut short. Someone who sees
    every repo of the org passes; a client's admin who sees one of them does not. The offer's
    repos are the signer's complete listing: an installation whose listing was cut short or
    failed is never offered.
    """
    standing = await org_membership(client, token, offer.owner)
    if standing == "admin":
        return "org_admin"
    if standing == "other":
        return None
    listing_token = await mint_installation_listing_token(
        client, jwt=app_jwt, installation_id=offer.installation_id
    )
    # `list_installation_repository_details` raises on any page it cannot complete.
    everything = await list_installation_repository_details(client, token=listing_token)
    ids = {repo.repo_id for repo in everything}
    if None in ids or not ids <= offer.repo_ids:
        return None
    return "full_listing"


async def org_admin_offers(
    factory: Callable[[], httpx.AsyncClient],
    token: str,
    offers: Sequence[FollowOffer],
    *,
    app_jwt: str,
) -> tuple[FollowOffer, ...]:
    """The offers the signer can prove an org for, so nobody is shown a box that would refuse
    them. A GitHub error hides the offer; the save asks again."""
    kept: list[FollowOffer] = []
    if offers:
        async with factory() as client:
            for offer in offers:
                try:
                    if await org_proof(client, token, offer, app_jwt=app_jwt) is not None:
                        kept.append(offer)
                except (httpx.HTTPError, ValueError):
                    continue
    return tuple(kept)
