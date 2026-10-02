"""Platform-neutral core for agent-authored, multi-step interactive forms.

This package lives in `daimon.core`, not in an adapter, because the two
processes that need it cannot import each other: the MCP server posts a
form on behalf of an agent, while the Discord or Teams adapter later
dispatches the taps on that form's buttons. Import-linter's independence
contract forbids adapters from importing one another, so the shared schema,
state, and wire grammar have to sit one level up, in core. This layer emits
a platform-neutral screen description; Discord renders it as components and
Teams as an Adaptive Card (`teams_card`).

The wizard runs on Discord and Teams; Slack is deliberately left out.
`tests/parity/test_wizard_slack_exempt.py` is the executable record of that
exemption: it fails the day a Slack wizard renderer appears without being
updated alongside it, rather than silently going stale as a comment would.
"""

from __future__ import annotations
