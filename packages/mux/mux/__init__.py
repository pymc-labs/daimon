"""Mux: provider-agnostic managed-agent interface.

The contract lives in `mux.contracts`, the declared provider profiles in
`mux.profiles`, and the provider drivers in `mux.drivers`. The top-level
import is deliberately `mux`, not `daimon.mux`: outside teams are meant to
implement against this, so the name must survive a spin-out to its own repo
with zero renames.

Dependency direction is one-way: `daimon` may consume `mux`, `mux` never
imports `daimon` (enforced by an import-linter contract). That keeps a future
extraction to a `git subtree split` with no code motion.
"""
