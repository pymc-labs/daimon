"""The connect page's offer to follow an org: one checkbox, its rules and its words.

The offer shows only when the agent, after the save, holds every repo the person can see
in one org whose GitHub App installation covers all repos. The page's script shows and
hides it as repos are added and removed; `confirmed_offer` checks the same rule again on
the server.
"""

from __future__ import annotations

import html
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import Protocol

from daimon.core.stores.github_access import AgentRepo


@dataclass(frozen=True)
class FollowOffer:
    installation_id: int
    owner: str
    repo_ids: frozenset[int]
    """Every repo of the org the person can see and connect."""


class _Repo(Protocol):
    @property
    def id(self) -> int: ...
    @property
    def usable(self) -> bool: ...


class _Installation(Protocol):
    """What the offer reads from the page's installation list (`oauth_github._Installation`)."""

    @property
    def id(self) -> int: ...
    @property
    def owner_login(self) -> str: ...
    @property
    def repository_selection(self) -> str: ...
    @property
    def state(self) -> str: ...
    @property
    def fetch_state(self) -> str: ...
    @property
    def truncated(self) -> bool: ...
    @property
    def repos(self) -> Iterable[_Repo]: ...


def follow_offers(
    installations: Iterable[_Installation],
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


def follow_offers_html(offers: Iterable[FollowOffer], *, agent_name: str) -> str:
    """One hidden checkbox per offer; the page's script shows it when the org is covered."""
    esc = html.escape
    return "".join(
        '<label class="gh-follow-org" hidden '
        f'data-owner="{esc(offer.owner, quote=True)}" '
        f'data-ids="{",".join(str(i) for i in sorted(offer.repo_ids))}">'
        f'<input type="checkbox" name="follow_org" value="{offer.installation_id}" disabled>'
        f"<span><strong>Also give {esc(agent_name)} new repos added to {esc(offer.owner)}</strong>"
        "<small>It can read and change them.</small></span></label>"
        for offer in offers
    )


def following_note_html(*, agent_name: str) -> str:
    return (
        f'<p class="gh-follow-note">{html.escape(agent_name)} gets new repos added to its '
        "org on its own.</p>"
    )
