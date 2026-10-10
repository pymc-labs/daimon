Gemini smoke price review, 2026-10-10 (USD per million tokens, Standard tier).

| Requested model | Input | Cached input | Output including thoughts | Verification |
| --- | ---: | ---: | ---: | --- |
| gemini-3.8-flash | 0.75 | 0.075 | 3.75 | Published current rate, through 2026-12-31 |
| gemini-flash-latest | 1.50 | 0.15 | 7.50 | Conservative bound; resolved model unverified |
| gemini-3.5-flash-lite | 0.30 | 0.03 | 2.50 | Published current rate |

Source: [official Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing).
The primary rates double on 2027-01-01. Re-review before a later run; do not
reuse this dated table across that price change.

The [official model alias documentation](https://ai.google.dev/gemini-api/docs/models#latest)
says latest aliases change with releases. No independent alias tariff or resolved
version is claimed here. Its bound uses the published undiscounted Standard
Flash rates. Alias usage is recorded, but its canonical receipt keeps the full
reservation and reports estimated, unverified until N9 verifies the resolved
price. This bound is an inference for admission, not an exact settled charge.

The smoke never creates explicit cached content or enables grounding. Its
cache-write count is therefore zero; the configuration's required cache-write
rate conservatively equals ordinary input. Cache storage, search/maps and other
tool charges cannot be certified by this smoke. Code-execution compute is
currently unbilled in preview per the pricing page; no compute is requested here.

Every HTTP response records only status, requested model and the four nullable
usage counters. Interactions supplies `usage.total_input_tokens`,
`total_output_tokens`, `total_cached_tokens`, `total_thought_tokens`; these are
explicitly labelled when projected into the usageMetadata-shaped receipt.
Native usageMetadata is also supported without replacing missing values with
zero. See [the official Interactions reference](https://ai.google.dev/api/interactions-api).
Only the latest cumulative interaction snapshot enters N9's `settle()`, with
thoughts added once to visible output and cached reads subtracted from input.
Repeated polling snapshots are never summed. Unknown usage retains a hold.
