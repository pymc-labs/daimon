"""Names of what a session carries for the person asking, never for a fleet worker.

The session projector adds these to an agent's resources; the worker ceiling
(runtime_manifest.worker_source_ceiling) excludes every one of them.
"""

from typing import Final

#: The opaque binding standing for the caller's private carry (a DM, a seal, attached
#: material, a held vault).
CALLER_RESOURCES_ID: Final = "caller-resources"
#: Every caller vault login: `caller-mcp:<tenant>:<account>:<agent>`, with its Slack
#: turn (`:slack-turn:<id>`) and hold (`:hold:<key>`) variants.
CALLER_VAULT_PREFIX: Final = "caller-mcp:"
#: A GitHub App session's repository vault: `app-session-vault:<tenant>:<agent>`.
APP_SESSION_VAULT_PREFIX: Final = "app-session-vault:"
