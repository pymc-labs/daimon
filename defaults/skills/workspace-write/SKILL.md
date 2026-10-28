---
name: workspace-write
description: Send or draft Gmail and create or edit Google Drive files, Docs and Calendar events through a dedicated shared Workspace identity.
---

Use for a concrete user's requested write. Reads already available through a
connected tool need no new credential. This script acts as the operator-bound
Google identity, normally daimon@, and never as the requester. Run on the
operator's dedicated `workspace-writer` agent; do not add shared credentials to
default Daimon. Every user of a bound agent can mint its tokens and use their
scopes directly. Script refusals do not restrict arbitrary sandbox code.

Call `get_cli_token("gcloud")`; pass its short-lived token directly to script
stdin without printing it, placing it in argv, or persisting it. Google tokens
require operator deployment domain-wide delegation and an agent binding; ordinary
chat carries the needed signed identity. If missing, report the operator setup
error; never request a refresh token or run `gws auth`.

Write non-secret request JSON to a working file, then run
`python3 <this-skill>/scripts/workspace_write.py <request.json>` with the token
on stdin. This script uses Python's standard library. Never put request files
or credentials in `/mnt/session/outputs`. Read [references/requests.md](references/requests.md)
for operation schemas. There is no generic command, raw URL or HTTP passthrough.

Respect the requested recipients, resources and changes. Missing targets are an
error; never infer “my” from the shared identity. Sharing, permission changes,
permanent file deletion, Calendar deletion and Drive moves are unsupported.
A clear user request authorizes that scope; do not add an extra approval step.
Do not schedule these writes as unattended routines.

On success report the returned resource ID. An error/timeout can mean a write
applied; inspect the target using available reads before retrying. Never
automatically retry sends, creates or other writes. Failure output is redacted.
