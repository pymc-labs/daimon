"""Enclosed eval sandboxes: one pod per item, on a Docker host or in Kubernetes."""

from __future__ import annotations

from daimon.core.config import EvalsSettings
from daimon.core.evals.sandbox.ca import EvalCA, EvalCAError, eval_ca
from daimon.core.evals.sandbox.docker import DockerRuntime, docker_client
from daimon.core.evals.sandbox.kubernetes import KubernetesRuntime
from daimon.core.evals.sandbox.runtime import SandboxError, SandboxRuntime


def sandbox_runtime(settings: EvalsSettings) -> SandboxRuntime:
    match settings.sandbox_runtime:
        case "docker":
            return DockerRuntime(docker_client(settings), network=settings.docker_network)
        case "kubernetes":
            return KubernetesRuntime(settings.kubernetes_namespace)
        case None:
            raise SandboxError("eval sandboxes are off: set DAIMON_EVALS__SANDBOX_RUNTIME")


def configured_ca(settings: EvalsSettings) -> EvalCA:
    if settings.ca_key is None:
        raise EvalCAError("no eval CA: set DAIMON_EVALS__CA_KEY")
    return eval_ca(settings.ca_key.get_secret_value())
