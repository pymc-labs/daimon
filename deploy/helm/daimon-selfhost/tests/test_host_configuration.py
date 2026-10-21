"""Rendered regressions for the live AKS runtime grant and host egress boundary."""

import base64
import contextlib
import io
import json
import os
import runpy
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

CHART = Path(__file__).resolve().parents[1]


def add_kube_stub_support(path):
    """Teach an offline CLI stub the real credential and server-verification flow."""
    shebang, source = path.read_text().split("\n", 1)
    if "python" not in shebang:
        source = (
            "import subprocess\nraise SystemExit(subprocess.run(['bash', '-c', "
            + repr(source)
            + ", sys.argv[0], *sys.argv[1:]]).returncode)\n"
        )
    prefix = r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
name = Path(sys.argv[0]).name
credentials = name == 'az' and args[:2] == ['aks', 'get-credentials']
metadata = name == 'az' and args[:2] == ['aks', 'show'] and '-o' in args and args[args.index('-o')+1] == 'json'
context = name == 'kubectl' and args[:2] == ['config', 'view']
if credentials or metadata or context:
    state = Path(os.environ['AKS_STATE'])
    with (state/'kube-calls.jsonl').open('a') as output:
        output.write(json.dumps({'tool':name, 'args':args, 'config':os.environ.get('KUBECONFIG'), 'helm_server':os.environ.get('HELM_KUBEAPISERVER'), 'helm_context':os.environ.get('HELM_KUBECONTEXT')})+'\n')
    if credentials:
        if os.environ.get('CREDENTIAL_EXIT'):
            sys.exit(1)
        target = Path(args[args.index('--file')+1])
        target.write_text(json.dumps({'server':os.environ.get('CREDENTIAL_SERVER','https://pilot.example.invalid')}))
    elif metadata:
        print(os.environ.get('AKS_METADATA', '{"fqdn":"pilot.example.invalid"}'))
    else:
        print(json.loads(Path(os.environ['KUBECONFIG']).read_text())['server'])
    sys.exit(0)
"""
    path.write_text(prefix + source)


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
from unittest.mock import patch
name = Path(sys.argv[0]).name
with open(os.environ['STUB_LOG'], 'a') as output:
    output.write(json.dumps([name, *sys.argv[1:]]) + '\\n')
if name == 'az' and 'properties.endpoint' in sys.argv:
    print('https://localhost')
elif name == 'az' and 'key1' in sys.argv:
    print('offline-placeholder')
""")
            add_kube_stub_support(stub)
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
        for existing, missing, deployment, underlying in (
            (True, False, "gpt-5.4-mini", "gpt-5.4-mini"),
            (True, False, "custom-deployment", "gpt-5.4-mini"),
            (True, True, "gpt-5.4-mini", "gpt-5.4-mini"),
            (False, False, "gpt-5.4-mini", "gpt-5.4-mini"),
            (True, False, "production", "gpt-6.1-sol"),
            (True, False, "production", "gpt-6-luna"),
            (True, False, "production", ""),
        ):
            with (
                self.subTest(
                    existing=existing, missing=missing, deployment=deployment, underlying=underlying
                ),
                tempfile.TemporaryDirectory() as directory,
            ):
                state = Path(directory)
                stub = state / "az"
                stub.write_text("""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
from unittest.mock import patch
args = sys.argv[1:]
with open(os.environ['STUB_LOG'], 'a') as output:
    output.write(json.dumps(args) + '\\n')
if args[:3] == ['acr', 'repository', 'show'] and 'postgres:18-alpine' in args:
    print('sha256:77f585114c32fbca283dc835b0596f4e52b51b4c6662d7810b2f4084f60a1873')
elif args[:3] == ['acr', 'manifest', 'show']:
    name = args[args.index('-n') + 1]
    if name.endswith('@sha256:77f585114c32fbca283dc835b0596f4e52b51b4c6662d7810b2f4084f60a1873'):
        print(json.dumps({'manifests': [{'digest': 'sha256:' + 'a' * 64,
              'platform': {'os': 'linux', 'architecture': 'amd64'}}]}))
    else:
        print(json.dumps({'config': {'digest': 'sha256:' + 'b' * 64}, 'layers': []}))
elif args[:2] == ['acr', 'run'] and '--no-wait' in args:
    print('WARNING: Queued a run with ID: offline-run', file=sys.stderr)
elif args[:3] == ['acr', 'task', 'show-run']:
    print('Succeeded')
elif args[:3] == ['cognitiveservices', 'account', 'list']:
    if os.environ['EXISTING'] == 'True':
        print('offline')
elif args[:4] == ['cognitiveservices', 'account', 'deployment', 'show']:
    if os.environ['MISSING'] == 'True':
        sys.exit(1)
    print(os.environ['UNDERLYING_MODEL'])
elif 'properties.endpoint' in args:
    print('https://localhost')
elif 'key1' in args:
    print('offline-placeholder')
""")
                add_kube_stub_support(stub)
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
                    UNDERLYING_MODEL=underlying,
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
                refused = missing or (existing and underlying != "gpt-5.4-mini")
                self.assertEqual(result.returncode, 1 if refused else 0, result.stderr)
                if not refused:
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
                    self.assertEqual(show[show.index("--query") + 1], "properties.model.name")
                    self.assertEqual(show[show.index("-o") + 1], "tsv")
                else:
                    self.assertEqual(len(aoai_creates), 2)
                    self.assertIn("gpt-5.4-mini", aoai_creates[1])
                if missing:
                    self.assertIn("missing or cannot read deployment", result.stderr)


class AksClusterBindingTests(unittest.TestCase):
    def run_cluster_command(self, command, overrides=None):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            home = state / "home"
            (home / ".kube").mkdir(parents=True)
            ambient = home / ".kube/config"
            original = "foreign cluster sentinel\n"
            ambient.write_text(original)
            stub = state / "stub"
            stub.write_text(r"""#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
name, args = Path(sys.argv[0]).name, sys.argv[1:]
with (Path(os.environ['AKS_STATE'])/'operations.jsonl').open('a') as output:
    output.write(json.dumps({'tool':name,'args':args,'config':os.environ.get('KUBECONFIG'), 'helm_server':os.environ.get('HELM_KUBEAPISERVER'), 'helm_context':os.environ.get('HELM_KUBECONTEXT')})+'\n')
if name == 'az' and '--query' in args and args[args.index('--query')+1] == 'nodeResourceGroup':
    print('pilot-node-rg')
elif name == 'az' and args[:2] == ['group','exists']:
    print('false')
""")
            add_kube_stub_support(stub)
            stub.chmod(0o755)
            for name in ("az", "kubectl", "helm"):
                (state / name).symlink_to(stub)
            env = dict(
                os.environ,
                HOME=str(home),
                KUBECONFIG=str(ambient),
                HELM_KUBEAPISERVER="https://foreign.example.invalid",
                HELM_KUBECONTEXT="foreign-context",
                PATH=str(state) + os.pathsep + os.environ["PATH"],
                AKS_STATE=directory,
                AOAI_DEPLOYMENT="gpt-5.4-mini",
                SELFHOSTED_MODEL="gpt-5.4-mini",
            )
            env.update({key: "offline" for key in ("RG", "LOCATION", "ACR", "AKS", "AOAI", "TAG")})
            env.update(overrides or {})
            # Probe's model lookup must succeed before the independent cluster check.
            source = stub.read_text().replace(
                "if name == 'az' and '--query' in args",
                "if name == 'az' and 'properties.model.name' in args:\n    print('gpt-5.4-mini')\n"
                "elif name == 'az' and '--query' in args",
            )
            stub.write_text(source)
            result = subprocess.run(
                ["bash", str(CHART / "scripts/aks.sh"), command],
                env=env,
                capture_output=True,
                text=True,
            )
            kube_calls = [
                json.loads(line) for line in (state / "kube-calls.jsonl").read_text().splitlines()
            ]
            operations_path = state / "operations.jsonl"
            operations = (
                [json.loads(line) for line in operations_path.read_text().splitlines()]
                if operations_path.exists()
                else []
            )
            self.assertEqual(ambient.read_text(), original)
            self.assertEqual(list((home / ".kube").iterdir()), [ambient])
            config = Path(kube_calls[0]["config"])
            self.assertNotEqual(config, ambient)
            self.assertEqual(config.parent.parent, state)
            self.assertFalse(config.parent.exists(), "temporary credentials must be removed")
            credentials = kube_calls[0]["args"]
            self.assertEqual(credentials[:2], ["aks", "get-credentials"])
            self.assertEqual(credentials[credentials.index("--file") + 1], str(config))
            self.assertEqual(credentials[credentials.index("-g") + 1], "offline")
            self.assertEqual(credentials[credentials.index("-n") + 1], "offline")
            self.assertTrue(all(c["config"] == str(config) for c in kube_calls))
            self.assertTrue(
                all(
                    c["config"] == str(config)
                    or (c["tool"] == "az" and "properties.model.name" in c["args"])
                    for c in operations
                )
            )
            for operation in operations:
                if operation["tool"] == "helm":
                    self.assertIsNone(operation["helm_server"])
                    self.assertIsNone(operation["helm_context"])
            return result, operations

    def test_every_cluster_command_refuses_a_foreign_api_server(self):
        for command in (
            "install",
            "register",
            "turn",
            "probe",
            "egress-start",
            "egress-check",
            "teardown",
        ):
            with self.subTest(command=command):
                result, operations = self.run_cluster_command(
                    command, {"CREDENTIAL_SERVER": "https://foreign.example.invalid"}
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("API server does not match", result.stderr)
                # Probe's read-only model metadata lookup is permitted; no workload call is.
                self.assertTrue(
                    all(
                        c["tool"] == "az" and "properties.model.name" in c["args"]
                        for c in operations
                    )
                )

    def test_teardown_accepts_only_verified_public_private_or_portal_server(self):
        for field in ("fqdn", "privateFqdn", "azurePortalFqdn"):
            with self.subTest(field=field):
                result, operations = self.run_cluster_command(
                    "teardown", {"AKS_METADATA": json.dumps({field: "pilot.example.invalid"})}
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(operations[0]["tool"], "helm")
                self.assertEqual(operations[0]["args"][:2], ["uninstall", "coke"])
                self.assertEqual(operations[1]["tool"], "kubectl")
                self.assertEqual(operations[1]["args"][:3], ["delete", "namespace", "daimon"])
                self.assertTrue(any(c["args"][:2] == ["aks", "delete"] for c in operations))

    def test_teardown_refuses_missing_metadata_insecure_server_and_credential_failure(self):
        for overrides in (
            {"AKS_METADATA": "{}"},
            {"CREDENTIAL_SERVER": "http://pilot.example.invalid"},
            {"CREDENTIAL_EXIT": "1"},
        ):
            with self.subTest(overrides=overrides):
                result, operations = self.run_cluster_command("teardown", overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(operations, [])


class LiveProbeTests(unittest.TestCase):
    def run_probe(self, env=None, invalid=None):
        requests = []
        marker = None

        def response(req, **kwargs):
            nonlocal marker
            body = json.loads(req.data)
            requests.append(body)
            turn = len(requests)
            if turn == 1:
                marker = body["input"][0]["text"].split("containing exactly ")[1].split(",")[0]
            name = "write" if turn == 1 else "read"
            args = {"path": "daimon-aks-proof"}
            if turn == 1:
                args["content"] = marker
            result = marker if turn == 2 else "written"
            if turn == 2 and invalid == "unrelated":
                name, args, result = "bash", {"command": "true"}, "file-does-not-exist"
            if turn == 2 and invalid == "wrong-marker":
                result = "daimon-aks-proof-stale"
            call = "call-" + str(turn)
            events = [
                {"type": "model_request_started", "data": {"request_id": str(turn)}},
                {"type": "tool_use", "data": {"call_id": call, "name": name, "input": args}},
                {
                    "type": "tool_result",
                    "data": {"call_id": call, "output": result, "is_error": False},
                },
                {"type": "usage", "data": {"request_id": str(turn)}},
                {"type": "turn_ended", "data": {"outcome": "completed"}},
            ]
            if turn == 2 and invalid == "wrong-call":
                events[2]["data"]["call_id"] = "unrelated-call"
            if turn == 1 and invalid == "wrong-write":
                args["content"] = "wrong"
            return io.BytesIO("".join("data: " + json.dumps(e) + "\n\n" for e in events).encode())

        values = {
            "DAIMON_SELFHOSTED__HARNESS_TOKEN_PRIMARY": "offline-placeholder",
            "AOAI_VERIFIED_MODEL": "gpt-5.4-mini",
            **(env or {}),
        }
        values = {key: value for key, value in values.items() if value is not None}
        with (
            patch.dict(os.environ, values, clear=True),
            patch("urllib.request.urlopen", side_effect=response) as network,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            try:
                runpy.run_path(str(CHART / "scripts/live-c3.py"), run_name="__main__")
            finally:
                if env and (
                    "sol" in str(env).lower()
                    or env.get("SELFHOSTED_MODEL") == "other"
                    or values.get("AOAI_VERIFIED_MODEL") != "gpt-5.4-mini"
                ):
                    network.assert_not_called()
        return requests

    def test_probe_refuses_unconfigured_and_sol_models_before_network(self):
        for env in (
            {"SELFHOSTED_MODEL": "gpt-6.1-sol"},
            {"SELFHOSTED_MODEL": "other"},
            {"SELFHOSTED_MODEL": "custom-SOL", "AOAI_DEPLOYMENT": "custom-SOL"},
        ):
            with self.subTest(env=env), self.assertRaises(SystemExit):
                self.run_probe(env)

    def test_direct_probe_requires_verified_underlying_model_before_http(self):
        for verified in (None, "", "gpt-6.1-sol", "GPT-6.1-SOL", "gpt-6-luna"):
            with self.subTest(verified=verified), self.assertRaises(SystemExit):
                self.run_probe({"AOAI_DEPLOYMENT": "production", "AOAI_VERIFIED_MODEL": verified})

    def test_probe_uses_configured_deployment_and_exact_persisted_marker(self):
        for env, model in (
            ({}, "gpt-5.4-mini"),
            ({"AOAI_DEPLOYMENT": "mini-deployment"}, "mini-deployment"),
        ):
            requests = self.run_probe(env)
            self.assertEqual([r["model"] for r in requests], [model, model])
            self.assertEqual(requests[0]["session_id"], requests[1]["session_id"])

    def test_probe_rejects_unrelated_success_and_inexact_persistence(self):
        for invalid in ("unrelated", "wrong-marker", "wrong-call", "wrong-write"):
            with self.subTest(invalid=invalid), self.assertRaises(AssertionError):
                self.run_probe(invalid=invalid)

    def test_aks_defaults_selfhosted_model_to_aoai_deployment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            kubectl = root / "kubectl"
            kubectl.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$SELFHOSTED_MODEL" "$AOAI_DEPLOYMENT" > "$AKS_STATE/models"\n'
            )
            kubectl.chmod(0o755)
            az = root / "az"
            az.write_text('#!/bin/sh\nprintf "gpt-5.4-mini\\n"\n')
            add_kube_stub_support(az)
            add_kube_stub_support(kubectl)
            az.chmod(0o755)
            env = dict(
                os.environ,
                PATH=str(root) + os.pathsep + os.environ["PATH"],
                AKS_STATE=directory,
                AOAI_DEPLOYMENT="configured-mini",
            )
            env.pop("SELFHOSTED_MODEL", None)
            env.update({key: "offline" for key in ("RG", "LOCATION", "ACR", "AKS", "AOAI", "TAG")})
            subprocess.run(
                ["bash", str(CHART / "scripts/aks.sh"), "probe"],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                (root / "models").read_text().splitlines(), ["configured-mini", "configured-mini"]
            )

    def test_aks_probe_checks_underlying_model_before_kubectl(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            az = root / "az"
            az.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$*" >> "$AKS_STATE/az-calls"\nprintf "%s\\n" "$UNDERLYING_MODEL"\nexit "${AZ_EXIT:-0}"\n'
            )
            az.chmod(0o755)
            kubectl = root / "kubectl"
            kubectl.write_text('#!/bin/sh\nprintf "%s\\n" "$*" > "$AKS_STATE/kubectl-call"\n')
            add_kube_stub_support(az)
            add_kube_stub_support(kubectl)
            kubectl.chmod(0o755)
            for underlying, az_exit in (
                ("gpt-6.1-sol", "0"),
                ("gpt-6-luna", "0"),
                ("", "0"),
                ("gpt-5.4-mini", "1"),
                ("gpt-5.4-mini", "0"),
            ):
                with self.subTest(underlying=underlying, az_exit=az_exit):
                    env = dict(
                        os.environ,
                        PATH=str(root) + os.pathsep + os.environ["PATH"],
                        AKS_STATE=directory,
                        AOAI_DEPLOYMENT="production",
                        SELFHOSTED_MODEL="production",
                        AOAI_VERIFIED_MODEL="gpt-5.4-mini",
                        UNDERLYING_MODEL=underlying,
                        AZ_EXIT=az_exit,
                    )
                    env.update(
                        {key: "offline" for key in ("RG", "LOCATION", "ACR", "AKS", "AOAI", "TAG")}
                    )
                    result = subprocess.run(
                        ["bash", str(CHART / "scripts/aks.sh"), "probe"],
                        env=env,
                        capture_output=True,
                        text=True,
                    )
                    allowed = underlying == "gpt-5.4-mini" and az_exit == "0"
                    self.assertEqual(result.returncode, 0 if allowed else 1, result.stderr)
                    call = (root / "az-calls").read_text().splitlines()[-1]
                    self.assertIn(
                        "--deployment-name production --query properties.model.name -o tsv", call
                    )
                    self.assertEqual((root / "kubectl-call").exists(), allowed)
                    if allowed:
                        self.assertIn(
                            "AOAI_VERIFIED_MODEL=gpt-5.4-mini", (root / "kubectl-call").read_text()
                        )

    def test_aks_probe_refuses_before_kubectl(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            kubectl = root / "kubectl"
            kubectl.write_text('#!/bin/sh\ntouch "$AKS_STATE/network-called"\nexit 90\n')
            kubectl.chmod(0o755)
            for model, deployment in (("other", "gpt-5.4-mini"), ("gpt-6.1-sol", "gpt-6.1-sol")):
                env = dict(
                    os.environ,
                    PATH=str(root) + os.pathsep + os.environ["PATH"],
                    AKS_STATE=directory,
                    SELFHOSTED_MODEL=model,
                    AOAI_DEPLOYMENT=deployment,
                )
                env.update(
                    {key: "offline" for key in ("RG", "LOCATION", "ACR", "AKS", "AOAI", "TAG")}
                )
                result = subprocess.run(
                    ["bash", str(CHART / "scripts/aks.sh"), "probe"],
                    env=env,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 1)
                self.assertIn("Sol models are refused", result.stderr)
                self.assertFalse((root / "network-called").exists())


if __name__ == "__main__":
    unittest.main()
