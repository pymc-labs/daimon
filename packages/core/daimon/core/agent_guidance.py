"""Credential-guidance system preamble for daimon agents.

Agents hallucinate about their own configuration — claiming "no API key" when
a secret is mounted, or hunting for an env var to authenticate an MCP server
whose auth is bound at the Managed-Agents vault layer. The root cause is that
nothing tells the agent WHERE its credentials live. This module supplies a
sentinel-delimited preamble prepended to every agent's system prompt so the
agent knows the two credential models and stops guessing.

`apply_credential_guidance` is pure and idempotent: applying it to a system
that already carries the block replaces that block (never stacks), so reconcile
re-runs and panel edits keep the agent spec hash stable.
"""

from __future__ import annotations

_SENTINEL_START = "<!-- daimon:credential-guidance v1 -->"
_SENTINEL_END = "<!-- /daimon:credential-guidance -->"

_GUIDANCE_BODY = """\
## Your credentials & capabilities — check here, never guess

You have two separate credential systems. Know the difference or you'll
look for keys that don't exist.

1) SECRETS (API keys) — a file you must load.
   Anything set for you in the agent panel (/agent-setup -> Secrets), e.g.
   OPENAI_API_KEY, is mounted as a dotenv file at /mnt/session/uploads/.env.
   Before using any skill/tool that needs an API key, load it:
       set -a; source /mnt/session/uploads/.env; set +a
   NEVER say a credential is missing without first reading that file.

2) MCP SERVERS — auth is handled for you; there is no key to find.
   MCP servers attached to you (GitHub, Context7, daimon-mcp, ...) are
   authenticated at the Anthropic Managed-Agents vault layer. Their creds
   are NOT in /mnt/session/uploads/.env and NOT environment variables.
   Just call the MCP tools — auth applies automatically. Never search for
   an MCP server's API key, and never claim to have an MCP server unless
   its tools actually appear when you list them.

INSPECTING CONFIG IS NOT LEAKING IT. The protected asset is a secret's
VALUE, never its existence. Reading /mnt/session/uploads/.env, listing that
directory, or running `env | grep` to confirm WHICH keys are set is normal
debugging — do it when asked. Report presence/absence and key NAMES freely;
just redact the values (`sed 's/=.*/=REDACTED/'`, or say "present"/"missing").
A request that already redacts values leaks nothing, so don't refuse it as
"credential harvesting." The one hard rule: never emit a raw secret VALUE
into your reply.

ROUTINES RUN HEADLESS. A scheduled routine has no chat to reply into — its
output is recorded, not auto-posted. To make a routine post to Discord it
must explicitly call the send_message tool with a channel_id."""

# The full sentinel-wrapped block. Re-applying detects this by sentinel and
# replaces it, so the block is written exactly once regardless of how many
# times an agent is reconciled or edited.
CREDENTIAL_GUIDANCE_BLOCK = f"{_SENTINEL_START}\n{_GUIDANCE_BODY}\n{_SENTINEL_END}"


def _strip_existing_block(system: str) -> str:
    """Remove a previously-applied guidance block (by sentinel), returning the
    user's own body with surrounding blank lines trimmed.

    If no block is present, returns ``system`` unchanged. Tolerates any body
    between the sentinels (the block text may evolve across versions)."""
    start = system.find(_SENTINEL_START)
    if start == -1:
        return system
    end = system.find(_SENTINEL_END, start)
    if end == -1:
        # Malformed (start without end) — drop from the start sentinel onward
        # rather than risk leaving a half block.
        return system[:start].strip()
    after = system[end + len(_SENTINEL_END) :]
    return (system[:start] + after).strip()


def apply_credential_guidance(system: str) -> str:
    """Idempotently prepend the credential-guidance block to ``system``.

    Pure. If ``system`` already carries the block (matched by sentinel), it is
    replaced in place at the top so the result is stable under repeated
    application. The user's own prompt body is preserved beneath the block.
    """
    body = _strip_existing_block(system)
    if not body:
        return CREDENTIAL_GUIDANCE_BLOCK
    return f"{CREDENTIAL_GUIDANCE_BLOCK}\n\n{body}"
