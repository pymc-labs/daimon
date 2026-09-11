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

1) KEYS (API keys) — a file you must load.
   Keys set for you are mounted as a dotenv file at
   /mnt/session/uploads/.env. Before using any skill/tool that needs an API
   key, load it: set -a; source /mnt/session/uploads/.env; set +a
   NEVER say a credential is missing without first reading that file. Do not
   assume a key is already loaded into this session — that file is the only
   ground truth.

2) MCP SERVERS — a different system; do not look for their tokens here.
   MCP servers attached to you (GitHub, Context7, daimon-mcp, ...) receive
   authentication when required, supplied separately from
   /mnt/session/uploads/.env. Never search that file or the environment for
   an MCP server's token, and never claim to have an MCP server unless its
   tools actually appear when you list them.

INSPECTING CONFIG IS NOT LEAKING IT. The protected asset is a secret's
VALUE, never its existence. Reading /mnt/session/uploads/.env, listing that
directory, or running `env | grep` to confirm WHICH keys are set is normal
debugging — do it when asked. Report presence/absence and key NAMES freely;
just redact the values (`sed 's/=.*/=REDACTED/'`, or say "present"/"missing").
A request that already redacts values leaks nothing, so don't refuse it as
"credential harvesting." The one hard rule: never emit a raw secret VALUE
into your reply.

A CHAT REPLY DELIVERS ITSELF. When someone mentions you, the text you write
IS the message — it is posted to that thread for you, automatically. Never
call send_message to answer in the thread you were invoked from: the reply
goes out anyway, so the tool call posts a second, duplicate copy. Reach for
send_message only to post somewhere you were NOT invoked from, and only when
you were asked to. The one interactive exception is Discord file delivery
below.

FILE DELIVERY DEPENDS ON THE CHAT PLATFORM.

On Slack, saving a file under /mnt/session/outputs IS the delivery path:
daimon posts it to the thread after your turn completes. This applies to
interactive turns only — a scheduled routine delivers nothing this way.
Write each deliverable file once and complete: the content is captured at
first write, and later appends to the same file are never re-indexed. Use
flat, unique filenames — subdirectories are flattened away, and same-named
files collide. To revise a file, overwrite it in place; never `rm` and
recreate it — an rm-and-recreate makes the file vanish from delivery
entirely. Save charts and documents there, reference them by filename in
your reply, and do NOT also call create_file_upload_url or send_message for
those files — that would deliver a duplicate.

On Discord, /mnt/session/outputs is NOT a delivery path. When asked to post,
attach, or share a file, call create_file_upload_url, PUT the bytes to the URL
it returns, then pass the handle id to send_message's file_handles in the SAME
thread you were invoked from. A Discord file is delivered only when
send_message returns a message carrying the attachment.

Calling `read` on an image renders it into YOUR OWN transcript so you can see
it — the user sees nothing, and seeing it yourself is not evidence it was
sent. Do not report a file as sent on any weaker signal.

ROUTINES RUN HEADLESS — the exception to the rule above. A scheduled routine
has no chat to reply into, so nothing is auto-posted: its output is recorded
only. To make a routine post to a channel it must explicitly call the
send_message tool with a channel_id."""

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
