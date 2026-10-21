#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
repo=$PWD
scratch=$(mktemp -d)
echo "Scratch: $scratch"
cluster="daimon-pods-smoke-$$"
image="daimon-harness:pods-smoke-$$"
base=${HARNESS_SMOKE_BASE:-daimon-harness:local}
forward_pid=
cleanup() {
  if [[ -f "$scratch/kubeconfig" ]]; then
    kubectl -n daimon-pods logs deploy/smoke-daimon-harness --tail=60 > "$scratch/controller.log" 2>&1 || true
    kubectl -n smoke-sandboxes logs -l daimon-controller=smoke-daimon-harness --all-containers --tail=60 > "$scratch/sandbox.log" 2>&1 || true
    kubectl -n daimon-pods logs deploy/smoke-daimon-mcp --tail=40 > "$scratch/mcp.log" 2>&1 || true
    kubectl -n smoke-sandboxes get events > "$scratch/events.log" 2>&1 || true
  fi
  if [[ -n "$forward_pid" ]]; then kill "$forward_pid" 2>/dev/null || true; fi
  kind delete cluster --name "$cluster"
  docker image rm "$image" >/dev/null 2>&1 || true
}
trap cleanup EXIT
# Build the production base first with apps/harness/Dockerfile, or supply an
# existing equivalent image via HARNESS_SMOKE_BASE. No real credential is needed.
docker image inspect "$base" >/dev/null
cat > "$scratch/Dockerfile" <<'DOCKER'
ARG BASE
FROM ${BASE}
COPY apps/harness/src/daimon_harness /opt/venv/lib/python3.12/site-packages/daimon_harness
COPY apps/harness/tests/pods/fake.py /opt/l16-smoke.py
COPY apps/harness/tests/pods/mcp.py /opt/l16-mcp.py
DOCKER
docker build --build-arg "BASE=$base" -f "$scratch/Dockerfile" -t "$image" .
chart_head=473a53a2ad0e315314c53d2615243ab46645a6f5
if ! git cat-file -e "$chart_head"; then git fetch origin "$chart_head"; fi
git archive "$chart_head" deploy/helm/daimon-selfhost | tar -x -C "$scratch"
patch -p1 -d "$scratch" < deploy/helm/patches/per-conversation.patch
export KUBECONFIG="$scratch/kubeconfig"
cat > "$scratch/kind.yaml" <<'KIND'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
kubeadmConfigPatches:
  - |
    kind: KubeProxyConfiguration
    conntrack:
      maxPerCore: 0
KIND
kind create cluster --name "$cluster" --config "$scratch/kind.yaml" --image kindest/node:v1.35.0 --wait 120s
# Avoid a host inotify watcher limit without changing host sysctls.
kubectl -n kube-system patch ds kube-proxy --type=json -p='[{"op":"replace","path":"/spec/template/spec/containers/0/command","value":["/usr/local/bin/kube-proxy","--cluster-cidr=10.244.0.0/16","--conntrack-max-per-core=0"]}]'
kind load docker-image "$image" --name "$cluster"
kubectl create namespace daimon-pods
kubectl create secret generic daimon-harness -n daimon-pods --from-literal=HARNESS_TOKEN=offline-l16
export HARNESS_SMOKE_IMAGE="$image"
helm template smoke "$scratch/deploy/helm/daimon-selfhost" -n daimon-pods \
  --set harness.mode=perConversation --set images.harness.repository=daimon-harness \
  --set "images.harness.tag=pods-smoke-$$" --set images.harness.pullPolicy=Never \
  --set harness.storage=20Gi --set harness.perConversation.maxSandboxes=1 --set init.enabled=false \
  --set postgres.host=unused --set services.teams.enabled=false --set services.scheduler.enabled=false \
  --set services.discord.enabled=false --set services.slack.enabled=false \
  --set postgres.bundled=false | uv run python apps/harness/tests/pods/render.py --kind > "$scratch/rendered.yaml"
kubectl apply -f "$scratch/rendered.yaml"
kubectl rollout status -n daimon-pods deployment/smoke-daimon-harness --timeout=180s
kubectl rollout status -n daimon-pods deployment/smoke-daimon-mcp --timeout=180s
kubectl port-forward -n daimon-pods service/smoke-daimon-harness 18088:8088 > "$scratch/forward.log" 2>&1 &
forward_pid=$!
uv run python - <<'PYREADY'
import socket,time
for _ in range(100):
    try:
        socket.create_connection(("127.0.0.1",18088),.2).close()
        break
    except OSError:
        time.sleep(.1)
else:
    raise RuntimeError("port forward not ready")
PYREADY
kubectl get secret -n daimon-pods smoke-daimon-mcp-tls -o jsonpath='{.data.ca\.crt}' | base64 -d > "$scratch/ca.crt"
export HARNESS_PROBE_TOKEN=offline-l16
for attempt in {1..120}; do
  if kubectl wait -n smoke-sandboxes --for=condition=Ready pod -l daimon-session=warm --timeout=1s >/dev/null 2>&1; then break; fi
  sleep 1
done
uv run python "$repo/apps/harness/tests/pods/secure_probe.py" --ca "$scratch/ca.crt" --output "$scratch/receipt.json"
kubectl delete pods -n smoke-sandboxes -l daimon-controller=smoke-daimon-harness --wait=true
for attempt in {1..120}; do
  if kubectl wait -n smoke-sandboxes --for=condition=Ready pod -l daimon-session=warm --timeout=1s >/dev/null 2>&1; then break; fi
  sleep 1
done
uv run python "$repo/apps/harness/tests/pods/secure_probe.py" --resume --ca "$scratch/ca.crt" --output "$scratch/resume.json"
echo "Evidence: $scratch (kindnet does not enforce NetworkPolicy)"
