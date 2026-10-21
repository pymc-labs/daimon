#!/usr/bin/env bash
# Build images with PR #1276 first; --reuse needs an explicit KIND_CLUSTER_NAME.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
chart=deploy/helm/daimon-selfhost
scratch=$(mktemp -d)
cluster=${KIND_CLUSTER_NAME:-daimon-l14-$(date +%s)-$$}
cleanup() {
  kind delete cluster --name "$cluster"
  rm -r "$scratch"
}
trap cleanup EXIT
export KUBECONFIG="$scratch/kubeconfig"
if [[ ${1:-} == --reuse ]]; then
  kind get kubeconfig --name "$cluster" > "$KUBECONFIG"
else
  kind create cluster --name "$cluster" --image kindest/node:v1.35.0 --wait 120s
fi
kind load docker-image daimon-l14:local daimon-harness-l14:local --name "$cluster"
kubectl create namespace daimon
kubectl label namespace daimon pod-security.kubernetes.io/enforce=privileged
# Dummy local credentials, never Azure or platform credentials.
python3 - "$scratch" <<'PY'
import base64
import os
import secrets
import sys
from pathlib import Path
p = Path(sys.argv[1])
password = secrets.token_hex(24)
(p/'pg.env').write_text('POSTGRES_PASSWORD='+password+'\n')
(p/'host.env').write_text(
    'DAIMON_DATABASE__URL=postgresql+asyncpg://daimon:'+password+'@smoke-daimon-postgres:5432/daimon\n'
    'DAIMON_MCP__JWT_SECRET='+secrets.token_hex(32)+'\n'
    'DAIMON_CRYPTO__KEYS='+base64.urlsafe_b64encode(os.urandom(32)).decode()+'\n'
    'DAIMON_MCP__PUBLIC_URL=https://mcp.example.invalid\n'
    'DAIMON_TEAMS__CLIENT_ID=00000000-0000-0000-0000-000000000001\n'
    'DAIMON_TEAMS__CLIENT_SECRET=offline-placeholder\n'
    'DAIMON_TEAMS__TENANT_ID=00000000-0000-0000-0000-000000000002\n'
    'DAIMON_TEAMS__ENABLED=false\n'
    'DAIMON_SELFHOSTED__HARNESS_TOKEN_PRIMARY=offline-kind-only\n'
    'DAIMON_ANTHROPIC__BASE_URL=http://127.0.0.1:9\n')
(p/'harness.env').write_text('HARNESS_TOKEN=offline-kind-only\n')
PY
for pair in postgres:pg host:host harness:harness; do
  kubectl -n daimon create secret generic "daimon-${pair%:*}" --from-env-file="$scratch/${pair#*:}.env"
done
cat > "$scratch/local.yaml" <<'YAML'
images:
  host: {repository: daimon-l14, tag: local, pullPolicy: Never}
  harness: {repository: daimon-harness-l14, tag: local, pullPolicy: Never}
azureOpenAI: {deployment: gpt-6-luna}
postgres: {storage: 1Gi}
harness: {storage: 1Gi}
YAML
helm lint "$chart"
helm template smoke "$chart" -f "$scratch/local.yaml" > "$scratch/rendered.yaml"
if command -v kubeconform >/dev/null; then
  kubeconform -strict -summary "$scratch/rendered.yaml"
else
  echo 'kubeconform unavailable; API server dry-run after install validates schema'
fi
kubectl -n daimon create configmap offline-harness --from-file="$chart/tests/fake.py"
cat > "$scratch/postrender" <<'RENDER'
#!/usr/bin/env bash
set -euo pipefail
exec uv run python deploy/helm/daimon-selfhost/tests/kind-postrender.py
RENDER
chmod +x "$scratch/postrender"
# One fresh release, all hosts and Job together; PG intentionally starts late.
helm install smoke "$chart" -n daimon -f "$scratch/local.yaml" \
  --post-renderer "$scratch/postrender" --wait --wait-for-jobs --timeout 15m
kubectl -n daimon wait --for=condition=complete job/smoke-daimon-init-1 --timeout=5s
helm status smoke -n daimon -o json | python3 -c \
  'import json,sys; s=json.load(sys.stdin); assert s["version"] == 1 and s["info"]["status"] == "deployed"; print("First install: revision 1 deployed")'
kubectl -n daimon get pods -o json | python3 -c \
  'import json,sys; pods=json.load(sys.stdin)["items"]; assert all(c["restartCount"] == 0 for p in pods for c in p["status"].get("containerStatuses", [])); print("No main-container restarts during delayed-Postgres install")'
kubectl -n daimon logs job/smoke-daimon-init-1 | tail -5
kubectl -n daimon wait --for=condition=ready pod --field-selector=status.phase!=Succeeded --timeout=300s
kubectl -n daimon get pods
kubectl -n daimon exec deployment/smoke-daimon-mcp -- python -c \
  'import urllib.request; print(urllib.request.urlopen("http://localhost:8765/healthz").read().decode())'
pod_scratch=$(kubectl -n daimon exec deployment/smoke-daimon-mcp -- mktemp -d)
kubectl -n daimon exec -i deployment/smoke-daimon-mcp -- python - before "$pod_scratch/evidence.json" < "$chart/tests/probe.py"
kubectl -n daimon logs smoke-daimon-harness-0 --tail=5
kubectl -n daimon exec smoke-daimon-harness-0 -- sh -c 'cat /proc/mounts | head -5'
kubectl -n daimon exec smoke-daimon-harness-0 -- python -c \
  'from pathlib import Path; assert Path("/home/harness/executions").read_text().splitlines() == ["kind-one", "kind-two"]'
# Same PVC, new process: completed turns replay without executing tools again.
kubectl -n daimon delete pod smoke-daimon-harness-0
kubectl -n daimon rollout status statefulset/smoke-daimon-harness --timeout=120s
kubectl -n daimon exec -i deployment/smoke-daimon-mcp -- python - after "$pod_scratch/evidence.json" < "$chart/tests/probe.py"
kubectl -n daimon exec smoke-daimon-harness-0 -- python -c \
  'from pathlib import Path; assert Path("/home/harness/executions").read_text().splitlines() == ["kind-one", "kind-two", "kind-one-read", "kind-two-read"]; print("Replay did not execute; new turns read persisted files")'
# Exercise the exact recipe registration with #1276 application images, no model I/O.
uv run python - "$scratch/local.yaml" "$scratch/aks-values.json" <<'PYLOCAL'
import json, sys, yaml
from pathlib import Path
Path(sys.argv[2]).write_text(json.dumps(yaml.safe_load(Path(sys.argv[1]).read_text())))
PYLOCAL
AKS_STATE="$scratch" RELEASE=smoke SELFHOSTED_MODEL=gpt-6-luna \
  SELFHOSTED_MODELS='{"gpt-6-luna-TEST":[{"effective_from":"2026-10-21","input":"0.75","output":"4.5","cache_read":"0.075"}]}' \
  POST_RENDERER="$scratch/postrender" bash "$chart/scripts/register.sh"
tenant=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["tenant_id"])' "$scratch/registration.json")
for message in write read; do
  kubectl -n daimon exec deploy/smoke-daimon-mcp -- daimon selfhosted turn \
    --tenant-id "$tenant" --thread kind-recipe --message "$message" > "$scratch/turn-$message.ndjson"
  python3 - "$scratch/turn-$message.ndjson" <<'PYTURN'
import json, sys
rows = [json.loads(line) for line in open(sys.argv[1]) if line.startswith('{')]
assert rows, 'No CLI lifecycle events'
print('Metered CLI turn returned lifecycle events')
PYTURN
done
kubectl -n daimon exec -i deploy/smoke-daimon-mcp -- python - < "$chart/tests/verify-ledger.py"
# kindnet (kind default) does not enforce NetworkPolicy. Do not claim enforcement.
echo 'NetworkPolicy enforcement NOT tested: kindnet CNI; requires Calico/Cilium/AKS policy engine'
kubectl apply --server-side --dry-run=server --force-conflicts -n daimon -f "$scratch/rendered.yaml" >/dev/null
