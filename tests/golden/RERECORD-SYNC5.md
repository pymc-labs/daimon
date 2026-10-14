# SYNC-5 whole-record provenance

Recorded from the reviewed N2 re-merge `f715cbb8d4d2c497ee331389e6da878c399ba8d9`, with only the scripted event-history fixture fix. The 33 fresh legacy replays passed their original scenario assertions. Eleven recordings change; twenty-two remain byte-identical. Both turn paths compare against these same files; no separate mux recordings exist.

Every changed field is explained by these exact incoming main anchors:

- **#824**, `0733181847dc775bda444d51c2467016780b8d5d`: `packages/core/daimon/core/turn/run.py:985` runs catch-up after the completed turn; `packages/core/daimon/core/usage_reconcile.py:172` lists native events using ascending order, limit 100, `span.model_request_end`, and optional inclusive lower/exclusive upper date bounds. Each new effect captures the exact GET, beta/version headers, query, status 200 and native `{data, next_page}` response. Its usage objects are the previously streamed objects, not invented spans. Subsequent turns also capture the cursor overlap/repair read; empty repair pages are genuinely empty.
- **#831**, `868723866e47875c6428a25d1ce6c5cadd26ec9c`: `packages/core/daimon/core/turn/notices.py:55,163,210` changes only the depleted-credit next-step copy to name `/billing`.
- **#813**, `f84699cf6ac7ad467ef59beaa44b65488005c60d` (main merge `073edf25e9a485f0977dff863da074ff93d0bb36`): `packages/adapters/discord/daimon/adapters/discord/context.py:108` appends the reply-target instruction inside the POST user message's delivery hint. No other prompt bytes change.
- **#833**, `677927b3db575e4061a390d7e05222a2d5e8e0c0`: `packages/testing/daimon/testing/ma_sessions.py:286` adds the installed SDK's native `next_page` field to Files pages. The four captured handoff Files responses gain `next_page: null`; requests, file metadata, bytes and upload/delete order stay exact.

Counts below count individual changed effects or prompt blocks.

| Golden | Complete change |
| --- | --- |
| `blocked_balance.json` | #831: depletion message names `/billing` (1) |
| `cold_discord.json` | #824: post-turn history GET (1) |
| `cold_slack.json` | #824: post-turn history GET (1) |
| `dead_session.json` | #824: post-turn history GET (1) |
| `dm_delivery.json` | #824: post-turn history GET (3) |
| `dm_turn.json` | #824: post-turn history GET (3) |
| `handoff_continuation.json` | #824: post-turn history GET (5); #813: reply-target delivery hint (1) |
| `handoff_full.json` | #833: Files page `next_page: null` (4) |
| `plain_discord.json` | #824: post-turn history GET (1) |
| `plain_slack.json` | #824: post-turn history GET (1) |
| `wake_continuation.json` | #824: post-turn history GET (5); #813: reply-target delivery hint (1) |

The independent evidence audit compares every root section and every effect. Reverting **only** the cited GET insertions, exact message/hint substitutions, and added Files response field yields each prior whole record exactly. All five legacy database rowsets remain exact across all 33 scenarios, including money, ledger dating, continuity and usage identities. The audit projection is an evidence script only: the committed comparator, normalization, instrumentation and scenario assertions are unchanged. Mutation payloads and complete-record difference requirements remain intact; the #823 target move is documented below.

The fail-closed transport route serves recorded history per session, including an empty page before any stream. It applies type/time/order filters without generating new events. Foreign sessions remain outside a scoped router; unknown query keys, cursors and unsupported continuation pages fail loudly. Focused real-SDK negatives exercise those boundaries. The 120-second oracle child timeout remains unchanged.

Main #823 (`c2300c7dbf59b12cb10e1975a6f96fe64e2495b3`) moved DM delivery from `DirectMessageCog.on_message` into its drain-tracked `_reply` helper. The `dm_delivery` mutation now targets that helper's unchanged answer slice. Its replacement text, original scenario assertions and complete-record difference requirement remain unchanged. Both retargeted mutation controls pass; this target move changes no golden bytes.
