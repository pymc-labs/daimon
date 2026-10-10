Gemini's supplementary F1 adapter is explicitly selected through
`mux.drivers.gemini.default_capability.adapter`. Pass it to the shared
`run_default_capability` entry, or as the fresh factory for
`replay_default_capability`. It constructs only an offline driver and in-memory
resources; no key discovery, SDK client or live invocation is installed.

F1 currently returns typed `ADAPTER_DEPENDENCY` before provisioning. The full
authored default includes the complete `pymc-artifact-style` skill, with binary
fonts/images and a bundle larger than the driver's text-only 2 MiB limit.
The other ten complete default skill bundles are feasible offline. A text-only
subset does not establish equivalence to all eleven complete bundles.

The Gemini API supports remote MCP over streamable HTTP, with optional headers
(`source-G-runtime.txt:587-613`), and filesystem-native skills under
`.agents/skills/` (`source-G-runtime.txt:751-752`). The driver's public MCP mapping
works, but authenticated daimon-mcp requires a credential resolver the driver
does not provide. The shared F1 session also needs the driver's required explicit
binding/thread extension. All six logical builtins route through native
`code_execution` (`source-G-runtime.txt:231,242-243`); native filesystem tool
identities are not modelled by the driver. The remaining gaps concern the
adapters for complete skills, authenticated MCP and session binding.

No upstream deployment evidence or successful default turn is fabricated.
Removing the pending declaration cannot pass the current full scenario.
A component test scripts the real driver with native code-execution calls and
results, records normalized events and replays all six builtin routes twice.
Dropping any builtin call fails the turn check. This component supplies no
whole-default deployment evidence; full F1 stays typed PENDING. Incomplete replay
tapes fail. F1 is separate from C01–C18; the existing Gemini
matrix remains eight PASS and ten typed PENDING. No full live certificate exists.

Future live F2 requires an explicit lead GO, the pinned key file and reviewed
budget rates. Only `gemini-3.5-flash-lite` may be selected; key appearance alone
does not authorize a call. This adapter ships no live path.
