"""Probe A1 (D-05): does channels_select fire block_actions over Socket Mode?

Background
----------
The ``channels_select`` element is a **native** Block Kit element — Slack populates
the channel list from the workspace itself, unlike ``external_select`` which calls an
options-load URL.  The D-04 concern (does ``block_suggestion`` route over the
WebSocket?) therefore does not apply.  Native selects should fire a standard
``block_actions`` payload on selection.

D-05 confidence is MEDIUM because the project has no live evidence on file for this
specific element.  This probe records whether:

1. Selecting a channel in a ``channels_select`` element fires ``req.type == "interactive"``
   over the Socket Mode WebSocket.
2. The payload type is ``block_actions``.
3. The selected value is keyed as ``selected_channel`` inside ``actions[0]``.

If all three hold → PASS and Phase 83 can use ``channels_select`` directly.
If any fail → FAIL and the UI-SPEC ``static_select``-from-``conversations_list`` fallback
applies (already specified in 83-UI-SPEC.md, Propagation Scope Picker).

Setup
-----
1. Install a Slack app on a test workspace with Socket Mode enabled (``xapp-`` app token).
2. Export the app token and a bot token (``xoxb-``):
     export DAIMON_SLACK__APP_TOKEN=xapp-...
     export DAIMON_SLACK__BOT_TOKEN=xoxb-...
3. Optionally export an initial channel id to pre-select:
     export PROBE_INITIAL_CHANNEL=C01234567

Run
---
    uv run python scripts/probes/slack/channels_select_socket_mode_probe.py

The probe:
  1. Connects a Socket Mode client.
  2. Posts an ephemeral message to #general (or the channel in PROBE_TARGET_CHANNEL)
     containing a ``channels_select`` element (action_id ``agent_setup__scope:channel``).
  3. Logs every inbound SocketModeRequest.
  4. On a matching block_actions event from ``channels_select``, prints PASS/FAIL and exits.

Press Ctrl-C to exit without a result (recorded as UNKNOWN).

CI guard
--------
The probe exits cleanly when DAIMON_SLACK__APP_TOKEN is unset — it is never
executed in CI (no live Slack credentials in CI).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Env gate — exit cleanly if the app token is absent (CI safety)
# ---------------------------------------------------------------------------

APP_TOKEN = os.environ.get("DAIMON_SLACK__APP_TOKEN", "")
if not APP_TOKEN:
    print(
        "DAIMON_SLACK__APP_TOKEN is not set.\n"
        "Export your Slack xapp- app-level token before running this probe:\n"
        "  export DAIMON_SLACK__APP_TOKEN=xapp-...\n"
        "Probe exits without connecting."
    )
    sys.exit(0)

BOT_TOKEN = os.environ.get("DAIMON_SLACK__BOT_TOKEN", "")
INITIAL_CHANNEL = os.environ.get("PROBE_INITIAL_CHANNEL", "")
TARGET_CHANNEL = os.environ.get("PROBE_TARGET_CHANNEL", "")

# ---------------------------------------------------------------------------
# Imports — slack-sdk is a runtime dep of the slack adapter; no new installs.
# ---------------------------------------------------------------------------

from slack_sdk.socket_mode.aiohttp import SocketModeClient  # noqa: E402
from slack_sdk.socket_mode.request import SocketModeRequest  # noqa: E402
from slack_sdk.socket_mode.response import SocketModeResponse  # noqa: E402
from slack_sdk.web.async_client import AsyncWebClient  # noqa: E402

_ACTION_ID = "agent_setup__scope:channel"
_BLOCK_ID = "agent_setup__scope_picker"


def _ts() -> str:
    return datetime.now(tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]


def _build_channels_select_block() -> list[dict[str, object]]:
    """Build a Block Kit section with a channels_select element."""
    element: dict[str, object] = {
        "type": "channels_select",
        "action_id": _ACTION_ID,
        "placeholder": {
            "type": "plain_text",
            "text": "Pick a channel",
        },
    }
    if INITIAL_CHANNEL:
        element["initial_channel"] = INITIAL_CHANNEL

    return [
        {
            "type": "section",
            "block_id": _BLOCK_ID,
            "text": {
                "type": "mrkdwn",
                "text": (
                    "*Probe A1 (D-05):* Select a channel below to confirm "
                    "`channels_select` fires `block_actions` over Socket Mode "
                    "with a `selected_channel` field."
                ),
            },
            "accessory": element,
        }
    ]


async def _post_probe_message(web_client: AsyncWebClient, channel: str) -> None:
    """Post the probe message to the target channel."""
    resp = await web_client.chat_postMessage(
        channel=channel,
        text="Probe A1: channels_select Socket Mode routing test",
        blocks=_build_channels_select_block(),
    )
    if not resp["ok"]:
        print(f"WARNING: chat.postMessage failed: {resp.get('error', 'unknown')}")
    else:
        print(f"  Posted probe message to channel {channel!r}")


async def _run_probe() -> None:
    print("channels_select Socket Mode probe (A1, D-05)")
    print("=" * 60)
    print("Purpose: confirm channels_select fires block_actions over")
    print("         Socket Mode with the 'selected_channel' field (D-05 / RESEARCH A1).")
    print("=" * 60)
    print()
    print("Connecting to Slack via Socket Mode …")

    web_client: AsyncWebClient | None = None
    if BOT_TOKEN:
        web_client = AsyncWebClient(token=BOT_TOKEN)
    else:
        print("  (DAIMON_SLACK__BOT_TOKEN not set — probe message posting skipped)")
        print("  Post a channels_select block manually and interact with it.")

    client = SocketModeClient(app_token=APP_TOKEN)

    result: dict[str, object] = {}  # populated by the listener on match

    async def on_request(sock_client: SocketModeClient, req: SocketModeRequest) -> None:
        # Ack immediately — probes must never let Slack time out.
        await sock_client.send_socket_mode_response(
            SocketModeResponse(envelope_id=req.envelope_id)
        )

        ts = _ts()
        req_type = req.type or "unknown"
        payload: dict[str, object] = req.payload or {}  # type: ignore[assignment]
        payload_type = payload.get("type", "—")

        print(f"[{ts}] req.type={req_type!r}  payload.type={payload_type!r}")

        if req_type != "interactive":
            return

        # Log the full interactive payload for inspection.
        print("  Full payload (JSON):")
        print("  " + json.dumps(payload, indent=2, default=str).replace("\n", "\n  "))

        if str(payload_type) != "block_actions":
            print(f"  (not block_actions — payload.type={payload_type!r}, skipping)")
            return

        actions: list[dict[str, object]] = []
        raw_actions = payload.get("actions")
        if isinstance(raw_actions, list):
            actions = raw_actions  # type: ignore[assignment]

        matching = [
            a for a in actions
            if a.get("action_id") == _ACTION_ID or a.get("type") == "channels_select"
        ]
        if not matching:
            print(f"  (block_actions but no {_ACTION_ID!r} action — might be a different button)")
            return

        action = matching[0]
        action_type = action.get("type")
        selected_channel = action.get("selected_channel")

        print()
        print(f"  *** channels_select action received (action_id={action.get('action_id')!r}) ***")
        print(f"      action.type={action_type!r}")
        print(f"      action.selected_channel={selected_channel!r}")
        print()

        # Determine PASS / FAIL.
        has_selected_channel = "selected_channel" in action
        passes = (
            req_type == "interactive"
            and str(payload_type) == "block_actions"
            and has_selected_channel
        )

        result["req_type"] = req_type
        result["payload_type"] = str(payload_type)
        result["action_type"] = str(action_type)
        result["selected_channel"] = selected_channel
        result["has_selected_channel"] = has_selected_channel
        result["pass"] = passes

        if passes:
            print(
                "  CONCLUSION: channels_select DOES fire block_actions over Socket Mode\n"
                "              with 'selected_channel' field present. D-05 CONFIRMED.\n"
                "              Phase 83 can use channels_select natively."
            )
        else:
            print(
                "  CONCLUSION: UNEXPECTED — block_actions received but 'selected_channel'\n"
                f"              is {'MISSING' if not has_selected_channel else 'present'}.\n"
                "              Action keys: " + str(list(action.keys()))
            )

        # Signal the main loop to stop after logging.
        await client.close()

    client.socket_mode_request_listeners.append(on_request)

    try:
        await client.connect()
        print("Connected.  Logging all Socket Mode events …\n")

        # Post the probe message if we have a bot token + target channel.
        if web_client is not None and TARGET_CHANNEL:
            await _post_probe_message(web_client, TARGET_CHANNEL)
        else:
            print(
                "  Interact with a channels_select element in your workspace to trigger.\n"
                "  (Set PROBE_TARGET_CHANNEL=C... and DAIMON_SLACK__BOT_TOKEN=xoxb-... to\n"
                "   have the probe post the test message automatically.)\n"
            )

        print("Press Ctrl-C to stop.\n")
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nProbe stopped.")
    finally:
        await client.close()
        _print_summary(result)


def _print_summary(result: dict[str, object]) -> None:
    print()
    print("=" * 60)
    print("PROBE SUMMARY (A1, D-05): channels_select over Socket Mode")
    print("=" * 60)

    if not result:
        print(
            "RESULT: UNKNOWN — no channels_select block_actions event was observed.\n"
            "  Either no interaction was triggered, or the probe was stopped early.\n"
            "  Re-run the probe and select a channel in the Slack workspace."
        )
        print()
        print("FALLBACK: use static_select-from-conversations_list (specified in 83-UI-SPEC.md).")
        return

    passed = result.get("pass", False)
    selected_channel = result.get("selected_channel")

    if passed:
        print(
            "RESULT: PASS\n"
            "  channels_select fires req.type='interactive', payload.type='block_actions'\n"
            f"  with 'selected_channel'={selected_channel!r} in actions[0].\n"
            "  D-05 is RESOLVED — channels_select is safe to use in Phase 83."
        )
    else:
        print(
            "RESULT: FAIL / UNEXPECTED\n"
            f"  req.type={result.get('req_type')!r}\n"
            f"  payload.type={result.get('payload_type')!r}\n"
            f"  has_selected_channel={result.get('has_selected_channel')!r}\n"
            f"  selected_channel={selected_channel!r}\n"
            "  Phase 83 should use the static_select fallback (83-UI-SPEC.md)."
        )
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(_run_probe())
