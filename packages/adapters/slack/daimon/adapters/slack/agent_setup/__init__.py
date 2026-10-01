"""Slack agent-setup panel — `/agent-setup` slash command surface.

The panel is read-only. It shows the workspace's agents, one agent's details,
and who answers where; changing an agent happens in the setup conversation.
The one exception is creating a new agent, which has nothing to put at risk.

Subpackage structure:
  state.py        — pure private_metadata (de)serialize (no I/O)
  panel_views.py  — pure Block Kit builders for the panel's screens
  read.py         — shell: roster, details and answering-map store reads
  write.py        — shell: create agents, credential writes
  coding_tools.py — shell: mint and revoke per-agent MCP tokens
  actions.py      — shell: slash handler + block_actions handlers
  submit.py       — shell: the New agent view_submission
"""
