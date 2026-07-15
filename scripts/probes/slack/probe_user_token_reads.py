"""Live Slack API probe: xoxp read semantics + ephemeral-in-thread.

Run manually against the staging workspace (never CI):

    SLACK_USER_TOKEN=xoxp-... SLACK_BOT_TOKEN=xoxb-... \
    PROBE_PUBLIC_UNJOINED_CHANNEL=C... PROBE_THREAD=C...:171717.1 PROBE_USER=U... \
    uv run python scripts/probes/slack/probe_user_token_reads.py

Obtain a user token by installing the app with user scopes from
api.slack.com → your app → OAuth & Permissions (your own xoxp appears there).
"""

from __future__ import annotations

import asyncio
import os
import sys

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient


async def main() -> int:
    user = AsyncWebClient(token=os.environ["SLACK_USER_TOKEN"])
    bot = AsyncWebClient(token=os.environ["SLACK_BOT_TOKEN"])
    public_unjoined = os.environ["PROBE_PUBLIC_UNJOINED_CHANNEL"]
    thread_channel, _, thread_ts = os.environ["PROBE_THREAD"].partition(":")
    probe_user = os.environ["PROBE_USER"]
    failures: list[str] = []

    # Probe 1: user-token history on a public channel the user never joined.
    try:
        await user.conversations_history(channel=public_unjoined, limit=1)  # pyright: ignore[reportUnknownMemberType]
        print(
            "probe1: user-token history on unjoined public channel SUCCEEDED "
            "(fallback in _read.py will simply never fire)"
        )
    except SlackApiError as err:
        code = str(err.response.get("error", ""))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        print(f"probe1: user-token history failed with error={code}")
        if code != "not_in_channel":
            failures.append(f"probe1: expected not_in_channel, got {code}")

    # Probe 2: ephemeral into a thread.
    try:
        await bot.chat_postEphemeral(  # pyright: ignore[reportUnknownMemberType]
            channel=thread_channel,
            user=probe_user,
            thread_ts=thread_ts,
            text="daimon probe: ephemeral-in-thread — visible inside the thread?",
        )
        print(
            "probe2: chat.postEphemeral(thread_ts=...) accepted — "
            "verify visually it rendered INSIDE the thread"
        )
    except SlackApiError as err:
        failures.append(f"probe2: postEphemeral failed: {err.response.get('error')}")  # pyright: ignore[reportUnknownMemberType]

    for f in failures:
        print(f"FAIL: {f}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
