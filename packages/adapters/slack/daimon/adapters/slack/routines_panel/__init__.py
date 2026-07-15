"""Slack routines panel — `/routines` slash command surface (SUX-02).

Subpackage structure:
  state.py   — pure glyph reducer + dataclasses (no I/O)
  read.py    — shell: load_routines (store reads → RoutineEntry list)
  views.py   — pure Block Kit builders (loading/content/last-output views)
  actions.py — shell: slash handler + overflow pause/resume/output handlers
"""
