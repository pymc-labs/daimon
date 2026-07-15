"""Tests for the credential-guidance system preamble.

The preamble tells every agent WHERE its credentials live (env-secret file vs
MA-vault-bound MCP auth) so it stops hallucinating "no key" / hunting for
non-existent MCP keys. It is sentinel-delimited so re-applying replaces the
block instead of stacking — reconcile re-runs and panel edits must be
idempotent or the spec hash never stabilises.
"""

from __future__ import annotations

from daimon.core.agent_guidance import (
    CREDENTIAL_GUIDANCE_BLOCK,
    apply_credential_guidance,
)


def test_prepends_block_when_absent() -> None:
    out = apply_credential_guidance("You are daimon. Be concise.")
    assert CREDENTIAL_GUIDANCE_BLOCK in out, "guidance block must be present after applying"
    assert out.endswith("You are daimon. Be concise."), (
        "original system must be preserved below the block"
    )
    assert "/mnt/session/uploads/.env" in out, "must tell the agent where env secrets are mounted"
    assert "MCP" in out, "must explain MCP vault-bound auth"


def test_idempotent_applied_twice_equals_once() -> None:
    once = apply_credential_guidance("base prompt")
    twice = apply_credential_guidance(once)
    assert twice == once, "re-applying must replace the block, never stack it"


def test_empty_system_yields_only_block() -> None:
    out = apply_credential_guidance("")
    assert CREDENTIAL_GUIDANCE_BLOCK in out, "empty system still gets the guidance block"


def test_carves_out_redacted_self_inspection() -> None:
    # Regression for the QA trace where an agent refused a values-redacted
    # request to confirm whether a key was present, calling it
    # "credential harvesting". The boundary the block must draw is on the
    # secret VALUE leaking, not on reading config at all.
    block = CREDENTIAL_GUIDANCE_BLOCK
    assert "VALUE" in block, "block must distinguish a secret's value from its existence"
    assert "credential harvesting" in block, (
        "block must explicitly defuse the 'credential harvesting' over-refusal"
    )
    assert "REDACTED" in block, "block must endorse value-redacted inspection as safe"


def test_replaces_stale_block_preserving_user_body() -> None:
    seeded = apply_credential_guidance("ORIGINAL BODY")
    # Simulate a user editing only their body underneath the (now stale) block.
    edited = seeded.replace("ORIGINAL BODY", "EDITED BODY")
    reapplied = apply_credential_guidance(edited)
    assert reapplied.count("/mnt/session/uploads/.env") == seeded.count(
        "/mnt/session/uploads/.env"
    ), "must not duplicate the block when one already exists"
    assert reapplied.endswith("EDITED BODY"), "user's edited body must be preserved"
