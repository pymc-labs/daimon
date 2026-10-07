"""Tests for daimon.core.notebooks.publish.

Transport-level mocking via httpx.MockTransport per project testing skill.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from daimon.core.config import NotebookSettings
from daimon.core.notebooks import publish
from daimon.core.notebooks.publish import (
    _principal_prefix,  # pyright: ignore[reportPrivateUsage]
    _resolve_slug,  # pyright: ignore[reportPrivateUsage]
    delete_notebook,
    list_notebooks,
)
from pydantic import HttpUrl, SecretStr


def _make_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _settings() -> NotebookSettings:
    return NotebookSettings(
        host_url=HttpUrl("http://notebook-host:8001"), admin_secret=SecretStr("secret")
    )


async def test_delete_notebook_resolves_prefix_and_calls_host() -> None:
    seen: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path))
        return httpx.Response(200, json={"slug": "x", "deleted": True})

    async with _make_client(handler) as client:
        deleted = await delete_notebook(
            slug="radar-plots",
            notebook_settings=_settings(),
            client=client,
            principal_key="acct-1",
        )
    assert seen[0][0] == "DELETE"
    assert seen[0][1] == f"/admin/notebooks/{_principal_prefix('acct-1')}-radar-plots", (
        "delete must target the principal-prefixed slug on the unified notebooks route"
    )
    assert deleted is True, "the host's deleted flag must reach the caller"


async def test_delete_notebook_returns_false_when_host_removed_nothing() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"slug": "x", "deleted": False})

    async with _make_client(handler) as client:
        deleted = await delete_notebook(
            slug="ghost",
            notebook_settings=_settings(),
            client=client,
            principal_key="acct-1",
        )
    assert deleted is False, (
        "deleting a slug that never existed must be distinguishable from a real removal"
    )


async def test_list_notebooks_strips_the_prefix_so_slugs_round_trip_into_delete() -> None:
    """The slug a caller reads back is the slug delete accepts.

    Handing back the namespaced form is what made delete a silent no-op: it
    prefixes once itself, so a namespaced input becomes double-prefixed and
    targets nothing.
    """
    mine = f"{_principal_prefix('acct-1')}-radar"
    theirs = f"{_principal_prefix('acct-2')}-radar"

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/admin/blogs"):
            return httpx.Response(200, json={"blogs": [{"slug": mine}, {"slug": theirs}]})
        return httpx.Response(200, json={"notebooks": []})

    async with _make_client(handler) as client:
        result = await list_notebooks(
            notebook_settings=_settings(), client=client, principal_key="acct-1"
        )

    assert [entry["slug"] for entry in result] == ["radar"], (
        "only the caller's notebook, reported under its bare authoring name"
    )
    assert result[0]["permanent"] is True, "a registered blog is permanent"


async def test_list_notebooks_merges_both_kinds_without_duplicating_a_live_blog() -> None:
    blog = f"{_principal_prefix('acct-1')}-published"
    scratch = f"{_principal_prefix('acct-1')}-draft"

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/admin/blogs"):
            return httpx.Response(200, json={"blogs": [{"slug": blog, "title": "Published"}]})
        # A live blog is also a tracked process, so the host lists it twice overall.
        return httpx.Response(
            200,
            json={"notebooks": [{"slug": blog, "alive": True}, {"slug": scratch, "alive": True}]},
        )

    async with _make_client(handler) as client:
        result = await list_notebooks(
            notebook_settings=_settings(), client=client, principal_key="acct-1"
        )

    by_slug = {entry["slug"]: entry for entry in result}
    assert sorted(by_slug) == ["draft", "published"], "each notebook appears exactly once"
    assert by_slug["published"]["permanent"] is True, "the registered one is the blog"
    assert by_slug["published"]["title"] == "Published", (
        "the blog record wins over its process-list twin, so title survives the merge"
    )
    assert by_slug["draft"]["permanent"] is False, "an unregistered process is a scratch notebook"


async def test_list_notebooks_includes_a_stopped_scratch_notebook_with_its_expiry() -> None:
    """A read-only scratch notebook is in the host's registry even while stopped."""
    stopped = f"{_principal_prefix('acct-1')}-draft"
    expires_at = "2026-11-06T12:00:00+00:00"

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/admin/blogs"):
            return httpx.Response(
                200, json={"blogs": [{"slug": stopped, "expires_at": expires_at, "url": None}]}
            )
        return httpx.Response(200, json={"notebooks": []})

    async with _make_client(handler) as client:
        result = await list_notebooks(
            notebook_settings=_settings(), client=client, principal_key="acct-1"
        )

    assert [entry["slug"] for entry in result] == ["draft"], "a stopped notebook is still listed"
    assert result[0]["permanent"] is False, "a registry entry with an expiry is not a blog"
    assert result[0]["expires_at"] == expires_at, "the caller sees when it will be deleted"


# An account whose 12-character tag happens to start with "-".
_DASH_ACCOUNT = "be834354-be02-48f2-853d-b5dc5e0e3916"


def test_a_namespaced_slug_never_starts_with_a_dash() -> None:
    """The host refuses a leading "-" (it would read as a marimo argv flag).

    About 1 account in 64 has a tag starting with "-", and the host refused
    every name it published.
    """
    slug = _resolve_slug(agent_slug="churn", principal_key=_DASH_ACCOUNT)
    assert not slug.startswith("-"), slug
    assert slug.endswith("-churn"), "the agent's name is kept"


def test_an_existing_namespace_does_not_move() -> None:
    """Notebooks already published are found by their tag, so it must stay put."""
    assert _resolve_slug(agent_slug="churn", principal_key="acct-1") == "70F2qUGX_5Fu-churn"


def test_a_random_slug_never_starts_with_a_dash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(publish.secrets, "token_urlsafe", lambda _n: "-AbCdEfGhIjKlMnOpQrStUv")  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]

    assert not _resolve_slug(agent_slug=None, principal_key=None).startswith("-")


async def test_list_notebooks_round_trips_a_slug_for_a_dash_tagged_account() -> None:
    namespaced = _resolve_slug(agent_slug="radar", principal_key=_DASH_ACCOUNT)

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/admin/blogs"):
            return httpx.Response(200, json={"blogs": [{"slug": namespaced}]})
        return httpx.Response(200, json={"notebooks": []})

    async with _make_client(handler) as client:
        result = await list_notebooks(
            notebook_settings=_settings(), client=client, principal_key=_DASH_ACCOUNT
        )

    assert [entry["slug"] for entry in result] == ["radar"], (
        "the bare name comes back, so it still round-trips into delete"
    )
