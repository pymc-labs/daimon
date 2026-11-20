"""Eval pod specs: what each runtime hands a pod, and what it never does."""

from __future__ import annotations

import io
import json
import tarfile
import uuid
from datetime import UTC, datetime
from functools import cache

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from daimon.core.evals.sandbox.ca import EvalCA, eval_ca
from daimon.core.evals.sandbox.docker import demux
from daimon.core.evals.sandbox.kubernetes import exit_code, pod_manifest, secret_manifest
from daimon.core.evals.sandbox.parity import missing_packages
from daimon.core.evals.sandbox.runtime import (
    PodFile,
    PodSpec,
    SandboxError,
    gateway_config,
    main_config,
    main_tar,
)
from daimon.core.evals.world.model import ItemRef
from daimon.core.specs import EnvironmentSpec
from daimon.testing.eval_world import eval_case


@cache
def fixture_ca() -> EvalCA:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return eval_ca(pem.decode())


def _spec(**extra: object) -> PodSpec:
    item = ItemRef(uuid.uuid4(), uuid.uuid4(), eval_case({}), "0" * 64)
    fields: dict[str, object] = {
        "item": item, "image": "img@sha256:1", "executor": "anthropic", "world_url": "http://w",
        "world_token": "wt", "ca": fixture_ca(), **extra,
    }  # fmt: skip
    return PodSpec(**fields)  # type: ignore[arg-type]


def test_executor_secrets_stay_out_of_the_agent_and_gateway() -> None:
    spec = _spec(executor_config={"ANTHROPIC_ENVIRONMENT_KEY": "sk-env"}, env={"GH_TOKEN": "fake"})
    main = json.loads(main_config(spec))
    assert main["exec_env"] == {"ANTHROPIC_ENVIRONMENT_KEY": "sk-env"}, "executor env carried"
    assert main["agent_env"] == {"GH_TOKEN": "fake"}, "agent env holds only the case's vars"
    assert b"sk-env" not in gateway_config(spec), "the gateway never sees executor secrets"
    manifest = json.dumps(pod_manifest(spec, "eval-x"))
    assert "sk-env" not in manifest, "secrets travel in the Secret, never the pod spec"


def test_gateway_tunnels_only_the_executors_control_plane_and_admin_hosts() -> None:
    spec = _spec(executor="openai", passthrough=frozenset({"mcp.example.com"}))
    hosts = json.loads(gateway_config(spec))["control_plane"]
    assert hosts == ["api.openai.com", "codex-cloud-environments.chatgpt.com", "mcp.example.com"], (
        "control plane plus admin hosts, nothing else"
    )


def test_kubernetes_pod_has_no_token_and_main_has_no_network_caps() -> None:
    pod = pod_manifest(_spec(), "eval-x")["spec"]
    assert isinstance(pod, dict), "pod spec is an object"
    assert pod["automountServiceAccountToken"] is False, "no cluster credential in the pod"
    containers = {c["name"]: c for c in pod["containers"]}  # type: ignore[union-attr,index]
    main_caps = containers["main"]["securityContext"]["capabilities"]["add"]  # type: ignore[index]
    assert not {"NET_ADMIN", "NET_RAW"} & set(main_caps), "main cannot change the packet rules"
    assert containers["gateway"]["securityContext"]["runAsUser"] == 61000, "gateway uid"  # type: ignore[index]
    data = secret_manifest(_spec(), "eval-x")["data"]
    assert isinstance(data, dict) and set(data) == {
        "gateway-config.json", "gateway-ca.pem", "main-pod.json", "main-ca.crt"
    }, "one Secret carries both containers' files"  # fmt: skip


def test_workspace_files_land_as_the_agent_and_end_with_the_ready_marker() -> None:
    files = [PodFile("/workspace/repo/a.txt", b"hi")]
    with tarfile.open(fileobj=io.BytesIO(main_tar(_spec(files=files), configs=False))) as tar:
        members = tar.getmembers()
    by_name = {m.name: m for m in members}
    assert by_name["workspace/repo/a.txt"].uid == 10001, "files belong to the agent"
    assert by_name["workspace/repo"].isdir(), "parent directories are created"
    assert members[-1].name == "run/eval/state/ready", "the ready marker comes last"
    with pytest.raises(SandboxError):
        main_tar(_spec(files=[PodFile("/etc/passwd", b"x")]), configs=False)


def test_eval_ca_is_stable_and_covers_past_clocks() -> None:
    cert = x509.load_pem_x509_certificate(fixture_ca().cert_pem)
    assert cert.not_valid_before_utc <= datetime(2001, 1, 1, tzinfo=UTC), "valid for past clocks"
    constraints = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert constraints.ca, "it is a CA"


def test_exec_streams_and_status() -> None:
    stream = b"\x01\x00\x00\x00\x00\x00\x00\x02ok\x02\x00\x00\x00\x00\x00\x00\x03bad"
    assert demux(stream) == (b"ok", b"bad"), "docker stdout and stderr split"
    failed = {"status": "Failure", "details": {"causes": [{"reason": "ExitCode", "message": "3"}]}}
    assert exit_code(failed) == 3 and exit_code({"status": "Success"}) == 0, "kubernetes exit codes"


def test_parity_names_missing_packages() -> None:
    spec = EnvironmentSpec.model_validate({
        "name": "d", "config": {"type": "cloud", "packages": {"apt": ["gh"], "pip": ["duckdb==1.1"]}}
    })  # fmt: skip
    assert missing_packages(spec, {"apt": ["gh"], "pip": ["duckdb"]}) == [], "matched"
    assert missing_packages(spec, {"apt": [], "pip": []}) == ["apt:gh", "pip:duckdb"], "drift"
