"""Report publishing MCP tools. Thin wrappers around daimon.core.reports.publish.

Registered with the plain tool decorator, no visibility tag of any kind.
This is the security control the whole module exists to carry: a report's
own token is agent-scoped (it carries an ``agent_id`` claim), and
``IdentityMiddleware`` narrows any agent-scoped session down to exactly one
tagged group of tools reserved for chat turns — neither tool here is a
member of that group. Tagging either tool with it would hand every report
reader the ability to publish or delete reports in the publishing tenant.
Untagged keeps both on the default operator surface, reachable by any
ordinary platform-user token in its own tenant, and invisible to a report's
own token.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal

import httpx
from anthropic.types.beta import BetaManagedAgentsAgent
from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.tools._channel_policy import (
    require_publishable,
    require_reader_source_publishable,
)
from daimon.adapters.mcp.tools._ctx import _auth  # pyright: ignore[reportPrivateUsage]
from daimon.adapters.mcp.tools._pin_guard import require_pin_write_access
from daimon.adapters.mcp.tools.setup_target import require_turn_origin
from daimon.core.errors import DaimonError
from daimon.core.reports.host_client import Recipient, ReportHostError
from daimon.core.reports.publish import (
    HostNotConfiguredError,
    InvalidSlugError,
    delete_report,
    publish_report,
)
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError


def _report_host_error_message(err: ReportHostError) -> str:
    """Recover the timeout/unreachable distinction from the wrapped cause.

    ``daimon.core.reports.host_client``'s ``_send`` wraps every
    ``httpx.HTTPError`` — timeouts and transport failures included — into a
    single ``ReportHostError`` via ``raise ReportHostError(...) from exc``,
    so a raw ``httpx.TimeoutException``/``TransportError`` never reaches this
    module directly. The chained ``__cause__`` still carries the original
    exception, which is what lets a timed-out host and one that returned a
    non-2xx status read differently to the person in the chat thread, the
    same distinction the notebook tools draw at their own boundary.
    """
    cause = err.__cause__
    if isinstance(cause, httpx.TimeoutException):
        return "report host timed out"
    if isinstance(cause, httpx.TransportError):
        return f"report host unreachable: {cause}"
    return str(err)


async def _source_authorizer(
    runtime: McpRuntime, auth: AuthIdentity, origin_context_id: str | None
) -> Callable[[BetaManagedAgentsAgent], Awaitable[None]]:
    """The rules for the agent whose reader variant is published.

    A reader answers as its source agent, so publishing one is a change to
    what that agent reaches: never a channel's own agent; one with a rule by
    an admin, or a member only from a turn inside the source's channels
    (``origin_context_id``).
    """
    origin = (
        await require_turn_origin(runtime, auth, origin_context_id) if origin_context_id else None
    )

    async def authorize_source(source: BetaManagedAgentsAgent) -> None:
        await require_reader_source_publishable(runtime, auth, source)
        await require_pin_write_access(runtime, auth, ma_agent=source, origin=origin)

    return authorize_source


async def _publish_report_impl(
    runtime: McpRuntime,
    *,
    tenant_id: uuid.UUID,
    account_id: uuid.UUID,
    slug: str,
    title: str,
    recipients: list[Recipient],
    cap_usd: float | str,
    agent: str | None,
    client_factory: type[httpx.AsyncClient] = httpx.AsyncClient,
    auth: AuthIdentity | None = None,
    origin_context_id: str | None = None,
) -> dict[str, object]:
    # FastMCP has no Decimal-shaped tool argument, so cap_usd arrives as a
    # float or a string; a string round trip is what keeps a float's binary
    # imprecision from ever reaching the core function as a signed cap.
    cap = Decimal(str(cap_usd))
    if runtime.settings.mcp.jwt_secret is None:
        raise ToolError("report host not configured: DAIMON_MCP__JWT_SECRET is unset")
    jwt_secret = runtime.settings.mcp.jwt_secret.get_secret_value().encode()
    if auth is not None:
        await require_publishable(
            runtime, auth, tool_name="publish_report", origin_context_id=origin_context_id
        )
    authorize_source = (
        await _source_authorizer(runtime, auth, origin_context_id) if auth is not None else None
    )
    try:
        async with client_factory() as client:
            result = await publish_report(
                anthropic=runtime.client,
                session_factory=runtime.session_factory,
                http_client=client,
                report_host_settings=runtime.settings.report_host,
                jwt_secret=jwt_secret,
                max_bundle_bytes=runtime.settings.mcp.bundle_max_bytes,
                default=runtime.deployment_default,
                tenant_id=tenant_id,
                account_id=account_id,
                slug=slug,
                title=title,
                recipients=recipients,
                cap_usd=cap,
                agent=agent,
                now=datetime.now(UTC),
                authorize_source=authorize_source,
            )
    except HostNotConfiguredError as err:
        raise ToolError("report host not configured") from err
    except InvalidSlugError as err:
        raise ToolError(str(err)) from err
    except ReportHostError as err:
        raise ToolError(_report_host_error_message(err)) from err
    except DaimonError as err:
        raise ToolError(str(err)) from err
    return {"upload_url": result.upload_url, "links": result.links}


async def _delete_report_impl(
    runtime: McpRuntime,
    *,
    tenant_id: uuid.UUID,
    slug: str,
    client_factory: type[httpx.AsyncClient] = httpx.AsyncClient,
) -> dict[str, object]:
    try:
        async with client_factory() as client:
            result = await delete_report(
                session_factory=runtime.session_factory,
                http_client=client,
                report_host_settings=runtime.settings.report_host,
                tenant_id=tenant_id,
                slug=slug,
                now=datetime.now(UTC),
            )
    except HostNotConfiguredError as err:
        raise ToolError("report host not configured") from err
    except ReportHostError as err:
        raise ToolError(_report_host_error_message(err)) from err
    return {"tokens_revoked": result.tokens_revoked, "host_removed": result.host_removed}


def register_publish_tools(mcp: FastMCP, runtime: McpRuntime) -> None:
    @mcp.tool
    async def publish_report(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        slug: str,
        title: str,
        recipients: list[Recipient],
        cap_usd: float | str,
        agent: str | None = None,
        origin_context_id: str | None = None,
    ) -> dict[str, object]:
        """Publish a report: a page where the named people can read it and
        ask it questions.

        Pass a short slug (goes in the URL), a title, one entry per
        recipient (``name`` and ``label`` — a short identifier for their
        link, e.g. an email or handle), a dollar cap on how much reader Q&A
        may spend against your tenant's balance, and optionally which agent
        should answer questions (defaults to this tenant's configured
        agent).

        Pass this turn's ``origin_context_id``. An agent with a rule (where it
        runs) publishes only once the requester approves the call on a card: a
        link reaches whoever holds it. A channel's own agent never answers a
        report. When the answering agent has a rule, a non-admin may only
        publish its reader from inside its channels.

        Returns ``{upload_url, links}``: ``upload_url`` is a one-time
        capability URL: ``links`` maps each recipient's name to their own
        personal link.

        What to do next: build ONE gzip archive (``tar -czf bundle.tar.gz
        report.pdf analysis/``) with the report PDF at its root and the
        analysis files beside it, using relative paths only — no absolute
        paths, no leading slash — then PUT it to ``upload_url``:
        ``curl -sS -X PUT --data-binary @bundle.tar.gz "<upload_url>"``.
        Keep the archive under the size cap; if it's too large, raw model
        output files (posterior draws, large .nc/.csv dumps) are the first
        thing to drop — never the PDF or the code that produced it. Relay
        each recipient their own link; the links are not interchangeable.
        """
        auth = await _auth(ctx)
        return await _publish_report_impl(
            runtime,
            tenant_id=auth.tenant_id,
            account_id=auth.account_id,
            slug=slug,
            title=title,
            recipients=recipients,
            cap_usd=cap_usd,
            agent=agent,
            auth=auth,
            origin_context_id=origin_context_id,
        )

    @mcp.tool
    async def delete_report(ctx: Context, slug: str) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        """Un-publish a report you published.

        Revokes the report's credential and removes it from the host —
        every link you handed out stops working. This cannot be undone;
        re-publishing the same slug mints a new credential and new links,
        so anyone who needs access again must be sent their new link.
        """
        auth = await _auth(ctx)
        return await _delete_report_impl(runtime, tenant_id=auth.tenant_id, slug=slug)
