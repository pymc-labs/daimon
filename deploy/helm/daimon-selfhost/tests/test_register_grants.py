"""Offline: register.sh registers only a deployment backed by AOAI_MODEL, prices it by that
model's own tariff, and grants the CLI operator and, when asked, named Teams people."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_host_configuration import add_kube_stub_support

CHART = Path(__file__).resolve().parents[1]
# The only calls registration may make before it refuses: both read and change nothing.
READS = (["helm", "list"], ["az", "cognitiveservices", "account", "deployment", "show"])
CLI_TENANT = "11111111-1111-4111-8111-111111111111"
TEAMS_TENANT = "22222222-2222-4222-8222-222222222222"

STUB = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
name = Path(sys.argv[0]).name
with open(os.environ['STUB_LOG'], 'a') as output:
    output.write(json.dumps([name, *args]) + '\n')
if name == 'az' and 'properties.model.name' in args:
    print(os.environ['UNDERLYING_MODEL'])
elif name == 'kubectl' and 'exec' in args and args[-2:] == ['python', '-']:
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
    def run_register(self, *, command=("register.sh",), teams=False, **overrides):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = Path(directory.name)
        (state / "aks-values.json").write_text("{}")
        stub = state / "stub"
        stub.write_text(STUB)
        add_kube_stub_support(stub)
        stub.chmod(0o755)
        for name in ("az", "kubectl", "helm"):
            (state / name).symlink_to(stub)
        env = dict(
            os.environ,
            PATH=str(state) + os.pathsep + os.environ["PATH"],
            AKS_STATE=str(state),
            RELEASE="acme",
            NAMESPACE="daimon",
            STUB_LOG=str(state / "calls.jsonl"),
            STUB_REGISTRATION=json.dumps(registration(teams)),
            UNDERLYING_MODEL="gpt-5.4-mini",
            SELFHOSTED_MODELS=json.dumps({"gpt-5.4-mini-TEST": []}),
            ALLOW_TEST_TARIFFS="true",
            STUB_TEAMS="true" if teams else "false",
            TEAMS_GRANTS=json.dumps({"19:pilot@thread.tacv2": ["p"]}) if teams else "",
        )
        env.update({key: "offline" for key in ("RG", "LOCATION", "ACR", "AKS", "AOAI", "TAG")})
        # The model settings keep their defaults unless a test names them.
        for name in ("AOAI_MODEL", "AOAI_DEPLOYMENT", "SELFHOSTED_MODEL"):
            env.pop(name, None)
        env.update(overrides)
        script, *args = command
        result = subprocess.run(
            ["bash", str(CHART / "scripts" / script), *args],
            env=env,
            cwd=CHART.parents[2],
            capture_output=True,
            text=True,
        )
        return result, state

    def calls(self, state):
        log = state / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def assert_nothing_written(self, state):
        for call in self.calls(state):
            self.assertTrue(any(call[: len(read)] == read for read in READS), call)
        self.assertFalse((state / "runtime.json").exists())
        self.assertEqual((state / "aks-values.json").read_text(), "{}")

    def register(self, *, teams):
        result, state = self.run_register(teams=teams)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls(state)
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

    def test_the_runtime_deployment_is_priced_by_the_aoai_model_tariff(self):
        for overrides, deployments in (
            ({}, {"gpt-5.4-mini": "gpt-5.4-mini-TEST"}),
            (
                {
                    "AOAI_MODEL": "gpt-6.1-sol",
                    "UNDERLYING_MODEL": "gpt-6.1-sol",
                    "SELFHOSTED_MODELS": json.dumps({"gpt-6.1-sol-TEST": []}),
                },
                {"gpt-6.1-sol": "gpt-6.1-sol-TEST"},
            ),
            (
                {
                    "AOAI_MODEL": "gpt-6.1-sol",
                    "AOAI_DEPLOYMENT": "production",
                    "UNDERLYING_MODEL": "gpt-6.1-sol",
                    "SELFHOSTED_MODELS": json.dumps({"gpt-6.1-sol": []}),
                    "ALLOW_TEST_TARIFFS": "false",
                },
                {"production": "gpt-6.1-sol"},
            ),
        ):
            with self.subTest(overrides=overrides):
                result, state = self.run_register(**overrides)
                self.assertEqual(result.returncode, 0, result.stderr)
                host_env = json.loads((state / "aks-values.json").read_text())["host"]["env"]
                self.assertEqual(
                    json.loads(host_env["DAIMON_BILLING__SELFHOSTED__DEPLOYMENTS"]), deployments
                )
                [deployment] = deployments
                read = next(call for call in self.calls(state) if call[0] == "az")
                self.assertEqual(read[read.index("--deployment-name") + 1], deployment)

    def test_a_deployment_not_backed_by_aoai_model_is_refused_before_anything_is_written(self):
        mini, sol = "gpt-5.4-mini", "gpt-6.1-sol"
        # AOAI_MODEL, AOAI_DEPLOYMENT and SELFHOSTED_MODEL (None: the default), the model
        # Azure reports for the deployment, and why registration refuses.
        for model, deployment, runtime, underlying, refusal in (
            (mini, mini, sol, mini, "to be AOAI_DEPLOYMENT"),
            (sol, sol, mini, sol, "to be AOAI_DEPLOYMENT"),
            (None, sol, None, sol, "must be backed by AOAI_MODEL"),
            (None, "production", None, sol, "must be backed by AOAI_MODEL"),
            (sol, sol, None, mini, "must be backed by AOAI_MODEL"),
            (sol, "production", None, mini, "must be backed by AOAI_MODEL"),
        ):
            settings = {
                name: value
                for name, value in (
                    ("AOAI_MODEL", model),
                    ("AOAI_DEPLOYMENT", deployment),
                    ("SELFHOSTED_MODEL", runtime),
                )
                if value
            }
            # The named model has its own tariff, so only the deployment can be refused.
            settings["SELFHOSTED_MODELS"] = json.dumps({f"{model or mini}-TEST": []})
            for command in (("register.sh",), ("aks.sh", "register")):
                with self.subTest(settings=settings, underlying=underlying, command=command):
                    result, state = self.run_register(
                        command=command, UNDERLYING_MODEL=underlying, **settings
                    )
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn(refusal, result.stderr)
                    self.assert_nothing_written(state)

    def test_a_model_without_its_own_tariff_is_refused_before_anything_is_written(self):
        # The catalogue still holds only the gpt-5.4-mini tariff.
        for command in (("register.sh",), ("aks.sh", "register")):
            with self.subTest(command=command):
                result, state = self.run_register(command=command, AOAI_MODEL="gpt-6.1-sol")
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("no tariff named gpt-6.1-sol", result.stderr)
                self.assert_nothing_written(state)


if __name__ == "__main__":
    unittest.main()
