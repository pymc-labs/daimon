"""Slack routines panel — `/routines` slash command surface.

Subpackage structure:
  state.py   — pure dataclasses; glyph and label rules live in daimon.core.routines
  read.py    — shell: load_routines (store reads → RoutineEntry list)
  views.py   — pure Block Kit builders (loading/content/last-output views)
  actions.py — shell: slash handler + overflow pause/resume/output handlers
"""
