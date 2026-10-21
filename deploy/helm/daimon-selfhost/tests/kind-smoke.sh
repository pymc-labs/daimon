#!/usr/bin/env bash
# Build images first; --reuse accepts only this lane's already-created cluster.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
chart=deploy/helm/daimon-selfhost
scratch=$(mktemp -d)
cluster=daimon-l14
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
helm install smoke "$chart" -n daimon -f "$scratch/local.yaml" \
  --set services.enabled=false --set harness.enabled=false
kubectl -n daimon wait --for=condition=complete job/smoke-daimon-init-1 --timeout=300s
kubectl -n daimon logs job/smoke-daimon-init-1 | tail -5
helm upgrade smoke "$chart" -n daimon -f "$scratch/local.yaml" --set init.enabled=false
kubectl -n daimon create configmap offline-harness --from-file="$chart/tests/fake.py"
cat > "$scratch/fake-patch.yaml" <<'YAML'
spec:
  template:
    spec:
      volumes:
        - name: offline
          configMap: {name: offline-harness, defaultMode: 256}
      containers:
        - name: harness
          args:
            - mount -o remount,hidepid=2,gid=60002 /proc; chmod 755 /tmp /var/tmp /dev/shm /run/lock; chmod 600 /dev/termination-log; chown 0:0 /home/harness; chmod 711 /home/harness; exec python /validation/fake.py
          volumeMounts:
            - {name: offline, mountPath: /validation, readOnly: true}
YAML
kubectl -n daimon patch statefulset smoke-daimon-harness --type=strategic --patch-file "$scratch/fake-patch.yaml"
# OrderedReady can hold a failed original pod rather than replace it with the
# fixture revision. Delete exactly that pod after patching, preserving its PVC.
kubectl -n daimon delete pod smoke-daimon-harness-0
kubectl -n daimon rollout status statefulset/smoke-daimon-harness --timeout=300s
kubectl -n daimon wait --for=condition=ready pod --all --timeout=300s
kubectl -n daimon get pods
kubectl -n daimon exec deployment/smoke-daimon-mcp -- python -c \
  'import urllib.request; print(urllib.request.urlopen("http://localhost:8765/healthz").read().decode())'
kubectl -n daimon exec -i deployment/smoke-daimon-mcp -- python - < "$chart/tests/probe.py"
kubectl -n daimon logs smoke-daimon-harness-0 | head -5
kubectl -n daimon exec smoke-daimon-harness-0 -- sh -c 'cat /proc/mounts | head -5'
# Same PVC, new process: completed turns replay without executing tools again.
kubectl -n daimon delete pod smoke-daimon-harness-0
kubectl -n daimon rollout status statefulset/smoke-daimon-harness --timeout=120s
kubectl -n daimon exec -i deployment/smoke-daimon-mcp -- python - < "$chart/tests/probe.py"
# kindnet (kind default) does not enforce NetworkPolicy. Do not claim enforcement.
echo 'NetworkPolicy enforcement NOT tested: kindnet CNI; requires Calico/Cilium/AKS policy engine'
kubectl apply --server-side --dry-run=server --force-conflicts -n daimon -f "$scratch/rendered.yaml" >/dev/null
