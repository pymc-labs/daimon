"""Gemini managed agent with inline environment reuse.

Not core: the inline environment expires after inactivity, so a thread's
workspace is not guaranteed to persist. Expiry surfaces as `ContinuityLost`,
never as a silent fresh start, and a channel has to name this profile to get
it.
"""

from __future__ import annotations

from mux.contracts.profile import Profile

INLINE_REUSE = Profile(
    provider="gemini",
    profile_id="gemini.inline_reuse",
    schema_version="1",
    sdk_pin="google-genai>=2.7.0,<3",
    core=False,
    support={
        "thread_workspace_persistence": "unsupported",
        "turn_lifecycle": "native",
        "cancel": "native",
        "tool_loop": "native",
        "skills_bundle": "native",
        # Stored interactions can be re-read; lossless delta replay is unknown.
        "reconcile": "emulated",
        "event_previews": "native",
        "multiagent": "unsupported",
        "native_schedules": "native",
    },
)
