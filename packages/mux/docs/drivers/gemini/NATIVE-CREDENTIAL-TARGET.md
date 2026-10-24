# Native credential target offers (explicit operator capability)

`GeminiNativeCredentialAdapter` in `daimon.core.turn.gemini_credential_requests`
offers the shared GitHub connection card or private environment-key card for an
independently read-back Gemini named agent. It defaults to disabled and requires
an explicit `NativeCredentialDelivery`. A separately selected native MCP endpoint
can opt in through `register_gemini_credential_tools`; ordinary servers and their
legacy tools are unchanged. Colliding tool names refuse registration.

The authorized execution grant carries a full `resource_scope` independently of
the returned host. Tenant, resource account, principal, authorization, workspace,
provider, caller-private binding account, session ownership and target identity are checked before any database or
provider read. The requesting account can differ from the resource owner. An
active caller/place row, current policy, retained backend revision, durable
provider binding and catalog CAS revision are checked again after native GET and
after native registry registration, before any platform post. Legacy MA identity
fields never identify the target. External and MA-scoped credentials are refused.

The registry retains a scoped host control identity (`nc_gemini_input:`) and the
complete origin, target, requester and TTL before the delivery edge posts. It is
not a Gemini credential-request API. Delivery must install its native dispatcher
before acknowledging registration and return the actual posted message identity;
it must never route the control through an Anthropic resolver. The supplied
registry/delivery owns these obligations; no existing runtime installs it yet.

`origin.session` identifies a scoped mux session. Gemini uses interactions rather
than a native Sessions API; this adapter does not manufacture a provider session
or prove a named-agent session plan. Production execution must attest the binding
and target together before granting an origin. A1 currently refuses named-agent
interaction sessions, so this unit's native target/offer proof does not promote an
end-to-end conformance cell.

Offline proofs use google-genai Agents POST plus independent GET through owned
MockTransport, actual isolated Postgres rows, the shared card builders and real
discord.py serialization. The GitHub embed title and description are asserted in
both direct and authenticated MCP offer branches. Environment names pass the
existing reserved-name policy. No secret value is an MCP argument.

| Cell | Result | Evidence and remaining dependency |
| --- | --- | --- |
| D8-GITHUB-CONNECT-OFFERED / Gemini | PENDING | Native target GET, fresh origin/policy/binding and shared Discord offer components PASS. Original channel-copy scenario, production execution grant, native registry/dispatcher and runner binding remain unbound. |
| NEW21-FORM-EXPIRY-UPDATES-CARD / Gemini | PENDING | TTL is retained, but no expiry worker or timed card edit is implemented or claimed. |
| NEW27-MISTYPED-TOKEN / Gemini | DEFERRED | Credential submission, consumption, probing and their order are unchanged. |

No OAuth, credential submit, consume/probe, provider credential writes, runtime
fallback, expiry job, key access or live execution is added. Anthropic files,
shared request handlers, shared card copy and immutable goldens remain unchanged.
