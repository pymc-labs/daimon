"""A second release on a shared cluster names everything after itself."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml
from test_host_configuration import CHART, add_kube_stub_support

# A Teams-only release whose Secrets are named as aks.sh creates them for daimon-teams.
TEAMS_VALUES = """\
secrets: {host: daimon-teams-host, harness: daimon-teams-harness}
postgres: {existingSecret: daimon-teams-postgres}
azureOpenAI: {apiKeySecretRef: {name: daimon-teams-aoai}}
services: {teams: {enabled: true}}
ingress:
  enabled: true
  mcpHost: ""
  tls: [{hosts: [teams.example.invalid], secretName: daimon-teams-tls}]
networkPolicy: {teamsEgress: {enabled: true, cidrs: [10.2.3.4/32]}}
"""
# The chart's pilot release name; a second release must not reuse it.
SHARED = ("acme",)
DEFAULT_SECRETS = {
    "daimon-host",
    "daimon-harness",
    "daimon-postgres",
    "daimon-aoai",
    "daimon-runtime",
}


def render(release, *args):
    command = ["helm", "template", release, str(CHART), *args]
    output = subprocess.run(command, check=True, capture_output=True, text=True).stdout
    return output, [r for r in yaml.safe_load_all(output) if r]


def render_teams():
    with tempfile.NamedTemporaryFile("w", suffix=".yaml") as values:
        values.write(TEAMS_VALUES)
        values.flush()
        return render("daimon-teams", "-n", "daimon-teams", "-f", values.name)


def secret_references(resources):
    """Every Secret a rendered workload or Ingress reads, by name."""
    names = set()

    def walk(node, key=None):
        if isinstance(node, dict):
            if key in {"secretRef", "secretKeyRef"} and "name" in node:
                names.add(node["name"])
            if key == "secret" and "secretName" in node:
                names.add(node["secretName"])
            for child_key, child in node.items():
                walk(child, child_key)
        elif isinstance(node, list):
            for child in node:
                walk(child, key)

    for resource in resources:
        walk(resource.get("spec", {}))
        if resource["kind"] == "Ingress":
            names.update(t["secretName"] for t in resource["spec"].get("tls", []))
            proxy = resource["metadata"]["annotations"].get(
                "nginx.ingress.kubernetes.io/proxy-ssl-secret"
            )
            if proxy:
                names.add(proxy.split("/", 1)[1])
    return names


class TeamsReleaseRenderTests(unittest.TestCase):
    def test_daimon_teams_render_has_no_shared_names(self):
        output, resources = render_teams()
        lowered = output.lower()
        for name in SHARED:
            self.assertNotIn(name, lowered)
        for resource in resources:
            self.assertTrue(
                resource["metadata"]["name"].startswith("daimon-teams-"),
                resource["metadata"]["name"],
            )
        secrets = secret_references(resources)
        self.assertTrue(secrets)
        self.assertEqual({s for s in secrets if not s.startswith("daimon-teams-")}, set())
        self.assertLessEqual(
            {
                "daimon-teams-host",
                "daimon-teams-harness",
                "daimon-teams-postgres",
                "daimon-teams-aoai",
            },
            secrets,
        )
        # PriorityClasses are cluster-wide; the namespace keeps them apart.
        priorities = [r["metadata"]["name"] for r in resources if r["kind"] == "PriorityClass"]
        self.assertTrue(priorities)
        self.assertTrue(all("daimon-teams-daimon-daimon-teams-" in p for p in priorities))

    def test_defaults_keep_the_original_names(self):
        _, resources = render("acme")
        self.assertLessEqual(
            secret_references(resources) - {"acme-daimon-mcp-tls"}, DEFAULT_SECRETS
        )
        self.assertIn("daimon-host", secret_references(resources))

    def test_empty_host_skips_that_ingress(self):
        _, resources = render(
            "acme",
            "--set",
            "ingress.enabled=true",
            "--set",
            "services.teams.enabled=true",
            "--set",
            "ingress.mcpHost=",
        )
        ingresses = [r["metadata"]["name"] for r in resources if r["kind"] == "Ingress"]
        self.assertEqual(ingresses, ["acme-daimon-teams"])


STUB = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['STUB_LOG'], 'a') as output:
    output.write(json.dumps([name, *args]) + '\\n')
if name == 'az' and 'properties.endpoint' in args:
    print('https://localhost')
elif name == 'az' and 'key1' in args:
    print('offline-placeholder')
elif name == 'kubectl' and 'exec' in args and 'init' in args:
    print('agent-id environment-id')
elif name == 'kubectl' and 'exec' in args and '-i' in args:
    print(json.dumps({'tenant_id': 'tenant-id', 'account_id': 'account-id', 'config_digest': 'digest'}))
"""


class TeamsReleaseScriptTests(unittest.TestCase):
    def run_script(self, script, command, state, overrides=None):
        stub = state / "stub"
        if not stub.exists():
            stub.write_text(STUB)
            add_kube_stub_support(stub)
            stub.chmod(0o755)
            for name in ("az", "kubectl", "helm"):
                (state / name).symlink_to(stub)
        log = state / "calls.jsonl"
        log.unlink(missing_ok=True)
        (state / "values.yaml").write_text(TEAMS_VALUES)
        env = dict(
            os.environ,
            PATH=str(state) + os.pathsep + os.environ["PATH"],
            AKS_STATE=str(state),
            STUB_LOG=str(log),
            RELEASE="daimon-teams",
            NAMESPACE="daimon-teams",
            SECRET_PREFIX="daimon-teams",
            VALUES_FILE=str(state / "values.yaml"),
            ALLOW_TEST_TARIFFS="true",
            SELFHOSTED_MODEL="gpt-5.4-mini",
            SELFHOSTED_MODELS=json.dumps({"fixture": {}}),
        )
        env.update({key: "offline" for key in ("RG", "LOCATION", "ACR", "AKS", "AOAI", "TAG")})
        env.update(overrides or {})
        result = subprocess.run(
            ["bash", str(CHART / "scripts" / script), *command],
            env=env,
            capture_output=True,
            text=True,
            cwd=CHART.parents[2],
        )
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return result, calls

    def assert_only_release_names(self, calls):
        for call in calls:
            flat = " ".join(call)
            self.assertNotIn("acme", flat)
            if call[0] in {"kubectl", "helm"} and "-n" in call:
                self.assertEqual(call[call.index("-n") + 1], "daimon-teams", call)

    def test_install_and_register_use_the_release_namespace_and_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            result, calls = self.run_script("aks.sh", ["install"], state)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_only_release_names(calls)
            self.assertIn(
                [
                    "kubectl",
                    "create",
                    "namespace",
                    "daimon-teams",
                    "--dry-run=client",
                    "-o",
                    "yaml",
                ],
                calls,
            )
            created = {c[c.index("generic") + 1] for c in calls if "generic" in c}
            self.assertEqual(
                created,
                {
                    "daimon-teams-postgres",
                    "daimon-teams-host",
                    "daimon-teams-harness",
                    "daimon-teams-aoai",
                },
            )
            helm = next(c for c in calls if c[0] == "helm")
            self.assertEqual(
                helm[1:6],
                ["upgrade", "--install", "daimon-teams", "deploy/helm/daimon-selfhost", "-n"],
            )
            files = [helm[i + 1] for i, arg in enumerate(helm) if arg == "-f"]
            self.assertEqual(files, [str(state / "values.yaml"), str(state / "aks-values.json")])
            values = json.loads((state / "aks-values.json").read_text())
            self.assertEqual(
                values["secrets"], {"host": "daimon-teams-host", "harness": "daimon-teams-harness"}
            )
            self.assertEqual(values["postgres"]["existingSecret"], "daimon-teams-postgres")
            self.assertEqual(values["azureOpenAI"]["apiKeySecretRef"]["name"], "daimon-teams-aoai")
            self.assertIn("@daimon-teams-daimon-postgres:5432/", (state / "host.env").read_text())

            result, calls = self.run_script("register.sh", [], state)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_only_release_names(calls)
            self.assertTrue(any("deploy/daimon-teams-daimon-mcp" in c for c in calls))
            self.assertIn(
                "daimon-teams-runtime", {c[c.index("generic") + 1] for c in calls if "generic" in c}
            )
            values = json.loads((state / "aks-values.json").read_text())
            self.assertEqual(values["host"]["runtimeSecret"], "daimon-teams-runtime")
            binding = json.loads(values["host"]["env"]["DAIMON_SELFHOSTED__HARNESS_AUTH_BINDINGS"])
            self.assertEqual(binding[0]["origin"], "https://daimon-teams-daimon-harness:8088")
            self.assertEqual(
                values["harness"]["env"]["HARNESS_DAIMON_MCP_URL"],
                "https://daimon-teams-daimon-mcp:8765/mcp",
            )
            helm = next(c for c in calls if c[0] == "helm")
            files = [helm[i + 1] for i, arg in enumerate(helm) if arg == "-f"]
            self.assertEqual(files, [str(state / "values.yaml"), str(state / "aks-values.json")])

    def test_install_adds_operator_held_host_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            extra = state / "host-extra.env"
            for present in (False, True):
                if present:
                    extra.write_text("DAIMON_TEAMS__CLIENT_SECRET=offline-placeholder\n")
                result, calls = self.run_script("aks.sh", ["install"], state)
                self.assertEqual(result.returncode, 0, result.stderr)
                host = next(c for c in calls if "generic" in c and "daimon-teams-host" in c)
                files = [a.split("=", 1)[1] for a in host if a.startswith("--from-env-file=")]
                expected = [str(state / "host.env")] + ([str(extra)] if present else [])
                self.assertEqual(files, expected)
                # The extra keys reach the Secret, never the generated values or host.env.
                self.assertNotIn("CLIENT_SECRET", (state / "host.env").read_text())
                self.assertNotIn("CLIENT_SECRET", (state / "aks-values.json").read_text())

    def test_probe_targets_the_release_harness(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            stub_source = STUB.replace(
                "if name == 'az' and 'properties.endpoint' in args:",
                "if name == 'az' and 'properties.model.name' in args:\n    print('gpt-5.4-mini')\n"
                "elif name == 'az' and 'properties.endpoint' in args:",
            )
            (state / "stub").write_text(stub_source)
            add_kube_stub_support(state / "stub")
            (state / "stub").chmod(0o755)
            for name in ("az", "kubectl", "helm"):
                (state / name).symlink_to(state / "stub")
            result, calls = self.run_script(
                "aks.sh", ["probe"], state, {"AOAI_DEPLOYMENT": "gpt-5.4-mini"}
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            probe = next(c for c in calls if c[0] == "kubectl" and "exec" in c)
            self.assertIn("deployment/daimon-teams-daimon-mcp", probe)
            self.assertIn("HARNESS_URL=https://daimon-teams-daimon-harness:8088", probe)

    def test_remove_needs_the_resource_group_name(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            for confirmation in ("", "another-group"):
                result, calls = self.run_script(
                    "aks.sh", ["remove"], state, {"DELETE_RG": confirmation}
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("set DELETE_RG=offline", result.stderr)
                self.assertFalse(any(c[:3] == ["az", "group", "delete"] for c in calls))
            result, calls = self.run_script("aks.sh", ["remove"], state, {"DELETE_RG": "offline"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(any(c[:3] == ["az", "group", "delete"] for c in calls))

    def test_names_must_be_dns_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            for name, value in (
                ("RELEASE", "Daimon"),
                ("NAMESPACE", "a b"),
                ("SECRET_PREFIX", "x;rm"),
            ):
                result, calls = self.run_script("aks.sh", ["install"], state, {name: value})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(name + " must be a lowercase DNS label", result.stderr)
                self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
