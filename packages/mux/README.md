# mux

The provider-neutral managed-agent contract: types, port protocols, errors,
profiles and admission, the `StateStore` protocol and its in-memory test
store (`mux.state`), with one driver package per provider under
`mux/drivers/`. Daimon wraps its existing Anthropic client with resource
ports while retaining host policy, authorization, locking and client lifetime.
Turn and event ports remain independently injectable in the driver factory.

The import name is the top-level `mux`, not `daimon.mux`, so it can be split
into its own repository without a rename. `mux` never imports `daimon`, and
only `mux.drivers.<provider>` imports that provider's SDK; import-linter
contracts in the root `pyproject.toml` enforce both.

Install a driver's SDK with its extra: `daimon-mux[anthropic]`,
`daimon-mux[openai]` or `daimon-mux[gemini]`.

The full description is [docs/mux.md](../../docs/mux.md). Tests:
`uv run pytest packages/mux`.
