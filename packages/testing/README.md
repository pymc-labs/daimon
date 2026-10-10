# Discord surface capture

`daimon.testing.discord_surface.DiscordSurfaceHarness` captures offline Discord
posts through the unchanged `DiscordTurnLifecycle` and `DiscordPostTransport`.
Install the Discord adapter alongside `daimon-testing` (the workspace already
includes it). Import this module explicitly; ordinary testing imports do not
require Discord.

The harness creates real `discord.py` client, thread and message objects, with an
in-process HTTP gateway replacing their network boundary. It never logs in,
reads credentials or opens a Discord connection. Unsupported gateway operations
raise rather than falling through to a real client. Provider traffic in a test
must separately use its offline transport.

```python
from daimon.testing.discord_surface import DiscordSurfaceHarness

capture = DiscordSurfaceHarness(
    evidence_id="scenario:turn:discord",
    session_id=acknowledged_session_id,
    root_turn_id=acknowledged_root_id,
    clock=runner_clock,
    render_tables=True,
)

async def invoke(lifecycle):
    await lifecycle.post_initial()
    return await offline_host_turn(lifecycle=lifecycle)

receipt = await capture.run(invoke)
```

Bind the lifecycle to the actual turn invocation. The invocation's returned value
is ignored. The runner must independently match session/root identity to its
acknowledged input and native terminal evidence; this capture does not prove
provider completion. A single successful adapter terminal delivery followed by
the invocation returning closes the post window. A missing or duplicate terminal,
exception or cancellation leaves coverage incomplete. `snapshot()` retains
partial observations after failure; a harness cannot be reused for a second turn.

`events` contains immutable, ordered send/edit/delete observations, unique
capture evidence IDs, actual serialized requests and resulting message payloads.
`messages` contains surviving messages in Discord post order, with exact content,
embed text, component labels, attachment names, message IDs and observation times.
Edits replace only supplied fields; deletion removes the message from the final
snapshot. Drafts remain in history but do not reappear in finalized text. Footers
and cards remain separate from message content so the consumer can select its
explicit predicate domain instead of treating a headless return as a Discord post.

`post_capture_complete` covers this accepting fake gateway's message boundary.
`text_capture_complete` additionally refuses complete coverage when a surviving
attachment could contain unobserved text. Table PNGs are captured as files, never
converted into fabricated visible strings. Neither flag certifies browser pixels,
real gateway timing, permissions, webhook identity, reactions, tool posts outside
this gateway, or the full bot ingress/admission path. The default bot transport
path is exercised; identity webhooks are explicitly disabled. Configure the
lifecycle's table, notification, cancel-view and unprompted options to match the
scenario before comparing its output. Account balance footers require the host's
DB context and are outside this standalone harness. No product defaults change.

N9 binds its `VisibleText` from these observations only after matching the owned
capture window and identity. Keep predicates depending on uncaptured domains
PENDING. Native outcomes and headless receipts remain independent evidence.
