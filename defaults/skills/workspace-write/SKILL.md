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

Call `get_cli_token("gcloud")`; the token lasts about one hour. Its tool result
enters model context and the session transcript. The literal token in the bash
tool command also passes through the transcript and may appear in the shell's
own command arguments. This route does not keep tokens out of transcripts.
The script itself reads stdin, does not accept a token argument, and redacts
errors. Do not echo the token or persist it to a file. Google tokens require
an agent binding and either the matching account’s authorized-user JSON
(`google_user_json` plus `google_user_email`) or a service-account key
(`google_sa_json`) with domain-wide delegation. With a user credential, bind only scopes the sign-in was granted, such as `gmail.modify`, `calendar`, `drive`, `documents` and `presentations` (each under `https://www.googleapis.com/auth/`); any other scope makes Google refuse the refresh with `invalid_scope`.
Ordinary chat carries the signed
identity. Missing setup is an operator error; never request a refresh
token or run `gws auth`.

Write non-secret request JSON to a working file. Replace the script/request
paths and token placeholder below with the actual values from this task. Use a
quoted heredoc delimiter so the shell does not expand token characters:

```bash
python3 /path/to/workspace-write/scripts/workspace_write.py /root/work/request.json <<'DAIMON_GOOGLE_ACCESS_TOKEN'
PASTE_EXACT_GET_CLI_TOKEN_RESULT_HERE
DAIMON_GOOGLE_ACCESS_TOKEN
```

This passes the token to the script's stdin, not its argv. Python standard
library only. Never put request files or credentials in `/mnt/session/outputs`.
Read [references/requests.md](references/requests.md) for schemas. There is no
generic command, raw URL or HTTP passthrough.

Respect the requested recipients, resources and changes. Missing targets are an
error; never infer “my” from the shared identity. Sharing, permission changes,
permanent file deletion, Calendar deletion and Drive moves are unsupported.
A clear user request authorizes that scope; do not add an extra approval step.
Do not schedule these writes as unattended routines.

On success report the returned resource ID. An error/timeout can mean a write
applied; inspect the target using available reads before retrying. Never
automatically retry sends, creates or other writes. Failure output is redacted.
