"""compute_session: the thin MCP server that runs inside a compute session machine.

The control plane launches one machine per session, gives it one token in
``MCP_AUTH_TOKEN`` and drives it through four tools: ``run_bash``, ``write_file``,
``read_file`` and ``list_files``. This package is everything the machine runs. It
imports nothing from Daimon and holds no credential but that token.
"""

from __future__ import annotations
