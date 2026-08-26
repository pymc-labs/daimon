"""Discord-action MCP tools: read_thread, read_channel, list_channels,
list_threads, search_messages, parse_link, get_message, send_message,
create_thread.

Mirrors ``tools/routines.py`` shape: each ``@mcp.tool`` closure delegates to a
module-private ``_*_impl`` function that takes ``(runtime, auth, **kwargs)``.

Permission model: every tool resolves the caller's Discord ``Member`` against
the JWT-bound guild and runs ``permissions_for(member)`` channel-side. Cross-guild
and DM channels are rejected before any I/O. Thread visibility uses parent-channel
ACL plus explicit private-thread membership checks.

Token resolution: ``runtime.settings.discord.bot_token`` is required at boot
(validated by ``_validate_settings`` in Plan 24-04). The REST client is
constructed per-call via the ``rest_client(token)`` async context manager.
"""

from __future__ import annotations

from daimon.adapters.mcp.tools.discord._app_install_button import (
    _post_app_install_button_impl as _post_app_install_button_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._client import (
    _require_discord_identity as _require_discord_identity,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._client import (
    _require_guild_id as _require_guild_id,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._client import (
    rest_client as rest_client,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._credential_button import (
    _post_credential_button_impl as _post_credential_button_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._models import (
    AttachmentRow as AttachmentRow,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._models import (
    ChannelRow as ChannelRow,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._models import (
    MessageRow as MessageRow,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._models import (
    ParsedLink as ParsedLink,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._models import (
    ReadChannelResult as ReadChannelResult,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._models import (
    ReadThreadResult as ReadThreadResult,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._models import (
    SearchResult as SearchResult,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._models import (
    ThreadRow as ThreadRow,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._read import (
    _get_message_impl as _get_message_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._read import (
    _list_channels_impl as _list_channels_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._read import (
    _list_threads_impl as _list_threads_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._read import (
    _parse_link_impl as _parse_link_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._read import (
    _read_channel_impl as _read_channel_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._read import (
    _read_thread_impl as _read_thread_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._search import (
    _search_messages_impl as _search_messages_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._send import (
    _build_files as _build_files,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._send import (
    _build_files_from_handles as _build_files_from_handles,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._send import (
    _fetch_attachment as _fetch_attachment,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._send import (
    _send_message_impl as _send_message_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._threads import (
    _create_thread_impl as _create_thread_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._wizard import (
    PostedWizard as PostedWizard,
)
from daimon.adapters.mcp.tools.discord._wizard import (
    _post_wizard_impl as _post_wizard_impl,  # pyright: ignore[reportPrivateUsage]
)
from daimon.adapters.mcp.tools.discord._wizard import (
    build_wizard_view as build_wizard_view,
)
