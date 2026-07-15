"""Notebook MCP tools. Mint capability-upload URLs; thin wrappers around daimon.core.notebooks."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.core.notebooks.attach import InvalidAttachmentError
from daimon.core.notebooks.host_client import NotebookHostError
from daimon.core.notebooks.publish import (
    HostNotConfiguredError,
    InvalidSlugError,
    NotebookRateLimitError,
    delete_blog,
    list_blogs,
)
from daimon.core.notebooks.upload import (
    create_attachment_upload,
    create_blog_upload,
    create_notebook_upload,
)
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError


async def _create_blog_upload_impl(
    runtime: McpRuntime, *, slug: str, principal_key: str
) -> dict[str, str]:
    try:
        return create_blog_upload(
            slug=slug,
            notebook_settings=runtime.settings.notebook,
            principal_key=principal_key,
            now=datetime.now(UTC),
            rate_limiter=runtime.notebook_rate_limiter,
        )
    except HostNotConfiguredError as err:
        raise ToolError("notebook host not configured") from err
    except (InvalidSlugError, NotebookRateLimitError) as err:
        raise ToolError(str(err)) from err


async def _create_notebook_upload_impl(
    runtime: McpRuntime, *, slug: str | None, principal_key: str
) -> dict[str, str]:
    try:
        return create_notebook_upload(
            slug=slug,
            notebook_settings=runtime.settings.notebook,
            principal_key=principal_key,
            now=datetime.now(UTC),
            rate_limiter=runtime.notebook_rate_limiter,
        )
    except HostNotConfiguredError as err:
        raise ToolError("notebook host not configured") from err
    except (InvalidSlugError, NotebookRateLimitError) as err:
        raise ToolError(str(err)) from err


async def _create_attachment_upload_impl(
    runtime: McpRuntime, *, slug: str, name: str, principal_key: str
) -> dict[str, str]:
    try:
        return create_attachment_upload(
            slug=slug,
            name=name,
            notebook_settings=runtime.settings.notebook,
            principal_key=principal_key,
            now=datetime.now(UTC),
            rate_limiter=runtime.notebook_rate_limiter,
        )
    except HostNotConfiguredError as err:
        raise ToolError("notebook host not configured") from err
    except (InvalidAttachmentError, NotebookRateLimitError) as err:
        raise ToolError(str(err)) from err


async def _delete_blog_impl(
    runtime: McpRuntime,
    *,
    principal_key: str,
    slug: str,
    client_factory: type[httpx.AsyncClient] = httpx.AsyncClient,
) -> dict[str, str]:
    try:
        async with client_factory() as client:
            await delete_blog(
                slug=slug,
                notebook_settings=runtime.settings.notebook,
                client=client,
                principal_key=principal_key,
            )
    except HostNotConfiguredError as err:
        raise ToolError("notebook host not configured") from err
    except NotebookHostError as err:
        raise ToolError(str(err)) from err
    except httpx.TimeoutException as err:
        raise ToolError("notebook host timed out") from err
    except httpx.TransportError as err:
        raise ToolError(f"notebook host unreachable: {err}") from err
    return {"slug": slug, "status": "deleted"}


async def _list_blogs_impl(
    runtime: McpRuntime,
    *,
    principal_key: str,
    client_factory: type[httpx.AsyncClient] = httpx.AsyncClient,
) -> dict[str, object]:
    try:
        async with client_factory() as client:
            blogs = await list_blogs(
                notebook_settings=runtime.settings.notebook,
                client=client,
                principal_key=principal_key,
            )
    except HostNotConfiguredError as err:
        raise ToolError("notebook host not configured") from err
    except NotebookHostError as err:
        raise ToolError(str(err)) from err
    except httpx.TimeoutException as err:
        raise ToolError("notebook host timed out") from err
    except httpx.TransportError as err:
        raise ToolError(f"notebook host unreachable: {err}") from err
    result: dict[str, object] = {"blogs": blogs}
    return result


def register_notebook_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def create_notebook_upload_url(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, slug: str | None = None
    ) -> dict[str, str]:
        """Mint a one-time upload URL for an ephemeral marimo notebook.

        Get your notebook's .py into a sandbox file (author it incrementally with
        write/edit + read-back, or curl it from an origin), then PUT the file to the
        returned upload_url — the source never goes through a tool argument (which
        truncates large notebooks).

        Returns {upload_url, slug, upload_expires_at}. upload_expires_at is when the
        URL stops working (~5 min); use it promptly. Pass slug to reuse a stable URL
        across iterations, or omit it for a fresh random one.

        Then upload with: curl -sS -X PUT --data-binary @<file> "<upload_url>". The
        curl response JSON carries the live url (and expires_at for notebooks); share
        that. Never paste large file contents into a tool argument.
        """
        auth = await _auth(ctx)
        return await _create_notebook_upload_impl(
            runtime, slug=slug, principal_key=str(auth.account_id)
        )

    @mcp.tool
    async def create_blog_upload_url(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, slug: str
    ) -> dict[str, str]:
        """Mint a one-time upload URL for a PERMANENT, shareable blog (run mode).

        Get the blog's .py into a sandbox file, then PUT it to upload_url. slug is
        required and becomes part of the permanent public URL — choose a meaningful,
        stable name. The host validates that the cells execute before serving; the
        curl response carries the live url on success or a 422 with the failing cells.

        Returns {upload_url, slug, upload_expires_at}. Upload with:
        curl -sS -X PUT --data-binary @<file> "<upload_url>". Never paste large file
        contents into a tool argument.
        """
        auth = await _auth(ctx)
        return await _create_blog_upload_impl(
            runtime, slug=slug, principal_key=str(auth.account_id)
        )

    @mcp.tool
    async def create_attachment_upload_url(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, slug: str, name: str
    ) -> dict[str, str]:
        """Mint a one-time upload URL for a raw data file in a notebook/blog workspace.

        Produce the bytes in your sandbox (e.g. idata.to_netcdf("posterior.nc")) then
        PUT the file to upload_url with curl -sS -X PUT --data-binary @posterior.nc
        "<upload_url>". This is the ONLY way to attach large data — base64 through a
        tool argument cannot carry ~1 MB.

        name is the agent-visible filename (charset [A-Za-z0-9_][A-Za-z0-9_.-]{0,63},
        no slash); it becomes data/<name> inside the notebook. slug must match the slug
        you publish the notebook/blog under. Returns {upload_url, slug, upload_expires_at}.
        """
        auth = await _auth(ctx)
        return await _create_attachment_upload_impl(
            runtime, slug=slug, name=name, principal_key=str(auth.account_id)
        )

    @mcp.tool
    async def delete_blog(ctx: Context, slug: str) -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        """Un-publish a blog you previously published (frees its host port).

        slug is the same slug you passed to create_blog_upload_url.
        """
        auth = await _auth(ctx)
        return await _delete_blog_impl(runtime, principal_key=str(auth.account_id), slug=slug)

    @mcp.tool
    async def list_blogs(ctx: Context) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        """List the blogs you've published (slug, url, alive). Use to review or
        clean up — pass a slug to delete_blog to reclaim a host port."""
        auth = await _auth(ctx)
        return await _list_blogs_impl(runtime, principal_key=str(auth.account_id))
