"""Spike D-04: does external_select suggestion_request route over the Socket Mode WebSocket?

Background
----------
Block Kit ``external_select`` elements call an "options load URL" when the user types in the
select.  In Socket Mode apps the Slack docs imply this request is delivered over the same
WebSocket as other events rather than via an HTTP endpoint — but the docs do NOT state this
explicitly.

D-04 (Phase 80 CONTEXT, Claude's Discretion): prototype ``external_select``-over-Socket-Mode
as a **non-blocking spike**.  ``static_select`` remains the v1 default regardless of the
outcome.  The finding here is input for Phases 82/83, which own the interactive picker UX.

NON-BLOCKING: this spike does NOT gate Phase 80 success.  The phase ships with static_select
as the v1 default.  This probe exists only to record whether ``suggestion_request`` events
arrive over the Socket Mode WebSocket so Phases 82/83 can make an informed decision.

Finding format
--------------
After running the probe against a test Block Kit message containing an ``external_select``
element, record one of:

  external_select suggestion_request DOES route over Socket Mode
      (req.type=="interactive" with type=="block_suggestion" or payload.type=="block_suggestion")
  OR
  external_select suggestion_request DOES NOT route over Socket Mode
      (no interactive event of that type observed after interacting with external_select)

Setup
-----
1. Install a Slack app on a test workspace with Socket Mode enabled (``xapp-`` app token).
2. Add an ``external_select`` element to a Block Kit message and post it to a test channel
   via the Slack API or a separate script.  The element must reference your app's
   options_load_url (not needed in Socket Mode apps — Slack routes the request via WS).
3. Set the env var:
     export DAIMON_SLACK__APP_TOKEN=xapp-...

Run
----
    uv run python scripts/probes/slack/external_select_socket_mode_spike.py

The probe opens a Socket Mode connection and logs EVERY incoming event type.  Interact with
the ``external_select`` element (type into the select dropdown) and watch the output.  If
``suggestion_request`` / ``block_suggestion`` appears, the finding is confirmed.

Press Ctrl-C to exit.

For Phases 82/83
-----------------
If the finding is DOES route over Socket Mode:
  - ``external_select`` pickers can be used in Phase 82/83 Block Kit surfaces without any
    HTTP options-load endpoint.
  - Handle ``req.type == "interactive"`` with ``payload["type"] == "block_suggestion"`` in
    the SlackApp listener to return picker options.

If the finding is DOES NOT route over Socket Mode:
  - ``external_select`` pickers require an HTTP options-load URL and cannot be driven via
    the Socket Mode WebSocket alone.
  - Phases 82/83 should use ``static_select`` or a custom REST endpoint approach.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Env gate — exit cleanly if the app token is absent
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

# ---------------------------------------------------------------------------
# Imports — slack-sdk is a runtime dep of the slack adapter; no new installs.
# ---------------------------------------------------------------------------

from slack_sdk.socket_mode.aiohttp import SocketModeClient  # noqa: E402
from slack_sdk.socket_mode.request import SocketModeRequest  # noqa: E402
from slack_sdk.socket_mode.response import SocketModeResponse  # noqa: E402

# Types that indicate an external_select options request
_SUGGESTION_TYPES = frozenset({"block_suggestion", "dialog_suggestion"})


def _ts() -> str:
    return datetime.now(tz=timezone.utc).strftime("%H:%M:%S.%f")[:-3]


async def _run_spike() -> None:
    print("external_select Socket Mode spike (D-04, NON-BLOCKING)")
    print("=" * 60)
    print("Purpose: record whether suggestion_request / block_suggestion")
    print("         routes over the Socket Mode WebSocket.")
    print("Phase 80 ships with static_select regardless of this outcome.")
    print("Finding is input for Phases 82/83 interactive picker UX.")
    print("=" * 60)
    print()
    print("Connecting to Slack via Socket Mode …")
    print(
        "Interact with an external_select element in your test workspace to trigger.\n"
        "Press Ctrl-C to stop.\n"
    )

    client = SocketModeClient(app_token=APP_TOKEN)

    suggestion_observed = False

    async def on_request(sock_client: SocketModeClient, req: SocketModeRequest) -> None:
        nonlocal suggestion_observed

        # Ack immediately — probes must never let Slack time out.
        await sock_client.send_socket_mode_response(
            SocketModeResponse(envelope_id=req.envelope_id)
        )

        ts = _ts()
        req_type = req.type or "unknown"
        payload: dict[str, object] = req.payload or {}  # type: ignore[assignment]
        payload_type = payload.get("type", "—")

        print(f"[{ts}] req.type={req_type!r}  payload.type={payload_type!r}")

        # Log the full payload for interactive events (most informative)
        if req_type == "interactive":
            print("  Full payload (JSON):")
            print("  " + json.dumps(payload, indent=2, default=str).replace("\n", "\n  "))

        # Check for the specific suggestion_request pattern
        is_suggestion = (
            req_type == "interactive" and str(payload_type) in _SUGGESTION_TYPES
        )
        if is_suggestion:
            suggestion_observed = True
            value = payload.get("value", "")
            action_id = payload.get("action_id", "")
            print()
            print("  *** FINDING: external_select suggestion_request observed over Socket Mode ***")
            print(f"      payload.type={payload_type!r}  value={value!r}  action_id={action_id!r}")
            print()
            print(
                "  CONCLUSION: external_select suggestion_request DOES route over Socket Mode.\n"
                "  Phases 82/83 can use external_select without an HTTP options-load endpoint."
            )
            print()

    client.socket_mode_request_listeners.append(on_request)

    try:
        await client.connect()
        print("Connected.  Logging all Socket Mode events …\n")
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nProbe stopped by user.")
    finally:
        await client.close()
        print("\nSocket Mode connection closed.")
        print()
        print("=" * 60)
        print("SPIKE SUMMARY (D-04)")
        print("=" * 60)
        if suggestion_observed:
            print(
                "FINDING: external_select suggestion_request DOES route over Socket Mode\n"
                "  => Phases 82/83 CAN use external_select via Socket Mode without an HTTP endpoint."
            )
        else:
            print(
                "FINDING: external_select suggestion_request was NOT observed over Socket Mode\n"
                "  (Either no interaction was triggered, or it routes via HTTP, not the WS.)\n"
                "  => If no interaction was triggered: re-run and interact with the external_select.\n"
                "  => If interaction WAS triggered: DOES NOT route over Socket Mode.\n"
                "     Phases 82/83 should use static_select or a REST options-load endpoint."
            )
        print()
        print("NOTE: This spike does NOT gate Phase 80.  static_select is the v1 default.")


if __name__ == "__main__":
    asyncio.run(_run_spike())
