"""Thin httpx client to the notebook host's admin API.

Pure function — takes the http client + creds explicitly (no globals).
"""

from __future__ import annotations

from typing import Any, cast

import httpx
from daimon.core.errors import DaimonError
from pydantic import HttpUrl, SecretStr


class NotebookHostError(DaimonError):
    """Raised when the notebook host rejects or fails a publish."""


class NotebookValidationError(NotebookHostError):
    """The host ran the notebook and its cells failed to execute (HTTP 422).

    Subclasses ``NotebookHostError`` so existing ``except NotebookHostError``
    sites still catch it. Carries
    the per-cell error lines the host extracted so the caller can show the
    agent exactly what to fix.
    """

    def __init__(self, message: str, *, cell_errors: list[str]) -> None:
        super().__init__(message)
        self.cell_errors = cell_errors


def _parse_cell_errors(response: httpx.Response) -> list[str]:
    """Pull ``detail.cell_errors`` out of the host's 422 body, defensively."""
    try:
        body: Any = response.json()
    except ValueError:
        return []
    try:
        errors: Any = body["detail"]["cell_errors"]
    except (KeyError, TypeError, IndexError):
        return []
    if isinstance(errors, list):
        return [str(e) for e in cast("list[Any]", errors)]
    return []


async def attach_to_host(
    *,
    slug: str,
    name: str,
    content: bytes,
    host_url: HttpUrl,
    admin_secret: SecretStr,
    client: httpx.AsyncClient,
) -> dict[str, object]:
    """PUT /admin/notebooks/{slug}/data/{name} on the notebook host.

    Body is raw bytes (Content-Type: application/octet-stream). Returns the
    host's response JSON (slug, name, size_bytes, path). Raises
    NotebookHostError on non-2xx.
    """
    url = f"{str(host_url).rstrip('/')}/admin/notebooks/{slug}/data/{name}"
    headers = {
        "Authorization": f"Bearer {admin_secret.get_secret_value()}",
        "Content-Type": "application/octet-stream",
    }
    r = await client.put(url, content=content, headers=headers, timeout=30.0)
    if not r.is_success:
        raise NotebookHostError(f"notebook host returned {r.status_code}: {r.text[:200]}")
    return r.json()  # type: ignore[no-any-return]


async def publish_blog_to_host(
    *,
    slug: str,
    source: str,
    host_url: HttpUrl,
    admin_secret: SecretStr,
    client: httpx.AsyncClient,
) -> dict[str, object]:
    """PUT /admin/blogs/{slug} — publish a permanent run-mode blog.

    Same shape as publish_to_host but hits the blog route; the host spawns
    `marimo run` and records the slug in its registry. Returns the host body
    (slug, url, port, pid, size_bytes — no expires_at). Maps 422 → validation
    error, other non-2xx → NotebookHostError.
    """
    url = f"{str(host_url).rstrip('/')}/admin/blogs/{slug}"
    headers = {"Authorization": f"Bearer {admin_secret.get_secret_value()}"}
    r = await client.put(url, json={"source": source}, headers=headers, timeout=30.0)
    if r.status_code == 422:
        raise NotebookValidationError(
            "notebook failed validation", cell_errors=_parse_cell_errors(r)
        )
    if not r.is_success:
        raise NotebookHostError(f"notebook host returned {r.status_code}: {r.text[:200]}")
    return r.json()  # type: ignore[no-any-return]


async def delete_blog_from_host(
    *,
    slug: str,
    host_url: HttpUrl,
    admin_secret: SecretStr,
    client: httpx.AsyncClient,
) -> None:
    """DELETE /admin/blogs/{slug}. A 404 (already gone) is treated as success."""
    url = f"{str(host_url).rstrip('/')}/admin/blogs/{slug}"
    headers = {"Authorization": f"Bearer {admin_secret.get_secret_value()}"}
    r = await client.delete(url, headers=headers, timeout=30.0)
    if not r.is_success and r.status_code != 404:
        raise NotebookHostError(f"notebook host returned {r.status_code}: {r.text[:200]}")


async def list_blogs_from_host(
    *,
    host_url: HttpUrl,
    admin_secret: SecretStr,
    client: httpx.AsyncClient,
) -> dict[str, object]:
    """GET /admin/blogs — returns the host body ({"blogs": [...]})."""
    url = f"{str(host_url).rstrip('/')}/admin/blogs"
    headers = {"Authorization": f"Bearer {admin_secret.get_secret_value()}"}
    r = await client.get(url, headers=headers, timeout=30.0)
    if not r.is_success:
        raise NotebookHostError(f"notebook host returned {r.status_code}: {r.text[:200]}")
    return r.json()  # type: ignore[no-any-return]
