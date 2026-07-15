"""Probe: characterize the Slack Connect home-team field on shared-channel app_mention events.

Background (STURN-04 A2)
--------------------------
In a Slack Connect shared channel, the outer ``team_id`` of an ``app_mention`` event is the
channel-host workspace, NOT the external user's home workspace.  The gating function
``is_slack_connect_external`` (Plan 80-03) needs to compare the user's home team against the
host ``team_id`` to reject cross-tenant mentions safely.

Problem: the exact field name is MEDIUM-confidence ambiguous across Slack docs and SDK
implementations:
  - Slack docs / slack-go use ``user_team`` (``AppMentionEvent.UserTeam``)
  - Some Slack API references use ``source_team``
  - A third candidate ``user_team_id`` appears in certain Slack API contexts

Plan 80-03 ships a **defensive multi-field check** (treats any of the three as the home team
if present and != ``team_id``).  This probe CONFIRMS which field actually appears on a real
shared-channel ``app_mention``, so a future cleanup can narrow the defensive list to the
confirmed key only.

NON-BLOCKING: this probe does NOT gate Phase 80 success.  The multi-field check in
``gating.py`` already covers all candidates regardless of probe outcome.

Setup
------
1. You need a Slack app installed on at least ONE workspace (the "host") with Socket Mode
   enabled (``xapp-`` app-level token).
2. Create a Slack Connect shared channel and invite at least ONE user from a DIFFERENT
   (external) workspace.
3. Set the env var:
     export DAIMON_SLACK__APP_TOKEN=xapp-...

Run
----
    uv run python scripts/probes/slack/slack_connect_field_probe.py

The probe opens a Socket Mode connection and listens indefinitely.  Once the process is
running, have a user from the EXTERNAL workspace mention the bot (@BotName) in the shared
channel.

Reading the output
-------------------
For each ``app_mention`` received the probe prints:
  - The full raw event dict (JSON)
  - Which of ``user_team``, ``user_team_id``, ``source_team`` are present and their values
  - Whether each value matches the outer ``team_id``
  - A self-check line:  "FIELD CONFIRMED: <key>=<value>"
    (printed when a home-team field != outer team_id is observed — i.e., Slack Connect event)

Internal mentions (same workspace) will NOT trigger the confirmation line; the probe reports
them as normal events for comparison.

Press Ctrl-C to exit.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

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

# Candidate home-team field names (STURN-04 A2)
_HOME_TEAM_CANDIDATES = ("user_team", "user_team_id", "source_team")


def _analyze_event(event: dict[str, object]) -> None:
    """Print the full event plus a home-team field report."""
    print("\n" + "=" * 70)
    print("app_mention received")
    print("=" * 70)
    print("Full event (JSON):")
    print(json.dumps(event, indent=2, default=str))

    outer_team_id = event.get("team")  # top-level team = host workspace
    print(f"\nOuter team_id (host workspace): {outer_team_id!r}")
    print("\nHome-team candidate field report:")
    confirmed: list[str] = []
    for key in _HOME_TEAM_CANDIDATES:
        value = event.get(key)
        if value is not None:
            match = value == outer_team_id
            status = "SAME AS HOST (internal mention)" if match else "DIFFERENT (external user)"
            print(f"  {key!r}: {value!r}  →  {status}")
            if not match:
                confirmed.append(key)
        else:
            print(f"  {key!r}: NOT PRESENT")

    if confirmed:
        for key in confirmed:
            value = event.get(key)
            print(f"\nFIELD CONFIRMED: {key}={value!r}  (home team differs from host team_id)")
        print(
            "\nConclusion: gating.py's defensive multi-field check WOULD correctly catch this\n"
            "Slack Connect mention via the confirmed field(s) above."
        )
    else:
        print(
            "\nNo home-team field found that differs from the outer team_id.\n"
            "This is either:\n"
            "  a) An internal (same-workspace) mention — no Slack Connect field expected, OR\n"
            "  b) A Slack Connect mention where none of the candidate fields are present.\n"
            "     If (b), the Slack API shape differs from all documented candidates — update\n"
            "     gating.py and this probe."
        )
    print()


async def _run_probe() -> None:
    print("Slack Connect home-team field probe")
    print("Connecting to Slack via Socket Mode …")
    print("(Mention the bot from an EXTERNAL workspace in a shared/Connect channel to trigger)")
    print("Press Ctrl-C to stop.\n")

    client = SocketModeClient(app_token=APP_TOKEN)

    async def on_request(sock_client: SocketModeClient, req: SocketModeRequest) -> None:
        # Ack immediately — probes should never let Slack time out.
        await sock_client.send_socket_mode_response(
            SocketModeResponse(envelope_id=req.envelope_id)
        )

        if req.type != "events_api":
            print(f"[{req.type}] (not events_api — skipping)")
            return

        payload: dict[str, object] = req.payload  # type: ignore[assignment]
        event: dict[str, object] = payload.get("event", {})  # type: ignore[assignment]
        event_type = event.get("type")

        if event_type == "app_mention":
            _analyze_event(event)
        else:
            print(f"[events_api] event.type={event_type!r} (not app_mention — skipping)")

    client.socket_mode_request_listeners.append(on_request)

    try:
        await client.connect()
        print("Connected.  Waiting for events …\n")
        # Run indefinitely until Ctrl-C
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nProbe stopped by user.")
    finally:
        await client.close()
        print("Socket Mode connection closed.")


if __name__ == "__main__":
    asyncio.run(_run_probe())
