# Offline SDK boundary proofs

`conformance_sdk.offline_sdk_adapter()` is an explicit async context manager.
It constructs the pinned OpenAI SDK against a fixed `httpx.MockTransport`, with
an offline credential, zero retries, no redirects and no environment routing.
It has no live credential, endpoint or transport configuration. The optional
`sdk_factory` receives the adapter's fixed mock transport, credential and URL.
Each use
owns a fresh native script, driver and memory store; closing it closes the SDK
and all scripted streams. It does not alter the standard registry or host
dispatch. Saved native sessions name `gpt-6-luna`.

Before constructing the script, HTTP client or SDK, the adapter refuses the
presence of `OPENAI_ADMIN_KEY`, `OPENAI_WEBHOOK_SECRET`, `OPENAI_ORG_ID`,
`OPENAI_PROJECT_ID` or `OPENAI_CUSTOM_HEADERS`, including empty values. The
pinned SDK otherwise inherits these settings even with an explicit API key;
`None` triggers fallback and an empty header map still merges environment
headers. Refusal reports no values and leaves the environment untouched.
Driver reconstruction repeats this preflight before `SDKTransport.with_options`
can construct a copied SDK client, so newly inherited settings also refuse.
The standard conformance factory, C04 arrangement, F1 replay factory and C11
deployment fixture use the same preflight. Host tests inject
`mux.testing.isolated_sdk.build_async_openai` into the conformance/component
and F1 factories; C11 and direct SDK test clients use that shared helper too.
It refuses all named provider settings, including empty values, at initial
construction and every SDK copy. Expected-success tests clean those names with
a scoped pytest fixture; factories never erase the process environment.
Dedicated fallback regressions still exercise `offline_sdk.OfflineAsyncOpenAI`.
Mock HTTP clients explicitly disable environment
proxy discovery and redirects. These changes affect offline construction only;
the production transport and native request bodies remain unchanged.
Standalone mux keeps its guarded local default because mux cannot import
`daimon.testing`. `SDKFactory` is a structural injection seam, so importing
the driver does not import host testing or change package dependencies.
Fixed offline credentials, URL and transport plus credential/account admission
prevent inherited account, routing or proxy configuration.

```python
from mux.testing.conformance.runner import run_fixture
from mux.testing.drivers.openai.conformance_sdk import offline_sdk_adapter
from mux.testing.isolated_sdk import build_async_openai

async with offline_sdk_adapter(sdk_factory=build_async_openai) as adapter:
    result = await run_fixture("C06", adapter)
```

| Case | Component coverage | Limit |
| --- | --- | --- |
| C06 | Unchanged shared cancel/occupancy oracle over actual SDK POST/GET; EOF and requested cancel retain the root, saved cancelled evidence releases it once | Controlled native responses; no live cancellation certificate |
| C07 | Four separate SDK GET snapshots pass through the production usage normalizer/revision allocator, then the memory ledger/outbox and restart oracle | Synthetic null→100→120→110 correction; no provider-final price, settlement or durable database claim |
| C08 | Actual changed custom-tool and file-mount specifications; plan/apply refuse, zero update writes, unchanged effective revision/session | Full required-mount replacement remains typed PENDING; repository workspace sources also refuse before I/O |
| C09 | Unchanged shared paginated artifact/download/deletion oracle, exact binary bytes, typed interruption and independently observed retention of the shared vault | Scripted native catalogue/content; no new live artifact/vault certification |

Only C06/C07/C09 execute in this component. All other cases are declared typed
PENDING before any request. C08 is a capability refusal proof, not a full C08
PASS. The standard `openai.offline` adapter and historical live tapes remain
unchanged.

The C07 sampler performs four calls to the actual usage port, which assigns
revisions as each native meter changes. It neither places four duplicate turn
IDs into one provider page nor writes counts directly into the ledger. Driver
reconstruction retains the owned revision allocator and native SDK; unchanged
meter reads preserve revision 4. Replayed/stale observations cannot add outbox
rows. The shared oracle also proves a higher same-count revision, one-time
outbox application and a final 110-unit balance. This is token-unit accounting
evidence, not a dollar receipt or an exact-spend certificate.

Mutation controls fail the shared oracles for an unobserved cancellation,
missing revision allocation, truncated binary output and deletion of the
shared vault despite a receipt that claims retention. They inspect actual
driver outputs and retained HTTP effects, not adapter-supplied verdicts.

These scripts use the same Agents resource, `OpenAI-Beta: agents=v1`, native
turn/item and artifact shapes documented in the driver's pinned reference.
Full C12 additionally remains capability PENDING: neutral immutable-disjoint
usage coverage excludes simultaneous overlapping turn/session aggregates.
Partial host accounting proofs cannot certify that full contract. C14/C17/C18
require separate provider-selection, wake-generation and final-outcome host
envelopes. No host result is fabricated by this provider component; `mux` does
not import Daimon.
