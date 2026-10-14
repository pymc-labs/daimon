# Sync6 golden regeneration provenance

Pinned main: `cb48d92702b1fa057bffe1e9c75827e167262929`. Integration base: `ffe39729c1979e0e0fdc0eee6f5903c624ba30b2`.
Regenerated candidate: `178e051d45f01dc404e35cd48fd2b90e50006987` plus N2’s captured owner fixture fixes (SHA256 `328eee0ad123ceb82a024765b917d2ee47c8a2b4c41519e8e5c0442ec3661598`).
Artifact parent: N2 committed candidate fixes `60693785f534a9977795e2ec13dbf1d0326f0292`. Its golden/runtime support equals the tested snapshot; additional ingress/catalog-profile tests and inventory metadata are outside these replay scenarios.

33 legacy scenarios were regenerated; 19 shared JSON files changed. N2’s 36 failure cases comprise 19 legacy and 17 mux records. All 19 regenerated legacy records and their 156 unified-diff hunk headers exactly match the reconstructed legacy records in `lanes/N2-state/sync6-h6/goldens-diff-178e051d4.log`.

Every attribution commit is an ancestor of pinned main and is absent from the integration base (verified with both merge-base checks). Source line numbers below refer to the listed main commit, not the subsequently ported candidate.

| Main PR | Full main commit | Hunk memberships | Files | Source-owned change |
| --- | --- | ---: | ---: | --- |
| #903 | `83fdcb087dca1c08d344ed949871762fe909e643` | 33 | 17 | List requests use limit=100 |
| #876 | `fe9e7eb02c3962fa5fb7f3685ad8c3359af7efae` | 35 | 9 | Turn-end descending settle, full recorded span history and removed old cutoff walks |
| #866 | `5f19f197dc3959c20d6c2b32e1ceed05ff19177c` | 85 | 11 | Personal stores, shared/personal mounts, snapshots/fingerprints and derived ID shifts |
| #922 | `794610a98ef743fc9302d7b77897d214768a8637` | 6 | 2 | Root files and literal/NUL-safe checkpoint tar pipeline |
| #864 | `54fc3401e3ff29e0329dcfe6d34f5265e191f345` | 2 | 1 | Fresh-message answer owns summary; old terminal card says Done without footer |

There are **156 unique hunks, 5 mixed-class hunks, zero unexplained hunks**. Membership counts overlap in those mixed hunks. The machine-readable classification records every changed old/new line’s JSON pointer and attribution; its hunk headers use the same runner diff algorithm as N2’s log.

- **#903 source anchors:** packages/core/daimon/core/defaults/ma_index.py:82,128,155; packages/core/daimon/core/ma.py:64,92.
- **#876 source anchors:** packages/core/daimon/core/usage_sweep.py:148-159; packages/core/daimon/core/turn/run.py:94.
- **#866 source anchors:** packages/core/daimon/core/memory_resource.py:55,86,112,258; packages/core/daimon/core/session_snapshot.py:120,188; packages/core/daimon/core/session_preparation_stages.py:324,359; packages/core/daimon/core/stores/agent_memory_stores.py:127; packages/adapters/scheduler/daimon/adapters/scheduler/main.py:520.
- **#922 source anchors:** packages/core/daimon/core/checkpoint_prompt.py:174,196,200,223.
- **#864 source anchors:** packages/adapters/discord/daimon/adapters/discord/lifecycle.py:936,966,1064.

Whole-record checks retain exact money, models, callers, errors, usage quantities and delivery order. Database facts compare exactly after reversing the single personal-store alias insertion and validating/recomputing the source-owned snapshot fingerprint. Native requests compare exactly after the listed source changes; settle pages must equal the filtered union of actually recorded preceding SSE spans. Platform payloads compare exactly after the personal-store aliases, scheduler personal-memory flag and #864 card projection. No comparison or assertion was changed.

For #866, personal creation precedes shared creation, adds one host personal-store binding UUID and shifts subsequent `<id:N>` aliases and origin-context UUIDs consistently; the retained values and billing references still join. Scheduled shared routines explicitly carry `personal_memory: false`; existing snapshots additionally carry a null personal-store field.

#922 changes four `handoff_full` effect-4 text fields and two `handoff_transcript` effect-5 payload text fields. All six contain exactly the source’s five insertions (root-file wording, find pipe and tar literal/NUL flags); no extra prompt text changes remain. #864 changes `cold_discord`’s terminal edit payload; the answer’s summary and cost/balance remain unchanged.

| Golden file | Runner diff hunks | Main PRs |
| --- | ---: | --- |
| `billing_replay.json` | 1 | #903 |
| `blocked_balance.json` | 2 | #903 |
| `blocked_cap.json` | 2 | #903 |
| `cold_discord.json` | 23 | #864, #866, #876, #903 |
| `cold_slack.json` | 22 | #866, #876, #903 |
| `dead_session.json` | 5 | #866, #876, #903 |
| `dm_delivery.json` | 30 | #866, #876, #903 |
| `dm_turn.json` | 30 | #866, #876, #903 |
| `handoff_continuation.json` | 9 | #866, #876, #903 |
| `handoff_full.json` | 5 | #903, #922 |
| `handoff_history.json` | 1 | #903 |
| `handoff_transcript.json` | 3 | #903, #922 |
| `mcp_start_turn.json` | 2 | #903 |
| `plain_discord.json` | 4 | #866, #876, #903 |
| `plain_slack.json` | 4 | #866, #876, #903 |
| `reconnect.json` | 1 | #903 |
| `scheduler_run.json` | 1 | #866 |
| `sealed_channel.json` | 1 | #866 |
| `wake_continuation.json` | 10 | #866, #876, #903 |

Verification:

- Regeneration: **33 PASS**, 268.90s, unmutated legacy only.
- Owner dual-path + controls: **71 PASS, 3 FAIL** (536.99s): all **33 legacy matches PASS**, 30 mux matches PASS and **all eight mutation controls PASS**. Four real production mutations (Slack eyes, Discord eyes, ledger dating and DM delivery) were checked independently on each path.
- Remaining candidate composition omissions: mux `reconnect`, `billing_replay`, `dead_session` lack only #903’s `["limit", "100"]` on a history read. They were reported to N2/N4/N6; the goldens keep main’s correct request. Candidate-wide parity is **not claimed green**.
- The prior N2 mux-log records match the shared regenerated record in 16 of 17 changed mux cases; the remaining `dead_session` record shows that same missing page-size pair. Its other main-attributed changes match.

Evidence: `/home/clsandoval/cs/daimon-neutral-core-20261009/lanes/N3-oracle/sync6-goldens-20261013T140953Z` (`classification.json`, `classify.py`, exact source snapshot, reconstructed baseline records/diffs, regeneration and dual-path JUnit/logs). The classifier is evidence outside shipped packages; it does not alter the oracle or normalize production captures.

No product source, scenario code, comparison rules or mutation controls are changed by this artifact commit. No provider calls, key reads or migration changes.
