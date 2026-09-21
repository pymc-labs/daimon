# mux

`packages/mux/` is a scaffold. Read this page before you assume anything in
it runs.

## What it is for

daimon runs on Anthropic Managed Agents, and `daimon.core` calls the
Anthropic SDK directly. `mux` stakes out what a provider-agnostic version of
that interface would look like: a shared vocabulary for describing what a
"managed agent" backend can do, and one normalized event shape, so a caller
can branch on declared capabilities instead of assuming every backend behaves
like Anthropic.

The distribution is `daimon-mux` but the import name is the top-level `mux`,
not `daimon.mux`. `packages/mux/mux/__init__.py` says why: outside teams are
meant to implement against it, so the name has to survive being split out
into its own repository without a rename.

## What is actually in it

Five modules, plus one file per backend.

| Module | Contents |
| --- | --- |
| `packages/mux/mux/capabilities.py` | `BackendId`, `BACKEND_IDS`, and the frozen `Capabilities` model: `can_steer`, `can_schedule`, `durable_fs`, `self_hosted_sandbox` |
| `packages/mux/mux/core_profile.py` | `FLOOR = ("durable_fs",)` and the pure `missing_capabilities(caps)` |
| `packages/mux/mux/events.py` | `MuxEventKind` (`text`, `tool_use`, `tool_result`, `done`, `error`) and the frozen `MuxEvent` |
| `packages/mux/mux/errors.py` | `MuxError` and `CapabilityUnavailableError` |
| `packages/mux/mux/backends/` | `BACKENDS`, a dict of three `Capabilities` records |

That is the whole public surface — the `__all__` in
`packages/mux/mux/__init__.py` lists ten names. There is no `Protocol`, no
abstract base class, no client object, and no function that calls a provider.
It is a data model, not a driver.

## The backends

The three modules under `packages/mux/mux/backends/` each declare one
`Capabilities` record and a `BACKEND_ID` constant. None of them
makes an API call; none of them contains a stub raising
`NotImplementedError`, because there is no method to stub. The flag values
are self-declared starting positions — `packages/mux/mux/capabilities.py`
says the adjudicating cross-backend conformance suite is what would settle
them, and that suite does not exist in this repository.
`packages/mux/mux/backends/google.py` carries `durable_fs=False` explicitly
pending emulation work that has not landed.

## Not wired into the turn path

Nothing in `daimon` imports `mux`. It is not listed in the root
`pyproject.toml`'s `[project].dependencies`, so it is not installed alongside
the services; the `[tool.uv.sources]` entry for `daimon-mux` is a resolution
hint for anything that does depend on it, not a dependency itself. A turn
goes `daimon.core.turn` → the Anthropic SDK, with no mux in the chain. See
[architecture.md](architecture.md).

It is still a first-class member of the workspace: pyright checks it in
strict mode, its eleven tests run in CI, and `uv run lint-imports` holds this
contract from the root `pyproject.toml`:

```toml
[[tool.importlinter.contracts]]
name = "Mux must not import daimon"
type = "forbidden"
source_modules = ["mux"]
forbidden_modules = ["daimon"]
```

The direction matters. `mux` may never import `daimon` — that is what keeps a
future extraction a plain `git subtree split`. The reverse is deliberately
allowed and there is no contract against it, so `daimon` could consume `mux`
at any point. It does not yet.

## If you want to work on it

The honest gap is everything between the capability record and a call:
a backend protocol, an implementation per backend, a translation from each
provider's stream into `MuxEvent`, and the conformance suite that would turn
the declared flags into measured ones. Until at least the first two exist,
treat a change here as changing a design document that happens to type-check.
