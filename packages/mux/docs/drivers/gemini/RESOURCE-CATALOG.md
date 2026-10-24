# Explicit native resource catalog adapter

`GeminiManagedAgents` has an operator capability, `resource_catalog_enabled`,
which defaults to `False`. Off retains the existing inline agent port and
`catalog_adapter=None`, even if a catalog transport is supplied. On requires an
explicit `AgentCatalogTransport` and exposes the same `GeminiCatalogAdapter`
through `agents` and `catalog_adapter`. Neither branch discovers credentials,
constructs clients or selects a host factory. The private SDK transport consumes
an already owned client, with native SDK retries disabled.

The adapter compiles the documented native `agents.create` definition and uses
`agents.get` for independent readback. It retains the actual project-native ID;
local revisions are host intent, with `Revision.native=None` because Gemini does
not expose agent versioning or provider CAS. Every create has an explicit Flash
model, tools list (including an empty list), system/description when authored,
and the complete skill mounts. No implicit paid tools are enabled. Host names
and metadata remain authored fields, rather than invented provider metadata.

Skill versions must be concrete content hashes, not `latest`. Before creation
and readback, every bundle is checked against its retained hash, scope and
workspace; GCS receipts retain their existing manifest validation. Copies use
distinct owned skill IDs while retaining all original bytes. Publishing a new
version cannot change an old agent's pin. Session credential references refuse
before a native POST: per-session auth cannot be stored in a persistent agent.

A creation intent is durably fenced before the single native POST. Its ID is
bound to tenant, account, principal, workspace and operation key. A lost POST
response can only reconcile by GET after restart; the adapter never resends it
or fabricates an accepted resource. Missing/mismatched readback retains the
intent without admitting an agent. Independent GET must match model, tools,
mounts, base agent, system and description. Sparse acknowledgements and nullable
optional metadata are accepted only when the subsequent GET proves the complete
definition. `verify(scope, retained_agent)` additionally checks a separate host
catalog row against the driver's authored spec and revision.

The host's `PostgresNeutralCatalog(provider="gemini")` remains the CAS and
revision-history authority. The offline proof exercises actual PostgreSQL CAS,
old native IDs/pins, foreign-account refusal and independent native drift. CAS
does not become a fictional provider revision or an atomic native GET/write.
Failed publication retains the replacement for explicit reconciliation.

The owner-approved `NeutralResourceHost` edge additionally requires
`resource_catalog_enabled=True` and an explicitly supplied `catalog_adapter`
that is the backend's actual agent port, enabled and exactly Gemini/profile/
workspace matched. It uses the real Gemini catalog and the public verify edge.
The existing Gemini guard remains effective when either capability is off or
any identity differs; tenant/account checks precede native reads. Existing
OpenAI/Anthropic branches are preserved. This slice does not install CLI
routing. Native named agents also refuse the
existing inline session plan; no inline clone substitutes for a native agent.
Native update/archive/delete and named-agent turn execution remain typed
unsupported. D14-FORK-KEEPS-SKILLS and D5-UNBOUND-AGENT-EDIT-REFUSED remain whole
cell PENDING until their original host routes, assertions and cleanup run. In
particular, the frozen D5 Anthropic model is never translated to Gemini.

API evidence: the supplied official `source-G-agent.txt` snapshot, lines
400–850 (creation), 925–1010 (GET/delete) and 1015–1069 (definition and no
versioning), and the pinned `google-genai` 2.7.0 native agents SDK. Tests exercise
the real SDK over `httpx.MockTransport`, including request-body bytes; no key or
live interaction is used. The catalog database proof requires an explicit owned
test DSN and honors the shared registered isolated-CI exception; database-free
mux CI and plain contributor databases skip that PostgreSQL-only proof.
