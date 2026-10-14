# Accepted initial Discord card receipts

`DiscordTurnLifecycle` accepts optional synchronous
`on_initial_progress(InitialProgressReceipt)` from
`daimon.adapters.discord.progress_receipt`. The default is `None`: no
observer, extra clock sample, serialization or additional platform/provider I/O.
Supply the same shared monotonic clock already used by the owned host capture.

The actual `post_initial` send/edit branch emits only after its awaited SDK
operation succeeds and the public card reference is assigned. The immutable,
closed receipt carries `platform`, `phase="initial"`, `operation`,
`destination_id`, `thread_id`, `message_id`, `rendered_json` and `observed_s`.
Discord threads are the destination channel itself; Slack has an explicit thread
timestamp; Teams uses its full conversation id (including a thread suffix).
Cancel identifiers and requester restrictions are preserved. JSON roundtrip is
supported. Observer exceptions propagate in the opted-in path.

This is an accepted platform post fact, even if subsequent durable persistence
fails. Suppressed, failed and debounced posts produce no receipt; confirming an
existing Discord persistence callback without another post produces none.
An actually edited adopted card emits `operation="edit"` and needs independent
ownership of the earlier post. Ordinary render/terminal callbacks emit no initial
receipt. Teams repeated initial calls actually edit and therefore emit receipts.

`rendered_json` is the native render input from that post, not an independently
captured entire HTTP request or a running-root certificate. The consumer must
join the actual accepted wire post and public reference, unchanged destination
and thread, owned post order/callback window and shared clock; classify actual
wire UI through the separate card surface decoder. Later native acceptance,
persisted binding and a fresh authoritative root are separate evidence. No
session/root id is invented before binding. N3/N5/N9 own those joins and the
source oracle's initial-versus-running decision. Missing joins stay PENDING;
this hook neither releases a completion gate nor awards any matrix-cell PASS.

Offline regressions execute the real adapter and SDK serializer, compare the
accepted requests and public references, hold the SDK ACK, exercise failure,
recovery and immutable receipts, and replay the positive receipt check with
production posting removed. Anthropic request/render behavior is unchanged.
