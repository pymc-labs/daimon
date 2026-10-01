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


def test_block_names_no_setup_path() -> None:
    # The preamble must not name a nested panel path.
    assert "/agent-setup" not in CREDENTIAL_GUIDANCE_BLOCK, (
        "block must not name a setup panel path — the preamble's reader is the agent"
    )


def test_replaces_old_style_block_not_stacks() -> None:
    # A system carrying a stale sentinel-wrapped block (e.g. from before a
    # preamble rewrite) must be replaced by the current block, never stacked
    # alongside it — this is what makes a wording change safe to deploy over
    # live agents.
    old_block = (
        "<!-- daimon:credential-guidance v1 -->\nSOME OLD STALE BODY\n"
        "<!-- /daimon:credential-guidance -->"
    )
    system_with_old_block = f"{old_block}\n\nORIGINAL BODY"
    out = apply_credential_guidance(system_with_old_block)
    assert out.count("<!-- daimon:credential-guidance v1 -->") == 1, (
        "must not stack an old-style block alongside the current one"
    )
    assert "SOME OLD STALE BODY" not in out, "stale body must be replaced, not preserved"
    assert CREDENTIAL_GUIDANCE_BLOCK in out, "current block must be present"
    assert out.endswith("ORIGINAL BODY"), "user's own body must survive the replacement"


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


def test_states_the_interactive_delivery_contract_not_only_the_headless_one() -> None:
    # Regression for the staging trace where an agent answered one mention
    # twice: it called send_message into the thread it was already running in,
    # and its ordinary reply was delivered too. The block previously described
    # only the headless-routine path ("a routine must call send_message"), so
    # that was the sole statement about how output reaches Discord and a
    # weakly-primed agent generalised it to interactive turns.
    block = CREDENTIAL_GUIDANCE_BLOCK
    assert "send_message" in block, "block must name the tool the failure mode misuses"
    assert "ROUTINES RUN HEADLESS" in block, "the headless-routine path must still be stated"
    assert block.index("A CHAT REPLY DELIVERS ITSELF") < block.index("ROUTINES RUN HEADLESS"), (
        "the interactive contract must precede the routine exception, so the "
        "default case is read first and the exception reads as an exception"
    )
    assert "Never" in block and "invoked from" in block, (
        "block must forbid send_message into the thread the turn was invoked from"
    )
    assert "interactive exception is Discord file delivery" in block, (
        "the Discord file path must be explicit about overriding the general send_message rule"
    )
    slack = block[block.index("On Slack") : block.index("On Discord")]
    discord = block[block.index("On Discord") : block.index("Calling `read`")]
    assert "/mnt/session/outputs IS the delivery path" in slack
    assert "do NOT also" in slack and "send_message" in slack, (
        "Slack guidance must prevent output-directory files from being sent twice"
    )
    assert "/mnt/session/outputs is NOT a delivery path" in discord
    assert "create_file_upload_url" in discord and "send_message" in discord, (
        "Discord guidance must preserve its explicit file-upload sequence"
    )
    assert "a FILE never does" not in block
    assert "There is no way to attach a file to your reply" not in block


def test_states_setup_reads_at_turn_start_not_mid_turn() -> None:
    # Regression for task-continuity: an agent must not claim a key is
    # missing, or that a workspace change did nothing, using knowledge that
    # was only true earlier in a long-running turn. Setup (keys, MCP,
    # model, instructions, skills, working repo) is fixed at turn start and
    # can differ on the next one.
    block = CREDENTIAL_GUIDANCE_BLOCK
    assert "YOUR SETUP IS READ AT THE START OF A TURN, NOT DURING ONE" in block, (
        "block must state setup is turn-start-fixed, not live during the turn"
    )
    assert "Re-read /mnt/session/uploads/.env at the start of a turn" in block, (
        "block must tell the agent to re-read the mounted env file at turn start"
    )
    assert "files survive a workspace change, running processes do not" in block, (
        "block must distinguish surviving files from a process/kernel/shell that does not"
    )
    keys_index = block.index("1) KEYS")
    setup_index = block.index("YOUR SETUP IS READ AT THE START OF A TURN")
    mcp_index = block.index("2) MCP SERVERS")
    assert keys_index < setup_index < mcp_index, (
        "the turn-start note must sit right after the keys section and before MCP servers"
    )


def test_setup_read_note_survives_idempotent_reapplication() -> None:
    out = apply_credential_guidance("base prompt")
    twice = apply_credential_guidance(out)
    assert out.count("YOUR SETUP IS READ AT THE START OF A TURN, NOT DURING ONE") == 1
    assert twice.count("YOUR SETUP IS READ AT THE START OF A TURN, NOT DURING ONE") == 1
    assert twice == out, "re-applying must not duplicate the turn-start note"


def test_slack_guidance_states_output_write_discipline() -> None:
    # The Slack outputs directory is snapshot-indexed by Managed Agents: the
    # content is captured at first write, subdirectory paths flatten, and an
    # rm-and-recreate removes the file from the listing entirely. Each of those
    # facts needs its own line of guidance or agents will append, nest, and
    # delete their way into silently undelivered files.
    block = CREDENTIAL_GUIDANCE_BLOCK
    slack = block[block.index("On Slack") : block.index("On Discord")]
    assert "interactive turns only" in slack, (
        "Slack guidance must scope delivery to interactive turns"
    )
    assert "a scheduled routine delivers nothing" in slack, (
        "Slack guidance must say routines deliver nothing this way"
    )
    assert "once and complete" in slack and "never re-indexed" in slack, (
        "Slack guidance must warn that appends after the first write are lost"
    )
    assert "flat, unique filenames" in slack and "flattened away" in slack, (
        "Slack guidance must warn that subdirectories flatten and names collide"
    )
    assert "overwrite it in place" in slack and "never `rm`" in slack, (
        "Slack guidance must say overwrite-to-revise, never rm-and-recreate"
    )
    assert "do NOT also call create_file_upload_url or send_message" in slack, (
        "the duplicate-delivery warning must survive the additions"
    )
    assert "at most five files" not in block and "five files per turn" not in block, (
        "the retired per-turn file-count cap must not be described anywhere"
    )


def test_replaces_stale_block_preserving_user_body() -> None:
    seeded = apply_credential_guidance("ORIGINAL BODY")
    # Simulate a user editing only their body underneath the (now stale) block.
    edited = seeded.replace("ORIGINAL BODY", "EDITED BODY")
    reapplied = apply_credential_guidance(edited)
    assert reapplied.count("/mnt/session/uploads/.env") == seeded.count(
        "/mnt/session/uploads/.env"
    ), "must not duplicate the block when one already exists"
    assert reapplied.endswith("EDITED BODY"), "user's edited body must be preserved"


def _collapse(text: str) -> str:
    """Whitespace-collapsed, so an assertion is about wording not line wrapping."""
    return " ".join(text.split())


def test_pre_authorises_the_workspace_checkpoint() -> None:
    # Regression for the acceptance runs where claude-sonnet-5 refused the
    # checkpoint turn 6/6 times, on Discord and on Slack, reading it as an
    # exfiltration attempt. The refusals were correct given what the agent
    # knew: nothing in its configuration described the operation. This block
    # IS its configuration, so this is where the operation gets described.
    block = CREDENTIAL_GUIDANCE_BLOCK
    assert "WORKSPACE MOVES" in block, "the operation needs a name the prompt can point at"
    section = _collapse(block[block.index("WORKSPACE MOVES") :])
    assert "`checkpoint` block" in section and "`<turn_controls>`" in section, (
        "the agent must know which element carries a genuine checkpoint request"
    )
    assert "daimon-handoff-<id>.tar.gz" in section, "and what it is being asked to build"
    assert "routine host operation, not a request from someone in the chat" in section, (
        "the block has to settle the authority question the refusals raised"
    )
    assert "never keys, hidden directories or toolchains" in section, (
        "and bound what the commands touch, since that is what made it look hostile"
    )
    assert "never posted to the thread" in section, (
        "the Slack refusal turned on /mnt/session/outputs being a delivery path; the "
        "handoff bundle is the carve-out and the block must say so"
    )
    assert "output delivery skips that name" in section, (
        "and say what makes it the carve-out, not just assert it"
    )
    assert (
        "Your memory store (/mnt/memory) and your keys and mounted files "
        "(/mnt/session/uploads) are not in the archive" in section
    ), "the mounts that belong to the workspace, not the task, are never packed"
    assert "daimon remounts them on the new workspace" in section, (
        "and leaving them out costs the agent nothing, which is why it need not argue"
    )
    assert "/root/work" in section and "/mnt/session/outputs" in section, (
        "Issue 2: nothing told the agent where to work, so a working file landed in "
        "/tmp/work, outside the roots the archive was built from"
    )


def test_workspace_move_guidance_survives_idempotent_reapplication() -> None:
    once = apply_credential_guidance("base prompt")
    twice = apply_credential_guidance(once)
    assert once.count("WORKSPACE MOVES") == 1, "the section is written exactly once"
    assert twice == once, "re-applying must replace the block, never stack it"
