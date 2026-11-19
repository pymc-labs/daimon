# Journey map

Every journey asks whether an authorized action changes the intended resource
and whether the visible outcome agrees with that effect. PASS requires all
listed observations. Missing evidence is PENDING; a contradicted assertion is
FAIL. An existing test reference identifies reusable coverage, not a new run.

| Journey | Scripted boundary (layer 1) | Browser boundary (layer 2) | Provider boundary (layer 3) |
| --- | --- | --- | --- |
| Mention and follow-up | Real turn driver, SDK MockTransport, persisted session and output receipt | Actual answer/card in the intended thread | Two Haiku 5.5 turns, retained context, model and usage receipts |
| Setup: select agent, return | Parity handlers, permission checks and stored selection | Private panel, labels, enabled controls, click and backend readback | No model needed |
| Private credential, wrong actor | Real submission handler and store, refusal has no write | Modal submission, private refusal, secret excluded from captures | Separate continuation canary, initially PENDING |
| Approve or deny tool call | Existing approval/parity fixtures and independent tool receipt | Card buttons, actual click, terminal control state and effect receipt | Remote tool execution canary, initially PENDING |
| Stop running turn | Existing cancellation and terminal-state regressions | Running and completed card controls, actual cancellation effect | Initially PENDING |
| GitHub: select, save, cancel, reopen | Existing route/store authorization tests | Real application page, DOM controls, resulting grants/working repo | GitHub consent remains PENDING with mocked external GitHub |
| Report: open, sidebar, meter | Existing report-host API tests | Real report page at desktop/mobile widths and host effect receipt | Sidebar provider behavior remains PENDING with fake provider |
| Notebook: publish, broken replacement | Existing store/host tests | Actual browser execution and previous good version retained | Initially PENDING |
| Billing, privacy export, memory | Existing store and adapter tests | Native panels and downloads, initially PENDING | No model needed for panel checks |
| Recovery with pending work | Existing controlled regressions and formal models | Post-recovery card agrees with backend, initially PENDING | Initially PENDING |

Discord and Slack handler evidence stays separate. Discord browser automation
uses one dedicated staging account at human pace. Slack browser access is
PENDING. Teams is excluded and remains in this inventory. Deletion is excluded.

## Evidence per browser step

Record the journey/step ID, actor and resource identities, viewport, browser
version, timestamp, screenshot, DOM labels/fields/button states, and an
independent backend receipt. A handler call or a locally rendered platform card
cannot satisfy a native-client observation. Never capture credential inputs,
cookies, user tokens, or login screens as journey artifacts.

Compare screenshots only against an explicitly accepted run with the same
browser settings. Keep side-by-side images for changed steps. A changed image
requests review; pixel equality alone cannot prove the backend effect. Missing
baseline or missing native evidence remains PENDING.

Run reports retain elapsed minutes, application/controller/judge token counts,
actual checks, observation gaps, and private artifact paths. Scripted tokens are
zero billed tokens even when a fake SDK event carries synthetic usage values.

## Reuse

`packages/testing/daimon/testing/{ma,ma_sessions,turn_router}.py` supplies the
scripted provider seam. `tests/parity` supplies real adapter entry points over
fake platform APIs. `qa/live` supplies staging identity checks, usage collection,
and the cost ledger. No production-accessible fixture switch is added.

The catalog/oracle on `integration/neutral-core` is a reference for evidence
completeness. Its 53-target external catalog is not available here, so this map
does not claim full catalog coverage. Its headless returns do not satisfy a
browser observation.
