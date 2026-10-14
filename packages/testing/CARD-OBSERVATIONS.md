# Offline card observations

`daimon.testing.card_observation` binds historical Discord card delivery to the
actual adapter's public message reference and accepted transport transcript.
It does not turn model text, native tool results or headless return text into
platform or workspace evidence.

Construct `CardObserver` with the owned harness's `snapshot`, the actual
adapter's card reference, the independently routed destination/thread and the
same monotonic clock used by the transport. Wrap `post_initial` as `initial`,
`on_render` during a running turn as `running`, finalizer renders as `finishing`,
and actual terminal delivery as `terminal`. Observe the actual adapter hooks;
the outer recording lifecycle alone cannot prove delivery.

After the adapter settles, call `bind_card_posts` with the independent native
root `TurnEvidence`, the final transport capture, the observer's trace and the
owned route/window. It revalidates serialized evidence, checks contiguous post
cursors, unique message creation, live edit/delete ownership, actual reference,
destination, clock windows and surviving message contents. An acknowledged,
authoritative native terminal and complete adapter delivery are both required.

The returned `TurnEvidence` contains only the card domain, so existing text,
approval and tool effects retain their own observation order. Use
`evaluate_card_posts` for the unchanged `progress_seen` and `card_finalized`
predicates. `card_edits_check` independently rebinds the original transcript and
counts accepted edits of that card during running callbacks before native
completion. Initial sends, finalizer/terminal edits and renders without I/O do
not count. A terminal callback whose delivered payload still uses Discord's
progress colour fails finalization.

This boundary proves historical delivery. It does not certify current-card
refetches after later waits, attachments, remote tools, accounting, or a live
provider's timing. Slack/Teams state classification remains explicitly
incomplete. Missing native authority or delivery coverage is PENDING. Fixtures
and scored catalog assertions remain unchanged; supplementary controls earn no
full-cell credit.
