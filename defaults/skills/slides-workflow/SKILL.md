---
name: slides-workflow
description: Copy Google Slides templates and edit decks through a shared Workspace identity, then render and visually inspect changed slides.
---

Run on the dedicated `workspace-writer` agent with its operator-bound Google
identity. Everyone using it can use that identity. Use `get_cli_token("gcloud")`
and supply the returned short-lived token on stdin, never in argv, logs or a
file. No shared-account refresh token is needed. Missing binding, delegation or
scopes is an operator setup error; never run `gws auth`.

Work only on a template copy made for this task or a deck ID the user explicitly
names. Never use an inferred last/recent deck or edit the source template.
A concrete user request authorizes that scope. Do not create unattended routines.
Prefer copying a branded template and duplicating exemplar slides to inventing
styles. Preserve the theme, replace placeholder text, then remove unused slides.

Run `python3 <this-skill>/scripts/slides_workflow.py <request.json>` with token on
stdin. Requests are non-secret working files. Read
[references/requests.md](references/requests.md) for schemas. Use `read` to get
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
