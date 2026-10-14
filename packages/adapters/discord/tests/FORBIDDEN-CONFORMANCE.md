# Offline Discord Forbidden observation

`forbidden_hook.DiscordForbiddenHook` is explicit test support. No application
imports it. The runner supplies an owned, fresh `DaimonBot`, a
`ForbiddenGateway`, and the `ScriptedTransport` bound to its isolated Anthropic
SDK. The shared database guard validates the primary and all routed binds before
connection, including the registered isolated-CI exception.

The bound `discord_overwrite_edge` accepts N6's original operation only:
`{do: admin, tool: discord_overwrite, args: {target: daimon_bot,
deny: [CREATE_PUBLIC_THREADS]}}`. Its explicit tenant/account scope must match
real database rows, the owned Discord guild/parent, and an actually configured
channel backend. It arms an offline HTTP edge; it never changes live Discord
permissions. Configuration and mention are single use.

Call `mention(real_message)` after binding. It invokes normal `on_message`
ingress, admission and orchestration. Actual `discord.py` thread creation raises
`discord.Forbidden` (403, 50013), and the adapter's existing error boundary sends
its parent reply through actual SDK serialization. The receipt contains that
creation invocation, independently captured parent posts, backend revision,
monotonic clocks, real native HTTP method/path trace, and queue cleanup state.
The hook refuses missing creation, foreign destinations and unbound traces.
The runner must disable its own Sentry transport and pass the shared offline
runtime validator, without replacing product admission.

`evaluate_new14` applies the original NEW14 text/deadline predicates and checks
zero provider turn writes. The exact host thread-naming request
`HOST_THREAD_NAMING_REQUEST = ("POST", "/v1/messages")` is the sole named
exception; it remains visible in the unfiltered native request trace. All other
non-read requests still count as writes. The frozen source is
`QA-NEW14-MISSING-THREAD-PERMISSION-COPY.yaml`, SHA-256
`75a44495ca1c2943c80a111e698a15bf47b440bf4b7c0acd03c5e47e1b30ab43`.
The existing notice is `Couldn't open a thread. @mention Daimon again.`. All
three configured backends therefore yield an honest offline FAIL: timely notice
and zero provider turn sends pass (one host naming POST is retained), while the
retry-text absence and permission-name
presence predicates fail. This unit does not change the notice.

Mutation controls remove production ingress or the error notice, correct only
the test's production-copy constant, and add an actual SDK write. Their results
track the observed path and wire data. Authorization, source-shape and DB routing
controls fail closed. There are no provider session/root IDs, synthesized turns,
live permission assertions or automatic matrix credit. N6/N9 must bind this edge
and independently join its receipts into their existing admin/capture runner.


The regression fixture uses the product-default `ThreadNamingSettings()`:
naming is enabled, with all its default limits and timeout. Its real host title
call traverses the isolated installed SDK to an owned scripted reply. The fake
Discord HTTP client accepts actual `channel.typing()` and records the typing
channel. A regression asserts the naming POST is present, its returned title is
used for the rejected creation, and only this request is excluded from the
turn-write predicate. The non-host write mutation still fails the predicate.

Remaining deliberate fixture configuration: `thread_open_notice_after_s=0`
(product default 3 seconds) makes the opening ⌛ reaction immediate instead of
delaying that acknowledgment. It preserves admission and native naming. This is
an offline timing override, not proof of live permission settings or
production latency. `TurnSettings(path="mux", channel_backends=True)` explicitly
enables the three backend configurations being tested; it does not represent an
unconfigured channel. Fixture Anthropic/Discord keys and Settings sources are
owned placeholders with environment/dotenv sources disabled. The Settings DSN
is a neutral unused placeholder; connections use the separately supplied,
validated owned sessionmaker. Agents, environments, tenant/account/channel and
trial credit are owned fixture rows. Billing configuration is absent, Sentry is
disabled, and the SDK uses the shared isolated scripted transport. The shared runtime
validator requires factory/backend/backend-session-ref/state-store absence and
an ordinary empty runtime mapping. No opaque native construction is admitted. These fixtures make no provider call or spend
claim. Ownership failures skip only a plain local contributor `daimon_test`;
foreign/query/routed and unregistered-CI boundaries remain hard refusals.


C4 and C7 use `daimon.testing.discord_runtime_capture` for the same current
outer/nested SDK and database admission. C4 runs it before construction, overwrite
reads and real mention dispatch, then again after each awaited observation before
arming or final database readback. Frozen replacement bundles cannot inherit an
old capture authorization. Negatives cover every reachable native/DB field,
including the genuine default factory, captured-vs-unbound SDK clients and a
real foreign PostgresStateStore with a connection trap.

The shared pytest11 `db_preflight` plugin uses the owned-engine/isolated-CI guard
before any DB fixture can connect. Its cache key follows the requesting
fixture's resolved schema; plain contributor databases skip and foreign targets
refuse. Two child-pytest controls trap `AsyncEngine.connect`/`begin` in the actual
Forbidden fixture graph, so fixture-order errors cannot be hidden by a later
runtime check. The temporary local autouse guard is retired. No product error copy,
thread naming request or NEW14 source predicate changes in this delta.
