"""Slack agent-setup panel — `/agent-setup` slash command surface.

The panel is read-only. It shows the workspace's agents, one agent's details,
and who answers where; changing an agent happens in the setup conversation.
The exceptions are creating a new agent, which has nothing to put at risk, and
workspace admins naming a channel's admins.

Subpackage structure:
  state.py        — pure private_metadata (de)serialize (no I/O)
  panel_views.py  — pure Block Kit builders for the panel's screens
  read.py         — shell: roster, details and answering-map store reads
  write.py        — shell: create agents, credential writes
  coding_tools.py — shell: mint and revoke per-agent MCP tokens
  actions.py      — shell: slash handler + block_actions handlers
  submit.py       — shell: the New agent view_submission
  channel_admins.py — shell: the channel admins view_submission
"""
