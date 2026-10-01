"""Google backend: the floor.

Ephemeral environments forked fresh per invocation, Flash-only models, no
durable session. Gemini sets the floor the abstraction cannot dip below — and
drags it down for everyone, which is exactly the argument the draft spec
needs to have in the open. `durable_fs` reads False until the emulation
(workspace persist plus re-fork per turn) lands; that emulation is what flips
this flag, and flipping it is Track A's proof of work.
"""

from __future__ import annotations

from mux.capabilities import Capabilities

BACKEND_ID = "google"

CAPABILITIES = Capabilities(
    can_steer=False,
    can_schedule=False,
    durable_fs=False,
    self_hosted_sandbox=False,
)
