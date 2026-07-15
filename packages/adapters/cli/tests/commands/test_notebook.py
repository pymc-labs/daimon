"""Tests for `daimon notebook` commands — file-based uploads via the admin API.

httpx.MockTransport per the testing skill; no real host. The command impls take
an injected http client seam (matching the skills.py sync pattern).
"""

from __future__ import annotations

import httpx
import pytest
import typer
from daimon.adapters.cli.commands.notebook import (
    attach_file,
    delete_blog_op,
    list_blogs_op,
    publish_blog_file,
)
from daimon.core.config import NotebookSettings
from pydantic import HttpUrl, SecretStr
from rich.console import Console

pytestmark = pytest.mark.asyncio


def _settings() -> NotebookSettings:
    return NotebookSettings(host_url=HttpUrl("http://nb:8001"), admin_secret=SecretStr("op-secret"))


async def test_publish_blog_file_puts_source_and_prints_url(tmp_path, capsys) -> None:
    src = tmp_path / "blog.py"
    src.write_text("import marimo as mo\napp = mo.App()\n")
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["auth"] = req.headers.get("authorization")
        seen["body"] = req.read()
        return httpx.Response(200, json={"slug": "my-blog", "url": "http://nb:8001/n/my-blog/"})

    console = Console()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await publish_blog_file(
            _settings(), console, slug="my-blog", file=str(src), http_client=client
        )
    assert seen["url"] == "http://nb:8001/admin/blogs/my-blog", (
        "PUTs the verbatim slug to the blog admin route"
    )
    assert seen["auth"] == "Bearer op-secret", "authorizes with the operator admin secret"
    assert b"mo.App()" in seen["body"], "the local file's source is sent"  # type: ignore[operator]
    assert "http://nb:8001/n/my-blog/" in capsys.readouterr().out, "prints the live blog URL"


async def test_attach_file_puts_raw_bytes(tmp_path, capsys) -> None:
    data = tmp_path / "posterior.nc"
    data.write_bytes(b"\x00\x01\x02\x03" * 1000)
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["body"] = req.read()
        return httpx.Response(
            200,
            json={
                "slug": "my-blog",
                "name": "posterior.nc",
                "size_bytes": 4000,
                "path": "data/posterior.nc",
            },
        )

    console = Console()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await attach_file(
            _settings(),
            console,
            slug="my-blog",
            name="posterior.nc",
            file=str(data),
            http_client=client,
        )
    assert seen["url"] == "http://nb:8001/admin/notebooks/my-blog/data/posterior.nc", (
        "PUTs to the data admin route"
    )
    assert seen["body"] == b"\x00\x01\x02\x03" * 1000, "raw file bytes sent unencoded"  # type: ignore[operator]
    assert "data/posterior.nc" in capsys.readouterr().out, "prints the read path"


async def test_publish_blog_file_errors_when_host_unset(tmp_path) -> None:
    src = tmp_path / "blog.py"
    src.write_text("x = 1\n")
    console = Console()
    with pytest.raises(typer.Exit):
        await publish_blog_file(
            NotebookSettings(host_url=None, admin_secret=None),
            console,
            slug="x",
            file=str(src),
            http_client=None,
        )


async def test_list_blogs_op_prints_blog_rows(capsys) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "blogs": [
                    {
                        "slug": "radar",
                        "url": "http://nb:8001/n/radar/",
                        "alive": True,
                        "created_at": "t0",
                    }
                ]
            },
        )

    console = Console()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await list_blogs_op(_settings(), console, as_json=False, http_client=client)
    assert "radar" in capsys.readouterr().out, "lists the blog slug in the table"


async def test_delete_blog_op_maps_host_error_to_exit(tmp_path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")  # non-404 non-2xx → NotebookHostError

    console = Console()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(typer.Exit):
            await delete_blog_op(_settings(), console, slug="x", yes=True, http_client=client)


async def test_delete_blog_op_success(capsys) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    console = Console()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await delete_blog_op(_settings(), console, slug="radar", yes=True, http_client=client)
    assert "deleted blog 'radar'" in capsys.readouterr().out, "prints the deletion confirmation"
