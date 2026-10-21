"""Rendered regressions for the live AKS runtime grant and host egress boundary."""

import base64
import json
import os
import subprocess
import tempfile
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
            "services.teams.enabled=true",
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
        self.assertGreater(
            priorities["coke-daimon-default-postgres"], priorities["coke-daimon-default-harness"]
        )
        self.assertGreater(priorities["coke-daimon-default-harness"], 0)
        for resource in hosts(resources):
            pod = resource["spec"]["template"]["spec"]
            self.assertEqual(pod["terminationGracePeriodSeconds"], 90)
            if resource["kind"] == "Deployment":
                container = pod["containers"][0]
                probe = container["readinessProbe"]["httpGet"]
                self.assertEqual(probe["port"], container["ports"][0]["containerPort"])
                if resource["metadata"]["name"].endswith(("-mcp", "-teams")):
                    self.assertEqual(probe["path"], "/readyz")
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
        resources = render(
            "ingress.enabled=true",
            "services.teams.enabled=true",
            "images.registry=fixture.azurecr.io",
        )
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
                next(
                    c
                    for c in host["spec"]["template"]["spec"]["initContainers"]
                    if c["name"] == "wait-for-postgres"
                )["image"],
                "fixture.azurecr.io/postgres:18-alpine",
            )

    def test_mcp_tls_ca_mount_is_scoped_and_service_readable(self):
        resources = render(
            "keyVaultCSI.enabled=true",
            "keyVaultCSI.secretProviderClass=fixture",
            "harness.env.HARNESS_MCP_CA_BUNDLE=/ignored",
        )
        mcp = next(r for r in hosts(resources) if r["metadata"]["name"].endswith("-mcp"))
        pod = mcp["spec"]["template"]["spec"]
        container = pod["containers"][0]
        self.assertIn("ssl_keyfile=", container["args"][0])
        self.assertIn("ssl_certfile=", container["args"][0])
        self.assertLess(
            container["args"][0].index("config.load()"),
            container["args"][0].index("os.setuid(1000)"),
        )
        for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
            self.assertEqual(container[probe]["httpGet"]["scheme"], "HTTPS")
        self.assertEqual(container["securityContext"]["runAsUser"], 0)
        self.assertEqual(
            container["securityContext"]["capabilities"],
            {"drop": ["ALL"], "add": ["SETUID", "SETGID"]},
        )
        self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
        self.assertEqual(container["securityContext"]["seccompProfile"]["type"], "RuntimeDefault")
        self.assertNotIn("securityContext", pod)
        tls = next(v["secret"] for v in pod["volumes"] if v["name"] == "mcp-tls")
        self.assertEqual(tls["defaultMode"], 0o400)
        harness = next(
            r
            for r in resources
            if r["kind"] == "StatefulSet" and r["metadata"]["name"].endswith("-harness")
        )
        pod = harness["spec"]["template"]["spec"]
        container = pod["containers"][0]
        env = {e["name"]: e.get("value") for e in container["env"]}
        self.assertEqual(env["HARNESS_MCP_CA_BUNDLE"], "/etc/daimon/mcp-ca/ca.crt")
        self.assertEqual(env["HARNESS_DAIMON_MCP_URL"], "https://coke-daimon-mcp:8765/mcp")
        volumes = {v["name"]: v for v in pod["volumes"]}
        self.assertEqual(
            volumes["mcp-ca-source"]["secret"]["items"], [{"key": "ca.crt", "path": "ca.crt"}]
        )
        self.assertIn("secrets-store", volumes)
        mounts = {v["name"]: v for v in container["volumeMounts"]}
        self.assertTrue(mounts["mcp-ca"]["readOnly"])
        self.assertEqual(mounts["mcp-ca"]["mountPath"], "/etc/daimon/mcp-ca")
        self.assertNotIn("mcp-ca-source", mounts)
        self.assertIn("-o 10000 -g 10000 -m 0400", pod["initContainers"][0]["command"][2])

    def test_generated_certificate_chain_and_service_sans(self):
        resources = render()
        secret = next(r for r in resources if r["kind"] == "Secret")
        self.assertEqual(secret["type"], "kubernetes.io/tls")
        self.assertEqual(set(secret["data"]), {"ca.crt", "tls.crt", "tls.key"})
        with tempfile.TemporaryDirectory() as directory:
            for key in ("ca.crt", "tls.crt"):
                Path(directory, key).write_bytes(base64.b64decode(secret["data"][key]))
            for host in (
                "coke-daimon-mcp",
                "coke-daimon-mcp.default",
                "coke-daimon-mcp.default.svc",
                "coke-daimon-mcp.default.svc.cluster.local",
                "coke-daimon-harness",
                "coke-daimon-harness.default",
                "coke-daimon-harness.default.svc",
                "coke-daimon-harness.default.svc.cluster.local",
            ):
                result = subprocess.run(
                    [
                        "openssl",
                        "verify",
                        "-CAfile",
                        directory + "/ca.crt",
                        "-verify_hostname",
                        host,
                        directory + "/tls.crt",
                    ],
                    capture_output=True,
                )
                self.assertEqual(result.returncode, 0, "Service SAN/chain verification failed")
            result = subprocess.run(
                [
                    "openssl",
                    "verify",
                    "-CAfile",
                    directory + "/ca.crt",
                    "-verify_hostname",
                    "unrelated.invalid",
                    directory + "/tls.crt",
                ],
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0, "Wrong hostname accepted")
            # An unrelated chart-generated CA cannot authenticate this leaf.
            other = next(r for r in render() if r["kind"] == "Secret")
            Path(directory, "other.crt").write_bytes(base64.b64decode(other["data"]["ca.crt"]))
            result = subprocess.run(
                ["openssl", "verify", "-CAfile", directory + "/other.crt", directory + "/tls.crt"],
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0, "Untrusted CA accepted")

    def test_operator_tls_secret_and_separate_ingress_protocols(self):
        resources = render(
            "mcpTLS.existingSecret=operator-cert",
            "ingress.enabled=true",
            "services.teams.enabled=true",
        )
        self.assertFalse(any(r["kind"] == "Secret" for r in resources))
        ingresses = {r["metadata"]["name"]: r for r in resources if r["kind"] == "Ingress"}
        annotations = ingresses["coke-daimon-mcp"]["metadata"]["annotations"]
        self.assertEqual(annotations["nginx.ingress.kubernetes.io/backend-protocol"], "HTTPS")
        self.assertEqual(annotations["nginx.ingress.kubernetes.io/proxy-ssl-verify"], "on")
        self.assertEqual(
            annotations["nginx.ingress.kubernetes.io/proxy-ssl-secret"], "default/operator-cert"
        )
        self.assertNotIn(
            "nginx.ingress.kubernetes.io/backend-protocol",
            ingresses["coke-daimon-teams"]["metadata"]["annotations"],
        )

    def test_harness_tls_ingress_is_release_and_namespace_scoped(self):
        policy = next(
            r
            for r in render()
            if r["kind"] == "NetworkPolicy" and r["metadata"]["name"].endswith("-harness")
        )["spec"]
        self.assertIn("Ingress", policy["policyTypes"])
        self.assertEqual(
            policy["ingress"],
            [
                dict(
                    ports=[dict(protocol="TCP", port=8088)],
                    **{
                        "from": [
                            dict(podSelector=dict(matchLabels={"app": "coke-daimon-" + name}))
                            for name in ("mcp", "teams", "scheduler", "discord", "slack")
                        ]
                    },
                )
            ],
        )

    def test_policy_switch(self):
        self.assertFalse(
            any(r["kind"] == "NetworkPolicy" for r in render("networkPolicy.enabled=false"))
        )

    def test_adapter_ports_and_host_bundle(self):
        resources = render(
            "services.scheduler.port=18082",
            "services.teams.port=13978",
            "services.teams.enabled=true",
            "services.discord.enabled=true",
            "services.discord.port=18081",
        )
        settings = {
            "scheduler": ("DAIMON_SCHEDULER__HEALTH_PORT", 18082),
            "teams": ("DAIMON_TEAMS__PORT", 13978),
            "discord": ("DAIMON_DISCORD__HEALTH_PORT", 18081),
        }
        for resource in hosts(resources):
            pod = resource["spec"]["template"]["spec"]
            c = pod["containers"][0]
            env = {e["name"]: e.get("value") for e in c["env"]}
            self.assertEqual(env["SSL_CERT_FILE"], "/etc/daimon/ca/ca-bundle.crt")
            init = next(c for c in pod["initContainers"] if c["name"] == "prepare-host-ca")
            self.assertIn("certifi.where()", init["args"][0])
            compile(init["args"][0], "prepare-host-ca", "exec")
            for name, (setting, port) in settings.items():
                if resource["metadata"]["name"].endswith("-" + name):
                    self.assertEqual(env[setting], str(port))
                    self.assertEqual(c["readinessProbe"]["httpGet"]["port"], port)
        harness = next(
            r
            for r in resources
            if r["metadata"]["name"].endswith("-harness") and r["kind"] == "StatefulSet"
        )
        pod = harness["spec"]["template"]["spec"]
        tls = next(v["secret"] for v in pod["volumes"] if v["name"] == "harness-tls")
        self.assertEqual(tls["defaultMode"], 0o400)
        c = pod["containers"][0]
        self.assertIn("--ssl-keyfile /etc/daimon/harness-tls/tls.key", c["args"][0])
        self.assertEqual(c["readinessProbe"]["httpGet"]["scheme"], "HTTPS")

    def test_teams_egress_never_selects_harness(self):
        resources = render(
            "networkPolicy.teamsEgress.enabled=true",
            "networkPolicy.teamsEgress.cidrs={10.2.3.4/32}",
        )
        policy = next(
            r
            for r in resources
            if r["kind"] == "NetworkPolicy" and r["metadata"]["name"].endswith("-teams-egress")
        )["spec"]
        self.assertEqual(policy["podSelector"], {"matchLabels": {"app": "coke-daimon-teams"}})
        self.assertEqual(
            policy["egress"],
            [
                {
                    "to": [{"ipBlock": {"cidr": "10.2.3.4/32"}}],
                    "ports": [{"protocol": "TCP", "port": 443}],
                }
            ],
        )
        for cidr in ("0.0.0.0/0", "169.254.169.254/32"):
            with self.assertRaises(subprocess.CalledProcessError):
                render(
                    "networkPolicy.teamsEgress.enabled=true",
                    "networkPolicy.teamsEgress.cidrs={" + cidr + "}",
                )

    def test_priority_names_differ_by_namespace(self):
        first = {r["metadata"]["name"] for r in render() if r["kind"] == "PriorityClass"}
        result = subprocess.run(
            ["helm", "template", "coke", str(CHART), "-n", "another"],
            check=True,
            capture_output=True,
            text=True,
        )
        second = {
            r["metadata"]["name"]
            for r in yaml.safe_load_all(result.stdout)
            if r and r["kind"] == "PriorityClass"
        }
        self.assertFalse(first & second)

    def test_install_retry_preserves_registration_and_ignores_terminal_pods(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            original = {
                "host": {
                    "runtimeSecret": "runtime",
                    "env": {
                        "DAIMON_SELFHOSTED__HARNESS_AUTH_BINDINGS": "binding",
                        "DAIMON_TURN__PATH": "mux",
                        "DAIMON_BILLING__SELFHOSTED__ALLOW_TEST_TARIFFS": "true",
                    },
                },
                "azureOpenAI": {"deployment": "gpt-5.4-mini"},
            }
            (state / "aks-values.json").write_text(json.dumps(original))
            (state / "runtime.json").write_text("[]")
            (state / "pg.env").write_text("POSTGRES_PASSWORD=offline-placeholder\n")
            (state / "harness.env").write_text("HARNESS_TOKEN=offline-placeholder\n")
            (state / "host.env").write_text("DAIMON_HARNESS_TOKEN=offline-placeholder\n")
            stub = state / "stub"
            stub.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
with open(os.environ['STUB_LOG'], 'a') as output:
    output.write(json.dumps([name, *sys.argv[1:]]) + '\\n')
if name == 'az' and 'properties.endpoint' in sys.argv:
    print('https://localhost')
elif name == 'az' and 'key1' in sys.argv:
    print('offline-placeholder')
""")
            stub.chmod(0o755)
            for name in ("az", "kubectl", "helm"):
                (state / name).symlink_to(stub)
            log = state / "calls.jsonl"
            env = dict(
                os.environ,
                PATH=str(state) + os.pathsep + os.environ["PATH"],
                RG="offline",
                LOCATION="offline",
                ACR="offline",
                AKS="offline",
                AOAI="offline",
                TAG="offline",
                AKS_STATE=str(state),
                STUB_LOG=str(log),
            )
            subprocess.run(
                ["bash", str(CHART / "scripts/aks.sh"), "install"],
                env=env,
                check=True,
                capture_output=True,
            )
            self.assertIn(
                "DAIMON_SELFHOSTED__HARNESS_TOKEN_PRIMARY=offline-placeholder",
                (state / "host.env").read_text().splitlines(),
            )
            values = json.loads((state / "aks-values.json").read_text())
            for key, value in original["host"].items():
                if key == "env":
                    for name, setting in value.items():
                        self.assertEqual(values["host"]["env"][name], setting)
                else:
                    self.assertEqual(values["host"][key], value)
            self.assertEqual(values["azureOpenAI"]["deployment"], "gpt-5.4-mini")
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertTrue(any("deployment" in c and "status" in c for c in calls))
            self.assertTrue(any("statefulset" in c and "status" in c for c in calls))
            self.assertFalse(any("wait" in c and "pod" in c for c in calls))
            self.assertTrue(any("secret" in c and "runtime" in c for c in calls))

    def test_bootstrap_reuses_existing_aoai_and_rejects_missing_deployment(self):
        for existing, missing, deployment in (
            (True, False, "gpt-5.4-mini"),
            (True, False, "custom-deployment"),
            (True, True, "gpt-5.4-mini"),
            (False, False, "gpt-5.4-mini"),
        ):
            with (
                self.subTest(existing=existing, missing=missing, deployment=deployment),
                tempfile.TemporaryDirectory() as directory,
            ):
                state = Path(directory)
                stub = state / "az"
                stub.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['STUB_LOG'], 'a') as output:
    output.write(json.dumps(args) + '\\n')
if args[:3] == ['cognitiveservices', 'account', 'list']:
    if os.environ['EXISTING'] == 'True':
        print('offline')
elif args[:4] == ['cognitiveservices', 'account', 'deployment', 'show']:
    sys.exit(1 if os.environ['MISSING'] == 'True' else 0)
elif 'properties.endpoint' in args:
    print('https://localhost')
elif 'key1' in args:
    print('offline-placeholder')
""")
                stub.chmod(0o755)
                for name in ("kubectl", "helm"):
                    (state / name).symlink_to(stub)
                log = state / "calls.jsonl"
                env = dict(
                    os.environ,
                    PATH=str(state) + os.pathsep + os.environ["PATH"],
                    RG="offline",
                    LOCATION="offline",
                    ACR="offline",
                    AKS="offline",
                    AOAI="offline",
                    TAG="offline",
                    AKS_STATE=str(state),
                    STUB_LOG=str(log),
                    EXISTING=str(existing),
                    MISSING=str(missing),
                )
                env.pop("AOAI_DEPLOYMENT", None)
                if deployment != "gpt-5.4-mini":
                    env["AOAI_DEPLOYMENT"] = deployment
                result = subprocess.run(
                    ["bash", str(CHART / "scripts/aks.sh"), "bootstrap"],
                    env=env,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 1 if missing else 0, result.stderr)
                if not missing:
                    subprocess.run(
                        ["bash", str(CHART / "scripts/aks.sh"), "install"],
                        env=env,
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    installed = json.loads((state / "aks-values.json").read_text())
                    self.assertEqual(installed["azureOpenAI"]["deployment"], deployment)
                calls = [json.loads(line) for line in log.read_text().splitlines()]
                aoai_creates = [
                    c for c in calls if c[:2] == ["cognitiveservices", "account"] and "create" in c
                ]
                if existing:
                    self.assertEqual(aoai_creates, [])
                    show = next(
                        c
                        for c in calls
                        if c[:4] == ["cognitiveservices", "account", "deployment", "show"]
                    )
                    self.assertEqual(show[show.index("--deployment-name") + 1], deployment)
                else:
                    self.assertEqual(len(aoai_creates), 2)
                    self.assertIn("gpt-5.4-mini", aoai_creates[1])
                if missing:
                    self.assertIn("missing or cannot read deployment", result.stderr)


if __name__ == "__main__":
    unittest.main()
