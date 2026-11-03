---
name: slides-workflow
description: Copy Google Slides templates and edit decks through a shared Workspace identity, then render and visually inspect changed slides.
---

Run on the dedicated `workspace-writer` agent with its operator-bound Google
identity. Everyone using it can use that identity. Use `get_cli_token("gcloud")`;
the token lasts about one hour. The returned token enters model context and the
session transcript. The literal token in a bash tool command is also part of
that transcript and may appear in the shell's own command arguments. Only the
script's argv/files/errors are kept free of tokens by this stdin handoff. Do not
echo or persist the token. The operator configures an agent binding and either
the matching account’s authorized-user JSON (`google_user_json` plus
`google_user_email`) or a service-account key (`google_sa_json`) with domain-wide
delegation. With a user credential, bind only scopes the sign-in was granted, such as `gmail.modify`, `calendar`, `drive`, `documents` and `presentations` (each under `https://www.googleapis.com/auth/`); any other scope makes Google refuse the refresh with `invalid_scope`. Missing binding, credentials or scopes is an operator setup error; never request a refresh token or run `gws auth`.

Work only on a template copy made for this task or a deck ID the user explicitly
names. Never use an inferred last/recent deck or edit the source template.
A concrete user request authorizes that scope. Do not create unattended routines.
Prefer copying a branded template and duplicating exemplar slides to inventing
styles. Preserve the theme, replace placeholder text, then remove unused slides.

Write a non-secret JSON request in the working directory. Replace these paths
and the placeholder with the exact token returned by `get_cli_token("gcloud")`:

```bash
python3 /path/to/slides-workflow/scripts/slides_workflow.py /root/work/request.json <<'DAIMON_GOOGLE_ACCESS_TOKEN'
PASTE_EXACT_GET_CLI_TOKEN_RESULT_HERE
DAIMON_GOOGLE_ACCESS_TOKEN
```

The quoted delimiter prevents shell expansion; the script receives the token
on stdin, not its argv. Read [references/requests.md](references/requests.md)
for schemas. Use `read` to get
current slide/shape IDs, `copy-template` to start, then named edit operations;
use validated `batch` for styling. There is no Drive sharing or file deletion
operation. Image URLs must be public HTTPS that Google can fetch; this script
does not publish local images or change Drive permissions. Ask for an authorized
hosted image when none is available.

After every text/layout/image edit, render all changed slides with `render`.
Read file-handling before opening the resulting PNGs; inspect for overflow,
clipping, overlap, missing images, template leftovers and readability. Fix and
render again when necessary. Never claim completion from API success or a
rendered file's existence alone. If rendering or inspection is unavailable,
report the deck as edited with review incomplete. Include the deck link and
only final useful PNGs in the reply. PNGs in `/mnt/session/outputs` attach to it.

Errors retain the deck ID when known. A timeout can mean a write applied:
read the deck before retrying. Copy failure with an unknown result also requires
checking for an existing copy before creating again. No automatic write retries.
The render/save path needs Pillow (`python3 -m pip install Pillow` if a custom
environment lacks it); all other operations use the standard library.
