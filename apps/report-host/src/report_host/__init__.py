"""report_host: a standalone host that serves a published report and its chat
sidebar.

This process talks to the daimon core only over HTTP, through the MCP seam
exposed by the daimon MCP adapter. It holds no Anthropic key, no database
credential, and no platform bot token — the only secrets it carries at
runtime are its own admin bearer and, in its own SQLite file, one seam token
per published report.
"""

from __future__ import annotations
