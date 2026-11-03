---
name: image-workflow
description: Generate or edit raster images through OpenAI and save the finished image as a reply attachment.
---

Use the dedicated `image-maker` agent. Obtain missing provider credentials with
`request_agent_key`, exact `key=OPENAI_API_KEY` and the
waiting task. Never accept pasted keys. They mount at `/mnt/session/uploads/.env`;
every user of that agent can use them and spend the provider project's quota.
Do not put shared keys on default Daimon. Prefer an operator-limited provider
project/budget. No unattended routines are added by this skill.

Use OpenAI `gpt-image-2` by default for generation and edits, or explicitly select
`gpt-image-1.5`. Google has retired Imagen in the Gemini API; Gemini requests
are refused before credential lookup or network. There is no automatic provider
fallback. Each invocation produces exactly one bounded PNG.

Run `python3 <this-skill>/scripts/image_workflow.py <request.json>`. Read
[references/requests.md](references/requests.md) for examples. Use Pillow for image
validation (`python3 -m pip install Pillow` if a custom sandbox lacks it); no
provider SDK is required. The script reads only its selected key without sourcing
the mounted file. Do not print keys or write them into request files.

For edits, inspect the reference image using file-handling first and provide its
local path. Download a user-provided attachment with existing authorized file
access before invoking this script; it accepts no remote reference URLs. Reference
files must be regular, non-hidden, non-symlink images under 10 MB and 25 megapixels.

Output is validated and saved under `/mnt/session/outputs` with a unique filename,
which attaches to the reply. Inspect the image using file-handling, report its
filename and any limitations, and do not post it again through messaging tools.
The script never overwrites existing outputs or accepts custom output paths.

A timeout or invalid output may still mean a charged provider request. Report
the failure, never automatically repeat it or switch provider. A successful
provider response followed by a save failure is reported distinctly; avoid
claiming an attachment exists until the script returns its filename.
