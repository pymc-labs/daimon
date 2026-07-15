"""Slack Block Kit privacy panel — /privacy slash command surfaces.

Subpackage layout:
  read.py    — resolve_privacy_account (read-only, no create) + load_purge_preview
  views.py   — pure Block Kit view/block builders (no slack_sdk imports)
  actions.py — slash-command handler + block_action handler (I/O shell)
  submit.py  — view_submission evaluation + background purge + views.update (I/O shell)
"""
