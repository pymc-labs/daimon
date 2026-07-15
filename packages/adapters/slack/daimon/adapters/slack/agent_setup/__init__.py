"""Slack agent-setup panel — `/agent-setup` slash command surface (SUX-01).

Subpackage structure:
  state.py   — pure private_metadata (de)serialize + reducer functions (no I/O)
  views.py   — pure Block Kit builders (loading/L1/L2/L3 views)
  read.py    — shell: load_tenant_roster, load_section_data (store reads)
  write.py   — shell: create/fork/delete agents, scope writes, credential writes
  actions.py — shell: slash handler + block_actions handlers
  submit.py  — shell: view_submission handlers
"""
