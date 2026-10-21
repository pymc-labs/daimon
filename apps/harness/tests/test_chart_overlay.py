"""Render the shipped overlay against the exact chart revision in its recipe."""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

PIN = "473a53a2ad0e315314c53d2615243ab46645a6f5"
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def chart(tmp_path):
    archive = subprocess.run(
        ["git", "archive", PIN, "deploy/helm/daimon-selfhost"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    subprocess.run(["tar", "-x", "-C", str(tmp_path)], input=archive.stdout, check=True)
    subprocess.run(
        ["patch", "-p1", "-d", str(tmp_path)],
        input=(ROOT / "deploy/helm/patches/per-conversation.patch").read_bytes(),
        check=True,
        capture_output=True,
    )
    return tmp_path / "deploy/helm/daimon-selfhost"


def render(chart, *extra):
    return subprocess.run(
        [
            "helm",
            "template",
            "smoke",
            str(chart),
            "-n",
            "daimon-pods",
            "--set",
            "harness.mode=perConversation",
            *extra,
        ],
        capture_output=True,
        text=True,
    )


def test_chart_owned_urls_unique_env_and_liveness(chart):
    url = "https://smoke-daimon-mcp.daimon-pods.svc:8765/mcp"
    result = render(
        chart,
        "--set",
        f"harness.env.HARNESS_DAIMON_MCP_URL={url}",
        "--set",
        f"host.env.DAIMON_MCP__PUBLIC_URL={url}",
    )
    assert result.returncode == 0, result.stderr
    docs = list(yaml.safe_load_all(result.stdout))
    hosts = []
    sandbox = None
    for doc in docs:
        if not doc:
            continue
        if doc["kind"] == "ConfigMap" and "pod.json" in doc.get("data", {}):
            sandbox = json.loads(doc["data"]["pod.json"])["spec"]["containers"][0]
            containers = [sandbox]
        else:
            containers = (
                doc.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
            )
        for container in containers:
            env = container.get("env", [])
            names = [e["name"] for e in env]
            assert len(names) == len(set(names)), (container["name"], names)
            values = {e["name"]: e.get("value") for e in env}
            if "DAIMON_MCP__PUBLIC_URL" in values:
                hosts.append(values["DAIMON_MCP__PUBLIC_URL"])
            if container["name"] == "controller":
                assert container["livenessProbe"]["httpGet"]["path"] == "/liveness"
                assert values["HARNESS_IDLE_SECONDS"] == "300"
                assert values["HARNESS_MAX_SANDBOXES"] == "4"
    assert hosts and set(hosts) == {url}
    assert next(e["value"] for e in sandbox["env"] if e["name"] == "HARNESS_DAIMON_MCP_URL") == url


@pytest.mark.parametrize(
    "key",
    [
        "harness.env.HARNESS_DAIMON_MCP_URL",
        "host.env.DAIMON_MCP__PUBLIC_URL",
        "services.mcp.env.DAIMON_MCP__PUBLIC_URL",
    ],
)
def test_chart_rejects_registration_short_url(chart, key):
    result = render(chart, "--set", f"{key}=https://smoke-daimon-mcp:8765/mcp")
    assert result.returncode != 0
    assert "chart-owned" in result.stderr
