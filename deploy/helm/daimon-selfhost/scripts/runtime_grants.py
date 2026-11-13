"""Turn `provision.py`'s output into `runtime.json` rows and the harness binding's tenants.

Standard library only: `register.sh` runs it on the operator's machine.

    runtime_grants.py tenants REGISTRATION       one tenant ID per line
    runtime_grants.py rows REGISTRATION IDS      runtime.json rows as JSON

`IDS` is a JSON object of tenant ID -> [agent ID, environment ID], from
`daimon selfhosted init` run once per tenant. Every row names one platform,
channel, person and channel configuration: a turn anywhere else is refused.
"""

import json
import sys
from pathlib import Path

PROFILE = "selfhosted.loop"
# Per-turn caps shared by every grant: a short tool loop and a two-minute deadline.
LIMITS = {"infrastructure_usd": "0", "max_tool_calls": 5, "deadline_s": 120}
CLI_CHANNEL = "selfhosted-operator"


def tenants(registration: dict) -> list[str]:
    """Tenants whose turns may reach the harness: the CLI tenant, then Teams' if granted."""
    found = [registration["tenant_id"]]
    teams = registration.get("teams")
    if teams is not None and teams["tenant_id"] not in found:
        found.append(teams["tenant_id"])
    return found


def rows(registration: dict, ids: dict[str, list[str]]) -> list[dict]:
    def row(tenant_id: str, platform: str, channel_id: str, account_id: str, digest: str) -> dict:
        agent_id, environment_id = ids[tenant_id]
        return {
            "profile": PROFILE,
            "tenant_id": tenant_id,
            "platform": platform,
            "channel_id": channel_id,
            "account_id": account_id,
            "config_digest": digest,
            "agent_id": agent_id,
            "environment_id": environment_id,
            **LIMITS,
        }

    found = [
        row(
            registration["tenant_id"],
            "cli",
            CLI_CHANNEL,
            registration["account_id"],
            registration["config_digest"],
        )
    ]
    teams = registration.get("teams")
    if teams is not None:
        found += [
            row(teams["tenant_id"], "teams", g["channel_id"], g["account_id"], g["config_digest"])
            for g in teams["grants"]
        ]
    return found


def main(argv: list[str]) -> None:
    command, registration = argv[0], json.loads(Path(argv[1]).read_text())
    if command == "tenants":
        print("\n".join(tenants(registration)))
    elif command == "rows":
        print(json.dumps(rows(registration, json.loads(Path(argv[2]).read_text()))))
    else:
        raise SystemExit(f"unknown command {command!r}")


if __name__ == "__main__":
    main(sys.argv[1:])
