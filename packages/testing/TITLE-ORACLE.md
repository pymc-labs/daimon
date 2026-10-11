# Offline title oracle

`daimon.testing.title_oracle.evaluate_title(pack, catalog_root=..., scenario_id=...,
backend=..., trigger_message_id=..., receipt=...)` consumes the verified frozen
TARGET-53 pack and the real C7 `DiscordIngressReceipt`. The sole authored mention
binds the source text; context messages do not renumber the authored assertion.
The returned `assertion` preserves every frozen YAML field and regex escape.

A title facet requires the actual gateway trigger, dispatch, channel backend,
thread-create request and response, naming HTTP request, and per-trigger usage
meter. IDs, parent/guild, author, model, message text and observed creation order
must agree. Missing, ambiguous, foreign or malformed captures report PENDING.
The predicate checks the actual created name against the authored length and
forbidden pattern. A proven violation reports FAIL. Re-evaluating a receipt after
JSON serialization applies the same checks; a declared title is insufficient.

The C7 title response is an authored offline SDK response, consumed through the
real production naming request and Discord thread creation. These controls prove
host title wiring and predicate execution; they do not certify model-generated
title quality, native provider completion or another assertion family. The result
always retains `whole_cell_status=PENDING` because the other frozen assertions
and their final-wait binding are outside this title-only capture. No TurnEvidence
or native roots are manufactured, and no runner dispatch is enabled implicitly.

Current source captures configure Anthropic, OpenAI and Gemini channels separately.
NEW8 and NEW9 have a real naming request and satisfy their title facets on all
three. I6 uses the actual production fallback `Chat with qa-agent`, with no naming
request or meter, and therefore remains PENDING under the real-request rule.
All nine whole cells stay PENDING; the six title-facet passes earn no completed
cell credit. The NEW8 URL is input data for the naming request; no URL fetch or
live service is involved. A future catalog runner must supply its own remaining
assertion-time and completion evidence before it can award whole-cell PASS.
