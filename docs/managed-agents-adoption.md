# Managed Agents adoption plan

Status: plan, for approval. Checked against the Managed Agents docs, the
release notes and this repo on 2026-10-08. Each item says what it is, what
Daimon gains, what it touches, and a go or no-go recommendation. The order is
the recommended order of work.

Done already: Sonnet 5.5 cache reads are billed at $0.10 per million tokens
(PR #460). `pricing.py` now carries a dated source, and a test fails when a
listed price changes.

## Summary

| # | Item | Recommendation |
| --- | --- | --- |
| 1 | Refusal and other new idle stop reasons | Go, first |
| 2 | Dreams (memory tidy-up) | Go for a review-only trial once access is granted; see [Dreams design](dreams-design.md) |
| 3 | Haiku 5.5 for thread naming and the classifier | Go, with thinking off |
| 4 | Web tools after the 2026-10-07 change | Go: docs and one prompt line |
| 5 | Fable 5.1 as a selectable agent model | Go |
| 6 | SDK upgrade to 1.x | Go, as its own piece of work, Skills first |
| 7 | Per-session budget cap | Go after 1 and 6 |
| 8 | Session runtime in the cost model | Go, low priority |
| 9 | Advisor | No-go until its spend is shown to reach the ledger |
| 10 | `auto` permission policy | No-go |
| 11 | `inference_geo` | No-go until a tenant needs it |
| 12 | Fast mode | No-go for now |

A theme runs through most items: `cost_of` prices tokens by model id alone.
An unknown model id is recorded as free, and the fast-mode, US-inference and
advisor surcharges are not seen. Any item that adds a model or a price
multiplier must add its price in the same change.

## 1. Refusal and other new idle stop reasons

**What.** Since 2026-09-30 a session can go idle with `stop_reason`
`refusal` (a safety classifier declined the turn) and a `stop_details`
object: `category` (`cyber`, `bio`, `frontier_llm`, `reasoning_extraction`,
`general_harms` or null) and `explanation`. Since 2026-09-24, refusals that
arrive before any output are billed again in the `bio`, `frontier_llm` and
`reasoning_extraction` categories. Budgets (item 7) add a third idle reason,
`budget_reached`.

**Today.** SDK 0.117.0 does not know these values. It parses them as an
end-of-turn object with the raw string in `.type`, without raising.
`terminal_stop_reason` (`ma.py:122`) returns it, the driver ends the turn
(`turn/driver.py:1216`) and records it as `COMPLETED`
(`turn/termination.py`). The person then sees one of three things on Discord
(`adapters/discord/lifecycle.py:386-430`):

- whatever partial text exists, posted as the answer;
- "Turn cancelled." when there is no text;
- a done card with no explanation when only tools ran.

The MCP `ask` and `start_turn` pollers see an idle session with no reply.
`stop_details` is never read.

**Gain.** A refused turn says so in plain words, on every platform, and is
recorded as a refusal rather than a success. Billing stays right once
pre-output refusals cost money again.

**Blast radius.** `ma.py`, `turn/termination.py` (new `REFUSED` and
`BUDGET_REACHED` members), `turn/driver.py`, `turn/reducers.py` (keep
`stop_details`), and the terminal copy in the Discord, Slack, Teams and CLI
lifecycles. The new copy is user-facing, so it follows the UX bar: write the
words, review them, then build. This works on the current SDK by matching
`.type`.

Open question: the docs do not say whether a billed pre-output refusal emits
the `span.model_request_end` event that Daimon bills from. A contract test on
the staging workspace would show it.

**Recommendation: go, first.** It is the only item where users see something
wrong today.

## 2. Dreams

A hosted job that consolidates an agent's memory store from recent session
transcripts. The full design, the swap hazard, the session exclusions and the
billing gap are in the [Dreams design](dreams-design.md).

**Recommendation: go for a review-only trial** once the organisation has
research-preview access, which neither deployment key has today.

## 3. Haiku 5.5 for side calls

**What.** Haiku 5.5 costs $0.10 input and $0.50 output per million tokens
for prompts up to 100,000 tokens. Haiku 4.5 costs $1 and $5. Two Messages API
side calls run on Haiku 4.5 today: thread naming (`thread_naming.py:24`,
`max_tokens=60`) and the thread-participation classifier
(`thread_classifier.py`, `max_tokens=100`, model from
`DAIMON_THREAD_PARTICIPATION__CLASSIFIER_MODEL`, `config.py:362-368`).

**Gain.** About ten times cheaper side calls.

**Blast radius and traps.**

- Haiku 5.5 thinks by default, and thinking tokens count against
  `max_tokens`. At 60 or 100 tokens a reply can stop after a thinking block
  with no text. A missing title falls back to the static one. A missing
  classifier reply fails closed to silence, so the feature would quietly stop
  answering while still spending tokens. Fix: send
  `thinking={"type": "disabled"}`, which Haiku 5.5 accepts at its default
  `medium` effort.
- The same text counts as about 30% more tokens than on Haiku 4.5. Check the
  60-token title cap.
- `pricing.py` has no Haiku 5.5 row, so the calls would be recorded as free.
  Adding the row to `AGENT_MODEL_PRICING` also makes Haiku 5.5 selectable as
  an agent model. Whether to offer that is a separate decision; the row can
  sit in a side-call table instead.
- Changing the classifier default regenerates `docs/configuration.md` and
  `.env.example`.

**Recommendation: go**, with thinking off on both calls and a price row in
the same change.

## 4. Web tools after the 2026-10-07 change

**What.** Two changes landed on 2026-10-07:

- An environment with `limited` networking now applies its `allowed_hosts`
  to `web_search` and `web_fetch`. With no hosts listed, neither tool returns
  anything.
- `web_fetch` now fetches only URLs that already appeared in the session: in
  a user message, a search result or an earlier fetched page. A URL that
  appears only in the agent's own output, its system prompt, an attached file
  or a tool's output (`bash`, `read`, MCP) returns `url_not_in_prior_context`.

**Today.** The shipped `default` environment sets no `networking`, so it is
unrestricted. On 2026-10-08 every live environment in both deployments was
`unrestricted` (12 in staging, 16 in production). No channel loses web search
today. The closed environment that sealed channels are told to pick (limited,
no hosts, `channel_environments.py:112`) would now block both web tools. That
is what the seal intends, and it closes a path the seal did not cover before.
The `github_app_session.py:450` restriction is on a vault credential, not an
environment, and is unaffected.

The `web_fetch` rule applies to every channel. An agent can no longer fetch a
URL it built itself or found through `bash` or an MCP tool. Daimon has no
handling for either error; the agent just reports that it could not fetch.

**Gain.** No surprise failures, and a stated rule for sealed channels.

**Blast radius.** Docs only, plus one line in the default agent prompt
(`defaults/agents/daimon.yaml`): when `web_fetch` refuses a URL that is not
in the conversation, fetch it with `bash` where the environment allows, or
ask the person to paste it. The sealed-channel section of
`docs/architecture.md` should say that a closed environment also turns off
web search and fetch.

**Recommendation: go.** Small.

## 5. Fable 5.1 as a selectable agent model

**What.** Fable 5.1 is Anthropic's most capable model: $10 input, $50
output, $12.50 cache write, $0.25 cache read per million tokens. Managed
Agents accepts it.

**Gain.** A top-end choice for hard analysis work, at 2.5 times Opus 5.5's
price.

**Blast radius.** A row in `AGENT_MODEL_PRICING`, a display name in
`constants.py:MODEL_DISPLAY_NAMES` (a test requires both), and the
system-message model lists in `handoff_context.py:44-48` and
`testing/ma_sessions.py:757`, which name `claude-fable-5` today. The model
pickers on every platform read `ALLOWED_MODEL_IDS`, so it appears there
automatically. Fable 5.1 needs 30-day data retention. That is fine for the
hosted deployment but worth one line in `docs/self-hosting.md`.

**Recommendation: go.**

## 6. SDK upgrade to 1.x

**What.** Daimon pins `anthropic` 0.117.0. The current release is 1.12.1.
Version 1.0 moved the SDK from `httpx` to `httpx2`, and 1.2 reshaped the
Skills and Files APIs. A spike on branch `spike/anthropic-sdk-1x` (not for
merging) measured the cost on 2026-10-08.

**Gain.** Typed support for everything above that 0.117.0 lacks: session
`budget` and `budget_reached`, `refusal` with `stop_details`, the advisor
roster entry, the `auto` permission policy, `output_behavior` on dreams,
`inference_geo` on the model object, and `allowed_domains`/`blocked_domains`
on web tools. Without the upgrade, each of these needs raw strings or
`extra_body`.

**What breaks.**

| Measure | 0.117.0 | 1.12.1, no code changes | 1.12.1, after mechanical fixes |
| --- | --- | --- | --- |
| pyright errors | 0 | 539, in 35 production files | 170, all in Skills code |
| pytest | 3 failed, 14,818 passed | tests do not load | 163 failed (159 Skills, 3 also failing before) |

- **Boot.** The Skills rate-limit transport (`core/skills/rate_limit.py`) is
  an `httpx` transport, and 1.x rejects it. Every adapter except Teams would
  fail to start. Fixed in the spike by moving it to `httpx2`.
- **Silent.** `turn/driver.py` catches `httpx.RemoteProtocolError` and
  `httpx.ReadTimeout` to reconnect a dropped stream. The 1.x SDK raises the
  `httpx2` versions, so dropped streams would stop reconnecting and a stalled
  stream would crash the turn. pyright does not catch this, and the test fakes
  raised the old exceptions, so the tests would not either.
  `core/anthropic_spend.py` has the same problem. The spike fixed all three;
  the real change needs a regression test that stalls and drops a stream
  through the SDK.
- **Tests.** 64 test clients pass `httpx` clients to the SDK. The spike adds
  a small bridge in `daimon.testing`, so about 400 existing fake handlers keep
  working.
- **Skills (SDK 1.2), not ported.**
  - `display_title` becomes `display_name` and `latest_version` becomes
    `latest_version_id`.
  - Versions become `skver_` ids instead of timestamps.
  - `source` becomes an object, not a string. A plain rename would make the
    twelve `source == "custom"` checks always false, so skills would be
    misclassified silently.
  - `user_skills.anthropic_latest_version` and `channel_skills.version` store
    timestamp versions, and agent skill pins are written from them.
  - The port touches 14 production files, the fake Skills API and 46 test
    files. It needs a contract run to learn whether old timestamp pins are
    still accepted and whether stored rows need a migration.
- **Smaller changes**, all fixed in the spike: Files types renamed and
  cursor paging, tool configs typed per tool, a redacted agent-message block
  with no text (skipped), and a new `workspace_id` create parameter.
- **MCP tool schema.** The agent self-edit tools' schema grows by about
  4,000 lines, and agents could then send `auto` permission policies and web
  domain filters. Given item 10, the upgrade must keep `auto` out of what
  Daimon's tools accept.
- **Observability.** Sentry's automatic `httpx` instrumentation stops seeing
  Anthropic calls.
- `httpx` stays installed alongside `httpx2`, because `fastmcp`, `mcp`,
  `google-genai` and the Teams SDK depend on it.

**Recommendation: go, as its own piece of work, after item 1**, in this
order:

1. Port Skills, with a contract run and a decision on stored versions.
2. Make the `httpx2` boundary changes, with the stream regression test.
3. Restrict the MCP schema so agents cannot send `auto`.
4. Add Sentry instrumentation for `httpx2`.

Items 1, 2, 4 and 8 do not need the upgrade. Item 7 is cleaner with it.

## 7. Per-session budget cap

**What.** A session can be created with
`budget: {"type": "limit", "max_list_cost": {"amount": "<cents>", "currency": "USD"}}`.
Managed Agents stops the session between model requests once its list cost
(tokens at list price, web searches, and runtime) reaches the cap. The
session goes idle with `stop_reason: budget_reached`. It is not terminated.
At the cap, a `user.message` returns 400 and a `user.interrupt` is ignored.
Raising the cap resumes the session.

**Gain.** A cap the server enforces mid-turn, behind the channel budget,
which today is checked only before a turn starts (`channel_budget.py`). One
runaway turn could no longer overshoot it by much.

**Blast radius and traps.**

- Daimon reuses one session across a thread's turns, and a budget covers the
  whole session. A cap per turn means raising it with `sessions.update` before
  each turn. A budget can only be set at creation, and removing one is
  permanent.
- Units differ. The cap is list cost in cents, with no markup, and includes
  runtime and web searches. The channel budget is ledger dollars with markup,
  and tokens only.
- `send_interrupt_and_wait` (`ma.py:139-176`) would wait out its 120-second
  timeout on a session paused at its cap.
- The driver needs the `budget_reached` branch from item 1, and the person
  needs plain words when their turn stops for money.

Typed support needs SDK 0.121 or later; until then the field can go through
`extra_body`.

**Recommendation: go after items 1 and 6.**

## 8. Session runtime in the cost model

**What.** Managed Agents bills $0.08 per session-hour while a session is
`running`, on top of tokens. Daimon's ledger records tokens only.

**Size.** Production sessions ran 37 active hours in the 30 days to
2026-10-08, about $3. The session object already exposes
`stats.active_seconds`.

**Blast radius.** `usage_sweep.py` (a delta of `active_seconds` against a
stored per-session value), a new ledger reason in `usage_recording.py`, and
`docs/billing.md`.

**Recommendation: go, low priority.** Correct, but a few dollars a month at
current volume.

## 9. Advisor

**What.** An agent's `multiagent` roster can include
`{"type": "advisor", "model": "claude-opus-5-5"}`. The agent then consults a
stronger model mid-turn, billed at that model's rates, on a separate platform
thread.

**Gain.** A Sonnet 5.5 default agent that asks Opus 5.5 on hard steps, for
less than running Opus throughout.

**Blocker.** Daimon bills from the primary thread's `span.model_request_end`
events and prices them at the agent's model. The docs put advisor events on
the advisor thread and proxy only "critical events" to the primary thread. So
advisor spend would probably never reach the ledger. This is an inference
from the docs, not tested. Typed support needs SDK 0.121 or later.

**Recommendation: no-go** until a staging contract test shows advisor spend
reaching the ledger at the advisor's price.

## 10. `auto` permission policy

**What.** With `permission_policy: {"type": "auto"}`, the server judges each
tool call: run it, deny it, or pause for approval.

**Why not.** Daimon's own policy is deterministic and operator-configured
(`tool_safety.py`: allow, ask or deny, with deny lists for unattended
writes). `auto` would hand that decision to the server and remove the human
confirmation card for writes. The docs themselves say `auto` "is not a human
checkpoint".

**Recommendation: no-go.**

## 11. `inference_geo`

**What.** Pins an agent's inference to the US, at 1.1 times all token
prices.

**Why not now.** No tenant has asked for data residency. It would also need
the 1.1 multiplier in `pricing.py`, the reconcile hash and the session
identity, or a pinned agent would be billed 10% under.

**Recommendation: no-go until a tenant needs it.**

## 12. Fast mode

**What.** `model: {"id": "claude-opus-5-5", "speed": "fast"}` gives faster
output at twice the price. Research preview, Opus only.

**Why not now.** `cost_of` ignores `usage.speed`, so fast turns would be
billed at half price. A speed change is not a session identity field, so it
would not take effect on live threads. Operators cannot set it today: the
agent tools take a model id string.

**Recommendation: no-go for now.** If wanted later, pricing comes first.
