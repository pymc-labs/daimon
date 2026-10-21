#!/usr/bin/env bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
repo=$PWD
scratch=$(mktemp -d)
cluster="daimon-pods-smoke-$$"
image="daimon-harness:pods-smoke-$$"
base=${HARNESS_SMOKE_BASE:-daimon-harness:local}
forward_pid=
cleanup() {
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
DOCKER
docker build --build-arg "BASE=$base" -f "$scratch/Dockerfile" -t "$image" .
chart_head=e9f7b0414baa7fd9e96284b447bd80f74bdd622b
if ! git cat-file -e "$chart_head"; then git fetch origin "$chart_head"; fi
git archive "$chart_head" deploy/helm/daimon-selfhost | tar -x -C "$scratch"
patch -p1 -d "$scratch" < deploy/helm/patches/per-conversation.patch
export KUBECONFIG="$scratch/kubeconfig"
kind create cluster --name "$cluster" --image kindest/node:v1.35.0 --wait 120s
kind load docker-image "$image" --name "$cluster"
kubectl create namespace daimon-pods
kubectl create secret generic daimon-harness -n daimon-pods --from-literal=HARNESS_TOKEN=offline-l16
helm template smoke "$scratch/deploy/helm/daimon-selfhost" -n daimon-pods \
  --set harness.mode=perConversation --set images.harness.repository=daimon-harness \
  --set "images.harness.tag=pods-smoke-$$" --set images.harness.pullPolicy=Never \
  --set harness.storage=20Gi --set services.enabled=false --set init.enabled=false \
  --set postgres.bundled=false | uv run python apps/harness/tests/pods/render.py --kind > "$scratch/rendered.yaml"
kubectl apply -f "$scratch/rendered.yaml"
kubectl rollout status -n daimon-pods deployment/smoke-daimon-harness --timeout=180s
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
uv run python "$repo/apps/harness/tests/pods/secure_probe.py" --ca "$scratch/ca.crt" --output "$scratch/receipt.json"
kubectl delete pods -n smoke-sandboxes -l daimon-controller=smoke-daimon-harness --wait=true
uv run python "$repo/apps/harness/tests/pods/secure_probe.py" --resume --ca "$scratch/ca.crt" --output "$scratch/resume.json"
echo "Evidence: $scratch (kindnet does not enforce NetworkPolicy)"
