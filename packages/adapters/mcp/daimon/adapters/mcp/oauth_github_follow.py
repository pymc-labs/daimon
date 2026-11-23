"""The connect page's markup for following an org: one checkbox and one note.

Whether to offer it, and whether a save may turn it on, is decided in
`daimon.core.github_connect_flow`; this module only draws what the view carries.
The page's script shows the box once the agent would hold every repo of the org.
"""

from __future__ import annotations

import html
from collections.abc import Iterable

from daimon.core.github_connect_views import FollowOffer


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
