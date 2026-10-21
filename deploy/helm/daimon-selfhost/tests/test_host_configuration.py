"""Rendered regressions for the live AKS runtime grant and host egress boundary."""

import subprocess
import unittest
from pathlib import Path

import yaml

CHART = Path(__file__).resolve().parents[1]


def render(*values):
    command = ["helm", "template", "coke", str(CHART)]
    for value in values:
        command.extend(["--set", value])
    output = subprocess.run(command, check=True, capture_output=True, text=True).stdout
    return [r for r in yaml.safe_load_all(output) if r]


def hosts(resources):
    return [r for r in resources if r["kind"] in {"Job", "Deployment"}]


class HostConfigurationTests(unittest.TestCase):
    def test_runtime_and_csi_compose_for_every_host(self):
        resources = render(
            "host.runtimeSecret=fixture-runtime",
            "keyVaultCSI.enabled=true",
            "keyVaultCSI.secretProviderClass=fixture-csi",
            "services.discord.enabled=true",
            "services.slack.enabled=true",
            "host.env.DAIMON_TURN__PROVIDER_RUNTIME_FILE=/ignored",
        )
        self.assertEqual(len(hosts(resources)), 6)
        for resource in hosts(resources):
            pod = resource["spec"]["template"]["spec"]
            container = pod["containers"][0]
            paths = [
                e["value"]
                for e in container["env"]
                if e["name"] == "DAIMON_TURN__PROVIDER_RUNTIME_FILE"
            ]
            self.assertEqual(paths, ["/etc/daimon-runtime/runtime.json"])
            mounts = {m["name"]: m for m in container["volumeMounts"]}
            self.assertEqual(mounts["runtime"]["mountPath"], "/etc/daimon-runtime")
            self.assertTrue(mounts["runtime"]["readOnly"])
            self.assertIn("secrets-store", mounts)
            volumes = {v["name"]: v for v in pod["volumes"]}
            self.assertEqual(volumes["runtime"]["secret"]["secretName"], "fixture-runtime")
            self.assertEqual(
                volumes["runtime"]["secret"]["items"],
                [{"key": "runtime.json", "path": "runtime.json"}],
            )
            self.assertIn("csi", volumes["secrets-store"])
            self.assertEqual(container["resources"]["limits"]["memory"], "1536Mi")
        harness = next(
            r
            for r in resources
            if r["kind"] == "StatefulSet" and r["metadata"]["name"].endswith("-harness")
        )
        self.assertNotIn(
            "runtime", [v["name"] for v in harness["spec"]["template"]["spec"].get("volumes", [])]
        )

    def test_runtime_disabled_has_no_mount_or_implicit_env(self):
        for resource in hosts(render()):
            pod = resource["spec"]["template"]["spec"]
            container = pod["containers"][0]
            self.assertNotIn("runtime", [v["name"] for v in pod.get("volumes", [])])
            self.assertNotIn(
                "DAIMON_TURN__PROVIDER_RUNTIME_FILE", [e["name"] for e in container["env"]]
            )

    def test_default_host_policy_only_allows_dns_and_release_services(self):
        policy = next(
            r
            for r in render()
            if r["kind"] == "NetworkPolicy" and r["metadata"]["name"].endswith("-host-egress")
        )["spec"]
        self.assertEqual(policy["policyTypes"], ["Egress"])
        selector = policy["podSelector"]["matchExpressions"][0]
        self.assertEqual(
            selector,
            dict(
                key="app",
                operator="In",
                values=[
                    "coke-daimon-" + n
                    for n in ("init", "mcp", "teams", "scheduler", "discord", "slack")
                ],
            ),
        )
        rules = policy["egress"]
        self.assertEqual(len(rules), 4)
        self.assertEqual(
            rules[0],
            dict(
                to=[
                    dict(
                        namespaceSelector=dict(
                            matchLabels={"kubernetes.io/metadata.name": "kube-system"}
                        ),
                        podSelector=dict(matchLabels={"k8s-app": "kube-dns"}),
                    )
                ],
                ports=[dict(protocol="UDP", port=53), dict(protocol="TCP", port=53)],
            ),
        )
        for rule, name, port in zip(
            rules[1:], ("postgres", "mcp", "harness"), (5432, 8765, 8088), strict=True
        ):
            self.assertEqual(
                rule,
                dict(
                    to=[dict(podSelector=dict(matchLabels={"app": "coke-daimon-" + name}))],
                    ports=[dict(protocol="TCP", port=port)],
                ),
            )

    def test_external_postgres_is_explicit_and_narrow(self):
        resources = render(
            "postgres.bundled=false",
            "postgres.host=pg.example.invalid",
            "networkPolicy.hostPostgresCidrs={10.1.2.3/32}",
            "postgres.port=6432",
        )
        policy = next(
            r
            for r in resources
            if r["kind"] == "NetworkPolicy" and r["metadata"]["name"].endswith("-host-egress")
        )["spec"]
        self.assertEqual(
            policy["egress"][1],
            dict(
                to=[dict(ipBlock=dict(cidr="10.1.2.3/32"))], ports=[dict(protocol="TCP", port=6432)]
            ),
        )
        for cidr in ("0.0.0.0/0", "169.254.169.254/32"):
            with self.assertRaises(subprocess.CalledProcessError):
                render(
                    "postgres.bundled=false",
                    "postgres.host=pg.example.invalid",
                    "networkPolicy.hostPostgresCidrs={" + cidr + "}",
                )

    def test_shutdown_health_and_priorities(self):
        resources = render()
        priorities = {
            r["metadata"]["name"]: r["value"] for r in resources if r["kind"] == "PriorityClass"
        }
        self.assertGreater(priorities["coke-daimon-postgres"], priorities["coke-daimon-harness"])
        self.assertGreater(priorities["coke-daimon-harness"], 0)
        for resource in hosts(resources):
            pod = resource["spec"]["template"]["spec"]
            self.assertEqual(pod["terminationGracePeriodSeconds"], 90)
            if resource["kind"] == "Deployment":
                container = pod["containers"][0]
                self.assertEqual(
                    container["livenessProbe"]["httpGet"], container["readinessProbe"]["httpGet"]
                )
                self.assertGreaterEqual(
                    container["startupProbe"]["failureThreshold"]
                    * container["startupProbe"]["periodSeconds"],
                    300,
                )
        for resource in resources:
            if resource["kind"] == "StatefulSet":
                pod = resource["spec"]["template"]["spec"]
                self.assertIn(pod["priorityClassName"], priorities)
                if resource["metadata"]["name"].endswith("-harness"):
                    container = pod["containers"][0]
                    self.assertEqual(
                        container["securityContext"]["seccompProfile"]["type"], "RuntimeDefault"
                    )
                    self.assertIn(
                        "exec setpriv --bounding-set=-sys_admin,-setpcap --", container["args"][0]
                    )

    def test_teams_graph_routes_and_acr_postgres(self):
        resources = render("ingress.enabled=true", "images.registry=fixture.azurecr.io")
        ingress = next(r for r in resources if r["kind"] == "Ingress")
        paths = ingress["spec"]["rules"][0]["http"]["paths"]
        self.assertEqual(
            {p["path"] for p in paths},
            {"/api/messages", "/api/graph/notifications", "/api/graph/lifecycle"},
        )
        for path in paths:
            self.assertEqual(path["backend"]["service"]["name"], "coke-daimon-teams")
            self.assertEqual(path["pathType"], "Exact")
        postgres = next(
            r
            for r in resources
            if r["kind"] == "StatefulSet" and r["metadata"]["name"].endswith("-postgres")
        )
        self.assertEqual(
            postgres["spec"]["template"]["spec"]["containers"][0]["image"],
            "fixture.azurecr.io/postgres:18-alpine",
        )
        for host in hosts(resources):
            self.assertEqual(
                host["spec"]["template"]["spec"]["initContainers"][0]["image"],
                "fixture.azurecr.io/postgres:18-alpine",
            )

    def test_policy_switch_disables_both_policies(self):
        self.assertFalse(
            any(r["kind"] == "NetworkPolicy" for r in render("networkPolicy.enabled=false"))
        )


if __name__ == "__main__":
    unittest.main()
