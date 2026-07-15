"""daimon notebook … sub-app — operator file-based uploads + blog management.

The operator holds the admin secret, so these hit the host's /admin/* routes
directly (no capability token — that exists for the secret-less agent). Files
are read from local disk and streamed to the host, so there is no token-stream
size limit. Slugs are used verbatim (the operator owns the full namespace).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, cast

import httpx
import typer
from daimon.adapters.cli.errors import run_cli
from daimon.adapters.cli.flags import JSON_OPTION, YES_OPTION
from daimon.adapters.cli.prompt import confirm_or_abort
from daimon.core.config import NotebookSettings, load_settings
from daimon.core.notebooks.host_client import (
    NotebookHostError,
    NotebookValidationError,
    attach_to_host,
    delete_blog_from_host,
    list_blogs_from_host,
    publish_blog_to_host,
)
from pydantic import HttpUrl, SecretStr
from rich.console import Console
from rich.table import Table

notebook_app = typer.Typer(help="Notebook host: publish blogs, attach data, manage blogs.")

_FILE_OPTION = typer.Option("--file", "-f", help="Local file to upload.")

# publish-blog triggers server-side validation: the host boots a marimo
# subprocess (spawn_timeout 20s) and runs the notebook (validation_timeout 60s).
# A cold first publish — sandbox installing the notebook's PEP 723 deps — can
# approach that ceiling, so the client must wait past the host's window or it
# ReadTimeouts on a publish the host would have completed.
_PUBLISH_TIMEOUT_SECONDS = 90.0


def _require_host(settings: NotebookSettings, console: Console) -> tuple[HttpUrl, SecretStr]:
    """Return (host_url, admin_secret) or print an error and exit with code 3."""
    if settings.host_url is None or settings.admin_secret is None:
        console.print(
            "[red]notebook host not configured — set DAIMON_NOTEBOOK__HOST_URL and "
            "DAIMON_NOTEBOOK__ADMIN_SECRET.[/red]"
        )
        raise typer.Exit(code=3)
    return settings.host_url, settings.admin_secret


# ---------------------------------------------------------------------------
# publish-blog
# ---------------------------------------------------------------------------


@notebook_app.command("publish-blog")
def publish_blog_command(
    slug: Annotated[str, typer.Argument(help="Public blog slug (used verbatim).")],
    file: Annotated[str, _FILE_OPTION],
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _go() -> None:
        await publish_blog_file(settings.notebook, console, slug=slug, file=file)

    run_cli(_go(), console=console)


async def publish_blog_file(
    settings: NotebookSettings,
    console: Console,
    *,
    slug: str,
    file: str,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    host_url, admin_secret = _require_host(settings, console)
    source = Path(file).read_text(encoding="utf-8")

    async def _put(http: httpx.AsyncClient) -> dict[str, object]:
        return await publish_blog_to_host(
            slug=slug, source=source, host_url=host_url, admin_secret=admin_secret, client=http
        )

    try:
        if http_client is not None:
            body = await _put(http_client)
        else:
            async with httpx.AsyncClient(timeout=_PUBLISH_TIMEOUT_SECONDS) as http:
                body = await _put(http)
    except NotebookValidationError as err:
        detail = "\n".join(f"  - {line}" for line in err.cell_errors) or "  (no detail)"
        console.print(f"[red]blog failed validation — cells did not execute:[/red]\n{detail}")
        raise typer.Exit(code=4) from err
    except NotebookHostError as err:
        console.print(f"[red]{err}[/red]")
        raise typer.Exit(code=5) from err
    url = str(body.get("url", ""))
    console.print(f"[green]✓ published blog {slug!r}[/green] → {url}")


# ---------------------------------------------------------------------------
# attach
# ---------------------------------------------------------------------------


@notebook_app.command("attach")
def attach_command(
    slug: Annotated[str, typer.Argument(help="Notebook/blog slug (used verbatim).")],
    name: Annotated[str, typer.Argument(help="Attachment name → data/<name>.")],
    file: Annotated[str, _FILE_OPTION],
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _go() -> None:
        await attach_file(settings.notebook, console, slug=slug, name=name, file=file)

    run_cli(_go(), console=console)


async def attach_file(
    settings: NotebookSettings,
    console: Console,
    *,
    slug: str,
    name: str,
    file: str,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    host_url, admin_secret = _require_host(settings, console)
    content = Path(file).read_bytes()

    async def _put(http: httpx.AsyncClient) -> dict[str, object]:
        return await attach_to_host(
            slug=slug,
            name=name,
            content=content,
            host_url=host_url,
            admin_secret=admin_secret,
            client=http,
        )

    try:
        if http_client is not None:
            body = await _put(http_client)
        else:
            async with httpx.AsyncClient(timeout=30.0) as http:
                body = await _put(http)
    except NotebookHostError as err:
        console.print(f"[red]{err}[/red]")
        raise typer.Exit(code=5) from err
    path = str(body.get("path", ""))
    console.print(f"[green]✓ attached {name!r}[/green] → {path}")


# ---------------------------------------------------------------------------
# list-blogs
# ---------------------------------------------------------------------------

_BLOG_COLUMNS = ("slug", "url", "alive", "created_at")


@notebook_app.command("list-blogs")
def list_blogs_command(as_json: Annotated[bool, JSON_OPTION] = False) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _go() -> None:
        await list_blogs_op(settings.notebook, console, as_json=as_json)

    run_cli(_go(), console=console)


async def list_blogs_op(
    settings: NotebookSettings,
    console: Console,
    *,
    as_json: bool,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    host_url, admin_secret = _require_host(settings, console)

    async def _get(http: httpx.AsyncClient) -> dict[str, object]:
        return await list_blogs_from_host(host_url=host_url, admin_secret=admin_secret, client=http)

    try:
        if http_client is not None:
            body = await _get(http_client)
        else:
            async with httpx.AsyncClient(timeout=30.0) as http:
                body = await _get(http)
    except NotebookHostError as err:
        console.print(f"[red]{err}[/red]")
        raise typer.Exit(code=5) from err

    blogs_any: Any = body.get("blogs", [])
    blogs: list[dict[str, object]] = []
    if isinstance(blogs_any, list):
        for entry in cast("list[Any]", blogs_any):
            if isinstance(entry, dict):
                blogs.append(cast("dict[str, object]", entry))

    if as_json:
        console.print(json.dumps(blogs), soft_wrap=True, highlight=False, markup=False)
        return

    table = Table(show_header=True, header_style="bold")
    for col in _BLOG_COLUMNS:
        table.add_column(col)
    for blog in blogs:
        table.add_row(*(str(blog.get(c, "")) for c in _BLOG_COLUMNS))
    console.print(table)


# ---------------------------------------------------------------------------
# delete-blog
# ---------------------------------------------------------------------------


@notebook_app.command("delete-blog")
def delete_blog_command(
    slug: Annotated[str, typer.Argument(help="Blog slug to un-publish.")],
    yes: Annotated[bool, YES_OPTION] = False,
) -> None:
    settings = load_settings()
    console = Console(highlight=False)

    async def _go() -> None:
        await delete_blog_op(settings.notebook, console, slug=slug, yes=yes)

    run_cli(_go(), console=console)


async def delete_blog_op(
    settings: NotebookSettings,
    console: Console,
    *,
    slug: str,
    yes: bool,
    http_client: httpx.AsyncClient | None = None,
) -> None:
    host_url, admin_secret = _require_host(settings, console)
    confirm_or_abort(console, f"delete blog {slug!r}?", yes=yes)

    async def _del(http: httpx.AsyncClient) -> None:
        await delete_blog_from_host(
            slug=slug, host_url=host_url, admin_secret=admin_secret, client=http
        )

    try:
        if http_client is not None:
            await _del(http_client)
        else:
            async with httpx.AsyncClient(timeout=30.0) as http:
                await _del(http)
    except NotebookHostError as err:
        console.print(f"[red]{err}[/red]")
        raise typer.Exit(code=5) from err
    console.print(f"[green]✓ deleted blog {slug!r}[/green]")
