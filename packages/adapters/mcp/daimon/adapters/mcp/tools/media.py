"""Media MCP tools: YouTube transcript and file upload.

``fetch_youtube_transcript`` is Gemini-backed and registers only when
``settings.gemini.api_key`` is set and the server has built a ``genai.Client``.
``register_upload_tool`` has no such dependency. See ``server.py`` for wiring.

Agent-produced files cannot travel as a tool argument — the model would have to
emit the whole file as base64 — so ``create_file_upload_url`` mints a one-time
URL the sandbox PUTs to, and the bytes land in Postgres. ``send_message``
resolves them via the ``file_handles`` parameter on both platforms.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from daimon.adapters.mcp.auth.resolver import AuthIdentity
from daimon.adapters.mcp.runtime import McpRuntime
from daimon.adapters.mcp.services._usage import MediaUsage
from daimon.adapters.mcp.services.youtube import (
    _MODEL as YOUTUBE_MODEL,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.services.youtube import YouTubeService, YouTubeTranscriptError
from daimon.adapters.mcp.tools._ctx import (
    _auth,  # pyright: ignore[reportPrivateUsage]
    _check_admission,  # pyright: ignore[reportPrivateUsage]
)
from daimon.core.billing import BillingConfig
from daimon.core.media.filenames import display_filename_for
from daimon.core.media.youtube_url import extract_video_id
from daimon.core.pricing import MODEL_PRICING
from daimon.core.stores.file_uploads import MAX_UPLOAD_BYTES, create_upload
from daimon.core.usage_recording import record_media_usage
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from google import genai
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def _meter_billed_call(
    auth: AuthIdentity,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    markup: Decimal,
    model_id: str,
    usage: MediaUsage,
) -> None:
    """Record spend for a successful Gemini call on the billed path only.

    No-op on the trusted (``platform_user_id is None``) path. Runs
    outside any try/except: a metering DB failure IS a tool failure
    (guideline:architecture Error Propagation).
    """
    if auth.platform_user_id is None:
        return
    await record_media_usage(
        sessionmaker=sessionmaker,
        tenant_id=auth.tenant_id,
        platform_user_id=auth.platform_user_id,
        model_id=model_id,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_input_tokens=usage.cache_read_input_tokens,
        markup=markup,
        pricing=MODEL_PRICING.get(model_id),
    )


def register_media_tools(
    mcp: FastMCP,
    *,
    gemini_client: genai.Client,
    sessionmaker: async_sessionmaker[AsyncSession],
    billing_config: BillingConfig | None,
    markup: Decimal,
) -> None:
    """Register the Gemini-backed media tools on ``mcp``."""
    _register_youtube(
        mcp,
        youtube_service=YouTubeService(client=gemini_client),
        sessionmaker=sessionmaker,
        billing_config=billing_config,
        markup=markup,
    )


def _register_youtube(
    mcp: FastMCP,
    *,
    youtube_service: YouTubeService,
    sessionmaker: async_sessionmaker[AsyncSession],
    billing_config: BillingConfig | None,
    markup: Decimal,
) -> None:
    @mcp.tool
    async def fetch_youtube_transcript(  # pyright: ignore[reportUnusedFunction]
        ctx: Context, url: str
    ) -> str:
        """Fetch the transcript of a public YouTube video for summarization or Q&A.

        Use this when the user shares a YouTube URL and asks about the video's
        content — summaries, key points, quotes, "what did they say at X". Do
        NOT use it for unrelated chat about a video the user mentions without
        asking for content help.

        The returned transcript includes [HH:MM:SS] timestamps every ~30
        seconds and bracketed visual notes (e.g. "[shows chart]") for moments
        not captured by audio. Use timestamps when you cite specific moments
        in your reply ("around 12:30 they discuss...").

        Limitations:
        - Public videos only. Private and unlisted URLs will error out — tell
          the user the video must be public.
        - Free-tier quota is 8 hours of YouTube video per day across the whole
          deployment. If you see a quota error, tell the user honestly.
        - Returns English transcripts. For non-English content, the speakers'
          original words are preserved but section labels are English.

        Args:
            url: A public YouTube video URL. Accepts youtube.com/watch?v=,
                youtu.be/, /embed/, /shorts/, /live/ shapes.

        Returns:
            The full transcript text with timestamps.
        """
        auth = await _check_admission(
            ctx,
            sessionmaker=sessionmaker,
            billing_config=billing_config,
            tool_name="fetch_youtube_transcript",
        )

        if extract_video_id(url) is None:
            raise ToolError(
                f"TERMINAL ERROR: {url!r} is not a recognised YouTube URL. "
                f"Accepted shapes: youtube.com/watch?v=ID, youtu.be/ID, /embed/ID, "
                f"/shorts/ID, /live/ID. Do not retry — ask the user for a YouTube link."
            )

        try:
            result = await youtube_service.extract_transcript(url)
        except YouTubeTranscriptError as exc:
            raise ToolError(str(exc)) from exc

        await _meter_billed_call(
            auth,
            sessionmaker=sessionmaker,
            markup=markup,
            model_id=YOUTUBE_MODEL,
            usage=result.usage,
        )
        return result.text


def register_upload_tool(mcp: FastMCP, *, runtime: McpRuntime) -> None:
    """Register ``create_file_upload_url``, independent of any Gemini configuration.

    Unlike the three tools above, this needs no paid external call — no
    admission/billing gate, just the ordinary auth check every tool has.
    """

    @mcp.tool
    async def create_file_upload_url(  # pyright: ignore[reportUnusedFunction]
        ctx: Context,
        title: str,
        mime_type: str,
    ) -> str:
        """Attach, post, send, or share a file in the chat — step 1 of 2.

        This is the ONLY way to put a file you produced (a chart, a table, an
        image, a data file) into Discord or Slack as an attachment. Your reply text
        delivers itself; a file never does. Reading a file, or copying it to
        /mnt/session/outputs/, does not send it anywhere.

        The intent words are in this first line on purpose: an agent looking
        for "attach an image" or "post a file" has to find this tool by
        search, and the name alone does not carry any of them.

        Call this FIRST, then send the bytes from your sandbox with curl:

            curl -sS -X PUT --data-binary @chart.png "<upload_url>"

        Do NOT base64 the file into a tool argument. The bytes go straight from
        your sandbox over HTTP; they never pass through your context, so a large
        file costs you nothing and cannot be truncated in transit.

        ``mime_type`` is required (e.g. "image/png", "text/csv") — it is not
        guessed, since a wrong one produces a wrong file extension.

        Returns an ``upload_url`` and a ``handle_id``. After the PUT succeeds,
        pass the handle id to ``send_message``'s ``file_handles`` argument to
        post it, in the same channel/thread where the user asked — do not post
        to a different channel unless the user explicitly names one. The URL is
        single-use and expires; mint a new one per file.
        """
        auth = await _auth(ctx)

        # app_root_url, not public_url: the PUT route is add_route'd at the app
        # root beside /healthz, while public_url carries the /mcp streamable
        # suffix. Minting from public_url yields /mcp/uploads/<token>, which 404s.
        app_root = runtime.settings.mcp.app_root_url
        if app_root is None:
            raise ToolError(
                "TERMINAL ERROR: file upload is unavailable — this deployment has no "
                "configured public URL, so an upload URL cannot be minted."
            )

        async with runtime.session_factory() as session:
            row, upload_token = await create_upload(
                session,
                tenant_id=auth.tenant_id,
                title=title,
                display_filename=display_filename_for(title, mime_type),
                content_type=mime_type,
                now=datetime.now(UTC),
            )
            await session.commit()

        upload_url = f"{app_root}/uploads/{upload_token}"
        return (
            f"Upload {row.display_filename!r} with:\n"
            f'  curl -sS -X PUT --data-binary @<your-file> "{upload_url}"\n'
            f"Then post it by passing handle id {row.id!r} to `send_message`'s "
            f"`file_handles` argument. Use the same channel/thread where the user "
            f"asked — do not post to a different channel. Max "
            f"{MAX_UPLOAD_BYTES // 1_000_000} MB; the URL is single-use."
        )
