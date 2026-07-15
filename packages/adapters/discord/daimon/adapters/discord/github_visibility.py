"""GitHub repo public-visibility check for the anon-bind guardrail.

Lives in the Discord adapter (not core): core must not import adapters, and the
bind path is already adapter-side I/O. The operator fallback PAT only clones
``anon:`` bindings, so an ``anon:`` binding must be verified-public at bind time
— otherwise a guild could bind a private repo as ``anon:`` and clone it
cross-tenant with the operator token.
"""

from __future__ import annotations

import httpx

__all__ = ["is_public_repo", "pat_can_access_repo"]


async def is_public_repo(http_client: httpx.AsyncClient, *, owner_repo: str) -> bool:
    """Return True iff the GitHub repo ``owner/repo`` is public.

    GETs ``https://api.github.com/repos/{owner_repo}``:
    - 200 with ``private == false`` → True (verified public).
    - 200 with ``private == true`` → False.
    - 404 → False (nonexistent repo treated as not-public).
    - any other status → raises (let failures propagate; never a sentinel).

    The ``httpx.AsyncClient`` is injected — no module-level client.
    """
    resp = await http_client.get(f"https://api.github.com/repos/{owner_repo}")
    if resp.status_code == 404:
        return False
    resp.raise_for_status()
    body: dict[str, object] = resp.json()
    return body.get("private") is False


async def pat_can_access_repo(http_client: httpx.AsyncClient, *, owner_repo: str, pat: str) -> bool:
    """Return True iff ``pat`` grants read access to the GitHub repo ``owner/repo``.

    GETs ``https://api.github.com/repos/{owner_repo}`` with the PAT:
    - 200 → True (the token can see the repo; public or private-with-access).
    - 401 / 403 / 404 → False (bad token, or no access — GitHub returns 404 for
      private repos the token cannot see, to avoid leaking existence).
    - any other status → raises (let failures propagate; never a sentinel).

    Load-bearing for tenant isolation: without it, a tenant could bind a repo
    it does not control by pasting a junk PAT, then ride the deployment's
    GitHub App installation token (which is keyed by repo, not tenant) to clone
    another tenant's private repo on the next webhook resync.
    """
    resp = await http_client.get(
        f"https://api.github.com/repos/{owner_repo}",
        headers={"Authorization": f"Bearer {pat}"},
    )
    if resp.status_code in (401, 403, 404):
        return False
    resp.raise_for_status()
    return True
