"""Notebook MCP tools. Mint capability-upload URLs; thin wrappers around daimon.core.notebooks."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import require_publishable
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.core.notebooks.attach import InvalidAttachmentError
from daimon.core.notebooks.host_client import NotebookHostError
from daimon.core.notebooks.publish import (
    HostNotConfiguredError,
    InvalidSlugError,
    NotebookRateLimitError,
    delete_notebook,
    list_notebooks,
)
from daimon.core.notebooks.upload import (
    create_attachment_upload,
    create_notebook_upload,
)
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError


async def _create_notebook_upload_impl(
    runtime: McpRuntime,
    *,
    slug: str | None,
    permanent: bool,
    principal_key: str,
    editable: bool = False,
    ttl_days: int | None = None,
    tenant: str | None = None,
) -> dict[str, str]:
    if permanent and editable:
        raise ToolError("editable=True is for scratch notebooks only; a blog is always read-only")
    try:
        return create_notebook_upload(
            slug=slug,
            permanent=permanent,
            editable=editable,
            ttl_days=ttl_days,
            notebook_settings=runtime.settings.notebook,
            principal_key=principal_key,
            now=datetime.now(UTC),
            rate_limiter=runtime.notebook_rate_limiter,
            tenant=tenant,
        )
    except HostNotConfiguredError as err:
        raise ToolError("notebook host not configured") from err
    except (InvalidSlugError, NotebookRateLimitError) as err:
        raise ToolError(str(err)) from err
    except ValueError as err:  # editable refused by the operator setting, or a bad ttl_days
        raise ToolError(str(err)) from err


async def _create_attachment_upload_impl(
    runtime: McpRuntime,
    *,
    slug: str,
    name: str,
    principal_key: str,
    tenant: str | None = None,
) -> dict[str, str]:
    try:
        return create_attachment_upload(
            slug=slug,
            name=name,
            notebook_settings=runtime.settings.notebook,
            principal_key=principal_key,
            now=datetime.now(UTC),
            rate_limiter=runtime.notebook_rate_limiter,
            tenant=tenant,
        )
    except HostNotConfiguredError as err:
        raise ToolError("notebook host not configured") from err
    except (InvalidAttachmentError, NotebookRateLimitError) as err:
        raise ToolError(str(err)) from err


async def _delete_notebook_impl(
    runtime: McpRuntime,
    *,
    principal_key: str,
    slug: str,
    client_factory: type[httpx.AsyncClient] = httpx.AsyncClient,
) -> dict[str, object]:
    """Delete and report what actually happened, not a fixed 'deleted'.

    An unconditional success string makes a wrong slug indistinguishable from a
    real removal, which is how a delete that never deleted anything went
    unnoticed in production.
    """
    try:
        async with client_factory() as client:
            deleted = await delete_notebook(
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
    return {"slug": slug, "deleted": deleted}


async def _list_notebooks_impl(
    runtime: McpRuntime,
    *,
    principal_key: str,
    client_factory: type[httpx.AsyncClient] = httpx.AsyncClient,
) -> dict[str, object]:
    try:
        async with client_factory() as client:
            notebooks = await list_notebooks(
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
    result: dict[str, object] = {"notebooks": notebooks}
    return result


def register_notebook_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def create_notebook_upload_url(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        slug: str | None = None,
        permanent: bool = False,
        editable: bool = False,
        ttl_days: int | None = None,
        origin_context_id: str | None = None,
    ) -> dict[str, str]:
        """Mint a one-time upload URL for a marimo notebook.

        Get your notebook's .py into a sandbox file (author it incrementally with
        write/edit + read-back, or curl it from an origin), then PUT the file to the
        returned upload_url — the source never goes through a tool argument (which
        truncates large notebooks).

        permanent=False (default) publishes a scratch notebook that the host deletes
        after ttl_days: 1 by default, up to 365. Pick a longer ttl_days when the
        person needs the link for longer, such as a client review or a report that
        links to it. permanent=True publishes it as a blog, kept until you delete it.
        Prefer the default; re-upload the same slug with permanent=True once the
        notebook is worth keeping. Re-uploading a scratch notebook restarts its
        ttl_days from now; re-uploading a blog keeps it a blog.

        Either survives host restarts. The host stops a notebook nobody has opened
        for a couple of hours, so opening a quiet link can take a few seconds while
        it starts again.

        Both are read-only apps by default: code hidden, interactive widgets live.
        editable=True (scratch only, and only where the operator has turned it on)
        serves the marimo code editor instead. Anyone holding that link can run
        arbitrary code on the notebook host, so pass it only when the person you
        are sharing with asked to edit the notebook.

        Returns {upload_url, slug, upload_expires_at}. upload_expires_at is when the
        upload URL stops working (~5 min), not the notebook; use it promptly. Pass
        slug to reuse a stable URL across iterations, or omit it for a fresh random
        one.

        Then upload with: curl -sS -X PUT --data-binary @<file> "<upload_url>". The
        curl response JSON carries the live url and expires_at (null for a blog);
        share that whole url and say when it expires. It is a notebook-scoped share
        link; share it only where the notebook belongs. Never post legacy URLs
        with access_token query strings; re-publish the same slug for a share link. Never
        paste large file contents into a tool argument.

        Pass this turn's origin_context_id. An agent with a rule (where it runs)
        publishes only once the requester approves the call on a card.
        """
        auth = await _auth(ctx)
        await require_publishable(
            runtime,
            auth,
            tool_name="create_notebook_upload_url",
            origin_context_id=origin_context_id,
        )
        return await _create_notebook_upload_impl(
            runtime,
            slug=slug,
            permanent=permanent,
            editable=editable,
            ttl_days=ttl_days,
            principal_key=str(auth.account_id),
            tenant=str(auth.tenant_id),
        )

    @mcp.tool
    async def create_attachment_upload_url(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, slug: str, name: str, origin_context_id: str | None = None
    ) -> dict[str, str]:
        """Mint a one-time upload URL for a raw data file in a notebook/blog workspace.

        Produce the bytes in your sandbox (e.g. idata.to_netcdf("posterior.nc")) then
        PUT the file to upload_url with curl -sS -X PUT --data-binary @posterior.nc
        "<upload_url>". This is the ONLY way to attach large data — base64 through a
        tool argument cannot carry ~1 MB.

        name is the agent-visible filename (charset [A-Za-z0-9_][A-Za-z0-9_.-]{0,63},
        no slash); it becomes data/<name> inside the notebook. slug must match the slug
        you publish the notebook/blog under. Returns {upload_url, slug, upload_expires_at}.
        Pass this turn's origin_context_id, as for create_notebook_upload_url.
        """
        auth = await _auth(ctx)
        await require_publishable(
            runtime,
            auth,
            tool_name="create_attachment_upload_url",
            origin_context_id=origin_context_id,
        )
        return await _create_attachment_upload_impl(
            runtime,
            slug=slug,
            name=name,
            principal_key=str(auth.account_id),
            tenant=str(auth.tenant_id),
        )

    @mcp.tool
    async def delete_notebook(ctx: Context, slug: str) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        """Un-publish a notebook or blog you published (deletes it from the host).

        slug is the bare name — the same one you passed to
        create_notebook_upload_url, and the same one list_notebooks reports.

        Returns {slug, deleted}. deleted=false means nothing by that name existed:
        check list_notebooks for the right slug rather than retrying.
        """
        auth = await _auth(ctx)
        return await _delete_notebook_impl(runtime, principal_key=str(auth.account_id), slug=slug)

    @mcp.tool
    async def list_notebooks(ctx: Context) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        """List what you've published — scratch notebooks and permanent blogs alike.

        Each entry carries slug, url, alive, permanent (true = a blog, kept until
        deleted) and, for a scratch notebook, expires_at. url is null while a
        notebook is stopped; its published link still works. The slug
        is the bare name: pass it straight to delete_notebook to reclaim a host
        port, or to create_notebook_upload_url to re-upload over it.
        """
        auth = await _auth(ctx)
        return await _list_notebooks_impl(runtime, principal_key=str(auth.account_id))
