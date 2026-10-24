`mux.testing.drivers.gemini.conformance_sdk.offline_sdk_adapter()` is an explicitly
imported async context manager for the C06/C07 SDK component. It supplies a fresh
Gemini driver, memory storage/StateStore, fixed MockTransport and a literal
non-secret offline SDK credential. It cannot discover a live key or install
itself in the host factory. It closes its owned client on exit. No existing
registry or default adapter is replaced.

The upstream script supplies only native responses and faults. Real HTTP request
body bytes cross the pinned Google SDK and are retained independently of driver
receipts. Driver projections are never edited. Every request uses explicit
`tools: []`, the canonical `FLASH_PRIMARY`, the pinned API revision, no retries,
no redirects and `trust_env=False`. A driver/store restart retains committed
records and reattaches the same explicitly owned SDK edge.

The unchanged shared C06 oracle passes through one interaction POST, one cancel
POST and exact-owned interaction GETs. A separate proof opens an empty SDK SSE
stream: cancellation acceptance and EOF leave root occupancy intact. Only a
newer canonical cancelled snapshot releases it, producing exactly one interrupted
root outcome even when read repeatedly. No child/preview completion is fabricated.

The unchanged shared C07 oracle observes native SDK snapshots with output counts
null,100,120,110. Stable interaction identity and revisioned observations feed
the memory accounting outbox once, including its synthetic signed correction;
restart/stale replay does not debit again. This is controlled contract evidence,
not exact live settlement. It does not alter terminal-GET monotonicity, max-seen
billing bounds, dated prices, reservations, holds or the canonical spend ledger.

C08 supplies actual changed tool and repository-mount specifications, and checks
that plan_update refuses and apply_update throws UnsupportedCapability without
any SDK mutation or effective session revision change. No occupied session is
silently reconfigured. C09 proves exact local binary artifact bytes survive a
restart and foreign scope refuses. Local artifacts are not native uploads or
vaults. Required vault admission and native session deletion refuse before SDK
I/O. Full C08/C09 remain typed PENDING with their existing reasons.

This component declares only C06/C07 executable; its other sixteen envelopes are
typed PENDING before dispatch. The existing standard Gemini offline adapter
retains its eight PASS/ten PENDING matrix. None of these proofs promotes a live
C-case, claims a complete certificate or supplies production durable storage.
N4 owns host lease/journal authority, N5 continuity envelopes, N7 platform
adapters and N9 fixture composition. The C04/C11 hooks are a separate integration
branch; no unmerged code is required here.

Native interactions/cancel/reuse and MCP/environment shapes are checked against
the retained official source-G-runtime/env snapshots, including the background
cancel example at source-G-runtime.txt:851. The SDK supplies its documented
cancel route variant; the independent body/path tests accept both native forms.
Sources: https://ai.google.dev/gemini-api/docs/antigravity-agent and
https://ai.google.dev/gemini-api/docs/agent-environment. No live/provider calls,
keys, normalized tape mutations, golden changes or historical hold releases.
