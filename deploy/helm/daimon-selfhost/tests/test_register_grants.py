"""Offline: register.sh grants the CLI operator and, when asked, named Teams people."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

CHART = Path(__file__).resolve().parents[1]
CLI_TENANT = "11111111-1111-4111-8111-111111111111"
TEAMS_TENANT = "22222222-2222-4222-8222-222222222222"

STUB = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
name = Path(sys.argv[0]).name
with open(os.environ['STUB_LOG'], 'a') as output:
    output.write(json.dumps([name, *args]) + '\n')
if name == 'kubectl' and 'exec' in args and args[-2:] == ['python', '-']:
    print(os.environ['STUB_REGISTRATION'])
elif name == 'kubectl' and args[2:3] == ['get']:
    sys.exit(0 if os.environ['STUB_TEAMS'] == 'true' and args[3].endswith('-teams') else 1)
elif name == 'kubectl' and 'init' in args:
    tenant = args[args.index('--tenant-id') + 1]
    print(f'sh_agent_{tenant[:4]} sh_env_{tenant[:4]}')
"""


def registration(teams):
    found = {"tenant_id": CLI_TENANT, "account_id": "cli-account", "config_digest": "cli-digest"}
    if teams:
        found["teams"] = {
            "tenant_id": TEAMS_TENANT,
            "grants": [
                {"channel_id": "19:pilot@thread.tacv2", "account_id": "a1", "config_digest": "d"},
                {"channel_id": "19:pilot@thread.tacv2", "account_id": "a2", "config_digest": "d"},
            ],
        }
    return found


class RegisterGrantsTest(unittest.TestCase):
    def register(self, *, teams):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = Path(directory.name)
        (state / "aks-values.json").write_text("{}")
        stub = state / "stub"
        stub.write_text(STUB)
        stub.chmod(0o755)
        for name in ("kubectl", "helm"):
            (state / name).symlink_to(stub)
        log = state / "calls.jsonl"
        env = dict(
            os.environ,
            PATH=str(state) + os.pathsep + os.environ["PATH"],
            AKS_STATE=str(state),
            RELEASE="acme",
            NAMESPACE="daimon",
            STUB_LOG=str(log),
            STUB_REGISTRATION=json.dumps(registration(teams)),
            SELFHOSTED_MODEL="gpt-5.4-mini",
            SELFHOSTED_MODELS=json.dumps({"gpt-5.4-mini-TEST": []}),
            ALLOW_TEST_TARIFFS="true",
            STUB_TEAMS="true" if teams else "false",
            TEAMS_GRANTS=json.dumps({"19:pilot@thread.tacv2": ["p"]}) if teams else "",
        )
        subprocess.run(
            ["bash", str(CHART / "scripts/register.sh")],
            env=env,
            cwd=CHART.parents[2],
            check=True,
            capture_output=True,
        )
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        runtime = json.loads((state / "runtime.json").read_text())
        host_env = json.loads((state / "aks-values.json").read_text())["host"]["env"]
        bindings = json.loads(host_env["DAIMON_SELFHOSTED__HARNESS_AUTH_BINDINGS"])
        return calls, runtime, bindings

    def test_cli_only_registration_is_unchanged(self):
        calls, runtime, bindings = self.register(teams=False)
        self.assertEqual(len(runtime), 1)
        self.assertEqual(runtime[0]["platform"], "cli")
        self.assertEqual(runtime[0]["channel_id"], "selfhosted-operator")
        self.assertEqual(runtime[0]["agent_id"], "sh_agent_1111")
        self.assertEqual(bindings[0]["tenants"], [CLI_TENANT])
        self.assertEqual(sum("init" in call for call in calls), 1)
        restart = next(call for call in calls if "restart" in call)
        self.assertNotIn("deploy/acme-daimon-teams", restart)

    def test_teams_grants_add_one_row_per_person_and_the_teams_tenant(self):
        calls, runtime, bindings = self.register(teams=True)
        teams = [row for row in runtime if row["platform"] == "teams"]
        self.assertEqual([row["account_id"] for row in teams], ["a1", "a2"])
        for row in teams:
            self.assertEqual(row["tenant_id"], TEAMS_TENANT)
            self.assertEqual(row["channel_id"], "19:pilot@thread.tacv2")
            self.assertEqual(
                (row["agent_id"], row["environment_id"]), ("sh_agent_2222", "sh_env_2222")
            )
            self.assertEqual(row["profile"], "selfhosted.loop")
        self.assertEqual(bindings[0]["tenants"], [CLI_TENANT, TEAMS_TENANT])
        inits = [call[call.index("--tenant-id") + 1] for call in calls if "init" in call]
        self.assertEqual(inits, [CLI_TENANT, TEAMS_TENANT])
        provision = next(call for call in calls if call[-2:] == ["python", "-"])
        self.assertIn('TEAMS_GRANTS={"19:pilot@thread.tacv2": ["p"]}', provision)
        restart = next(call for call in calls if "restart" in call)
        self.assertIn("deploy/acme-daimon-teams", restart)
        self.assertNotIn("secret", json.dumps(runtime).lower())


if __name__ == "__main__":
    unittest.main()
