# Slack Adapter — Trust Model

This page documents how daimon's Slack adapter handles per-user access and
what operators should understand about the resulting trust model.

### Per-user Slack access (optional)

By default daimon reads only channels the bot is invited to. Members can
additionally **connect their Slack account** (daimon nudges them once, and
offers a link whenever it hits a channel it can't read). A connected member's
reads run with *their* Slack permissions: any channel or DM they can see, no
bot invite needed, plus message search (results that come from a DM are only
surfaced when you ask in a DM with daimon).

Trust model notes for operators:

- Connected users' reach is no longer signalled by bot presence in a channel.
  daimon answers with channel content wherever the connected user asks, gated
  only by whether that user can see the source channel themselves.
- User tokens (`xoxp-…`) are stored Fernet-encrypted (`DAIMON_CRYPTO__KEYS`),
  one row per (workspace, user), and are deleted + revoked from the `/privacy`
  panel ("Disconnect Slack").
- Workspaces with admin app-approval must have an admin approve the added
  user scopes before members can connect.
- Reads mirror the connecting user's own Slack visibility: any channel or DM
  they can see, answered wherever they ask — the same model as the Discord
  bot. The one exception is direct-message content (DMs and group DMs), which
  daimon will only surface in a DM with you, never in a channel.
