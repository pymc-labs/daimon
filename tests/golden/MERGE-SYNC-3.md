# SYNC-3 merge provenance

Initial integration parent: `5b32481bc5c30fc41d1dfbe490c3bb5dc9a227e8`.
Pinned main parent: `a1bad886f59b05b53b82335545905eaf8c9c1b7a` (49 commits since the prior main sync).

This authorized local merge brings main #533, #535, #537, #540, #542, #546, #550, #554 and #555, plus the intervening fixes. Main behavior and neutral ports must both be retained. Final integration reconciliation and verification follow the lead's merge queue.

Migration audit: all 85 migration files from main are byte-identical. Main's head remains `0076_github_connect_followup`; neutral `0077_neutral_state` follows it, then `0078_usage_observation_revision` is the single head. No renumbering or main re-parenting is necessary.

## Main's added MA calls

An AST comparison against prior main `c77c7090a1821e1cf99e35ccaa0d9552f28f9b72` finds three newly introduced production SDK calls, all from #542/#546 stuck-confirmation recovery. Main's outside-driver count rises from 247 to 250. N4 moved sends/status through TurnIO and placed the history query in `LegacyTurnTransport.latest_idle_is_settled` under `packages/mux/mux/drivers/anthropic/transport.py`. None of the three remains outside drivers, so the ratchet must remain at 82 or fall. Source locations on pinned main are:

| Main source location | Added operation | Main PR | Owner |
| --- | --- | --- | --- |
| `packages/core/daimon/core/turn/driver.py:694` | Send `user.interrupt` during confirmation recovery. | #542, tightened by #546. | N4 |
| `packages/core/daimon/core/turn/driver.py:696` | Retrieve recovery session status. | #542, tightened by #546. | N4 |
| `packages/core/daimon/core/turn/driver.py:738` | List the latest idle event to confirm the interrupt was accepted. | #546. | N4 |

Rule 23 preserves main migration bytes. The 20:45 SYNC ratchet exception permits only calls main added; no unrelated increase is authorized.

## Owner resolutions

N6 supplied the exact agent-fork patch in `inbox/20261009T204914Z-N6-SYNC-3-NOTE.md`. Scoped creation remains intact, followed by main #535 face scheduling using provider-returned metadata. N6 reviewed the auto-merged channel-rules argument forwarding and confirmed no additional change was needed.

N4 supplied the driver/transport/proof-test patch in `inbox/20261009T205349Z-N4-N3-SYNC-3-TURN-RESOLUTION-NOTE.md`. Main #542/#546 request order, refusal cause, single recovery, stop/deadline bounds and terminal callbacks remain. The new proof checks real SDK wire/effect equivalence and injected-backend isolation.

The explicitly granted recovery cut is recorded by the owner in sprint FOLLOWUPS.md: until N4's cancel/filtered-history seam lands, a definite pending-confirmation 400 on the default Anthropic mux composition switches only that turn to LegacyTurnIO for recovery. An injected backend finalizes the original refusal without SDK recovery calls; uncertain/network failures never trigger this fallback. This remains a gap before M0 certification.

## Composed merge and oracle

Merge commit: `9e87ebe80d6f2285752ac5735a90276fe4b7fa19`.
Resolved outside-driver SDK count: **82 baseline / 82 current**, with zero parse errors and no retained main-only addition outside drivers. The inventory baseline and literal ceiling are unchanged.

Five shared goldens change solely for main #530's Discord subtext label, in ten content fields. All other transcript bytes remain identical. [RERECORD-20261009.md](RERECORD-20261009.md) lists every field and coverage limitation. The three full regeneration rounds, both-path comparison and full-suite evidence for the pinned merge are in the `a6927d8c2bca856d7b9a91428b1704ed97741992` READY report. They do not cover the integration reconciliation below, which receives fresh focused verification and a delta re-sign at its own head.


## Integration queue reconciliation

Reconciliation merges integration `f6de8d70242acc7586b2eff8b42a258490035510` into the dual-signed SYNC-3 head `a6927d8c2bca856d7b9a91428b1704ed97741992`. It includes #568 Gemini resources, #578 N6 memory/resolver/reader ports, #580 image-mirror CI, #581 N5 workspace transfer ports and #582 N8 usage sweep/headless ports. Main remains pinned at `a1bad886f59b05b53b82335545905eaf8c9c1b7a`; no later main change is included.

The only code conflict is reader-agent creation: retain main's explicit no-face-queue comment with N6's scoped `create_agent` call. The lead explicitly approved the exact proposed resolution on behalf of the owner. No reader face queue is introduced. Main's 85 migrations and both neutral migrations remain unchanged, with sole head `0078_usage_observation_revision`.

The complete inventory is regenerated for the composed tree, including test entries noted in the evaluator's LOW finding: 2,099 scanned files, 726 SDK calls and zero parse errors. The outside-driver count and literal ceiling are **56**, down from signed SYNC-3's 82. The incoming ports remove 26 calls (25 distinct file/scope/call keys); there are no added outside-driver sites. The shared 33 golden files remain unchanged from signed SYNC-3 and receive a fresh both-path matrix with mutation probes. Focused coverage includes the incoming resources, workspace transfer, usage sweep/headless and Gemini changes, reader/recovery composition, migrations and CI lint. The lead explicitly waived a second full suite for this reconciliation; its exact new head and results are recorded in a separate READY, then receive delta re-signatures.
