---
name: setup
description: Use when the user asks Claude Code to self-host Daimon OS or set up a local Daimon deployment. Follow the repository's SETUP.md and pause only for credentials or browser consent.
---

# Set up Daimon OS

1. Find the Daimon OS repository checkout. If none exists, clone
   `https://github.com/pymc-labs/daimon.git` into a directory the user chooses.
2. Read `SETUP.md` in that checkout. Follow its commands in order and parse
   the JSON from each setup or verification command. Treat a missing step as
   work to do; do not assume that an exit code alone proves setup is complete.
3. Tell the owner to enter an Anthropic API key from a workspace dedicated to
   this deployment directly into the local `.env` file with restricted
   permissions. Never ask for the key in chat or handle its value yourself.
   Never echo, log, commit or paste the key into a command argument.
4. Start the local CLI path and get a first reply before asking the user to
   create a Discord or Slack app. Record the elapsed time and human steps as
   `SETUP.md` describes.
5. For Discord, show the portal checklist and run the verifier after the user
   creates and invites the bot. For Slack, use the checked-in manifest and
   stop for the app creation, token issuance and workspace install steps.
6. Report what works, what remains, and the exact next human action. GitHub App
   registration is an optional later step; it does not gate the first reply.

If a command fails, report its redacted error and the command that failed.
Do not create accounts, register apps on an organization, or deploy without
the user's authorization.
