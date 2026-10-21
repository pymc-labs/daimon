#!/usr/bin/env bash
# Operator-run only. This script creates/deletes Azure resources when invoked.
set -euo pipefail
umask 077
cd "$(git rev-parse --show-toplevel)"
chart=deploy/helm/daimon-selfhost
: "${RG:?export RG}" "${LOCATION:?export LOCATION}" "${ACR:?export ACR}"
: "${AKS:?export AKS}" "${AOAI:?export AOAI}" "${TAG:?export TAG}"
: "${AKS_STATE:?export AKS_STATE to a private mktemp -d directory}"
[[ -d "$AKS_STATE" ]] || { echo 'AKS_STATE must exist' >&2; exit 1; }
case ${1:-} in
bootstrap)
  az group create -n "$RG" -l "$LOCATION" -o none
  az acr create -g "$RG" -n "$ACR" --sku Basic --admin-enabled false -o none
  az acr build -r "$ACR" -t "daimon:$TAG" . --no-logs -o none
  az acr build -r "$ACR" -t "daimon-harness:$TAG" -f apps/harness/Dockerfile . --no-logs -o none
  az acr import -n "$ACR" --source docker.io/library/postgres:18-alpine --image postgres:18-alpine --force -o none
  az acr repository show -n "$ACR" --image postgres:18-alpine --query digest -o tsv > "$AKS_STATE/postgres-digest"
  az aks create -g "$RG" -n "$AKS" --node-count 1 --node-vm-size Standard_D2s_v6 \
    --network-plugin azure --network-plugin-mode overlay --network-policy azure --attach-acr "$ACR" \
    --enable-managed-identity --generate-ssh-keys --tier free --node-osdisk-size 64 -o none
  az cognitiveservices account create -g "$RG" -n "$AOAI" -l "$LOCATION" \
    --kind OpenAI --sku S0 --custom-domain "$AOAI" -o none
  az cognitiveservices account deployment create -g "$RG" -n "$AOAI" \
    --deployment-name gpt-6-luna --model-name gpt-6-luna --model-version 2026-09-22 \
    --model-format OpenAI --sku-name DataZoneStandard --sku-capacity 1 -o none
  ;;
install)
  az aks get-credentials -g "$RG" -n "$AKS" --overwrite-existing -o none
  kubectl create namespace daimon --dry-run=client -o yaml | kubectl apply -f -
  # Explicit pilot admission exception. Obtain IT approval before running.
  kubectl label namespace daimon pod-security.kubernetes.io/enforce=privileged --overwrite
  python3 - "$AKS_STATE" "$RG" "$AOAI" "$ACR" "$TAG" <<'PY'
import base64
import json
import os
import secrets
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit
state, rg, aoai, acr, tag = sys.argv[1:]
p = Path(state)
def azure(*args):
    return subprocess.check_output(['az', *args, '-o', 'tsv'], text=True).strip()
endpoint = azure('cognitiveservices', 'account', 'show', '-g', rg, '-n', aoai,
                 '--query', 'properties.endpoint')
key = azure('cognitiveservices', 'account', 'keys', 'list', '-g', rg, '-n', aoai,
            '--query', 'key1')
if not (p/'pg.env').exists():
    password, token = secrets.token_hex(24), secrets.token_hex(32)
    (p/'pg.env').write_text('POSTGRES_PASSWORD='+password+'\n')
    (p/'harness.env').write_text('HARNESS_TOKEN='+token+'\n')
    (p/'host.env').write_text(
        'DAIMON_DATABASE__URL=postgresql+asyncpg://daimon:'+password+'@coke-daimon-postgres:5432/daimon\n'
        'DAIMON_MCP__JWT_SECRET='+secrets.token_hex(32)+'\n'
        'DAIMON_CRYPTO__KEYS='+base64.urlsafe_b64encode(os.urandom(32)).decode()+'\n'
        'DAIMON_MCP__PUBLIC_URL=https://mcp.example.invalid\n'
        'DAIMON_SELFHOSTED__HARNESS_TOKEN_PRIMARY='+token+'\n')
(p/'aoai.env').write_text('AZURE_OPENAI_API_KEY='+key+'\n')
host = urlsplit(endpoint).hostname
ips = sorted({r[4][0]+'/32' for r in socket.getaddrinfo(host,443,socket.AF_INET)})
assert ips, 'AOAI must resolve to IPv4 endpoint addresses'
values_path = p/'aks-values.json'
values = json.loads(values_path.read_text()) if values_path.exists() else {}
def merge(target, updates):
    for key, value in updates.items():
        if isinstance(value, dict):
            merge(target.setdefault(key, {}), value)
        else:
            target[key] = value
merge(values, {
    'images': {'registry': acr+'.azurecr.io', 'host': {'tag':tag}, 'harness': {'tag':tag}},
    'azureOpenAI': {'endpoint':endpoint, 'deployment':values.get('azureOpenAI', {}).get('deployment', 'gpt-6-luna'),
                    'apiKeySecretRef':{'name':'daimon-aoai','key':'AZURE_OPENAI_API_KEY'}},
    'services': {'teams':{'enabled':values.get('services', {}).get('teams', {}).get('enabled', False)}},
    'host': {'env': {'DAIMON_ANTHROPIC__BASE_URL':'http://127.0.0.1:9'}},
    'networkPolicy': {'azureOpenAICidrs':ips},
})
(p/'aks-values.json').write_text(json.dumps(values, indent=2)+'\n')
(p/'aoai-host').write_text(host)
PY
  for pair in postgres:pg host:host harness:harness aoai:aoai; do
    kubectl -n daimon create secret generic "daimon-${pair%:*}" \
      --from-env-file="$AKS_STATE/${pair#*:}.env" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  done
  runtime_secret=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("host", {}).get("runtimeSecret", ""))' "$AKS_STATE/aks-values.json")
  if [[ -n $runtime_secret ]]; then
    [[ -f "$AKS_STATE/runtime.json" ]] || { echo 'Saved registration requires AKS_STATE/runtime.json' >&2; exit 1; }
    kubectl -n daimon create secret generic "$runtime_secret" \
      --from-file=runtime.json="$AKS_STATE/runtime.json" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  fi
  helm upgrade --install coke "$chart" -n daimon -f "$AKS_STATE/aks-values.json" \
    --wait --wait-for-jobs --timeout 15m
  kubectl -n daimon rollout status statefulset/coke-daimon-harness --timeout=300s
  kubectl -n daimon rollout status deployment --timeout=300s
  kubectl -n daimon rollout status statefulset --timeout=300s
  mcp_port=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("services", {}).get("mcp", {}).get("port", 8765))' "$AKS_STATE/aks-values.json")
  kubectl -n daimon exec deployment/coke-daimon-mcp -- python -c \
    'import ssl,sys,urllib.request; ctx=ssl.create_default_context(); assert urllib.request.urlopen(sys.argv[1], context=ctx).status == 200; print("MCP HTTPS healthy")' "https://coke-daimon-mcp:$mcp_port/healthz"
  ;;
register)
  bash "$chart/scripts/register.sh"
  ;;
turn)
  tenant=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["tenant_id"])' "$AKS_STATE/registration.json")
  kubectl -n daimon exec deployment/coke-daimon-mcp -- daimon selfhosted turn \
    --tenant-id "$tenant" --thread aks-proof --message "${MESSAGE:?export MESSAGE}"
  ;;
probe)
  kubectl -n daimon exec -i deployment/coke-daimon-mcp -- python - < "$chart/scripts/live-c3.py"
  ;;
egress-start)
  kubectl -n kube-system create configmap coredns-custom --from-literal=log.override=log \
    --dry-run=client -o yaml | kubectl apply -f -
  kubectl -n kube-system rollout restart deployment/coredns
  kubectl -n kube-system rollout status deployment/coredns --timeout=120s
  date -u +%Y-%m-%dT%H:%M:%SZ > "$AKS_STATE/turn-start"
  kubectl -n daimon exec coke-daimon-harness-0 -- python -c \
    'import socket,sys; socket.getaddrinfo(sys.argv[1],443)' "$(cat "$AKS_STATE/aoai-host")"
  echo 'DNS evidence window started. Run the real Daimon turns now, then egress-check.'
  ;;
egress-check)
  kubectl -n kube-system logs -l k8s-app=kube-dns --all-containers=true --prefix \
    --since-time="$(cat "$AKS_STATE/turn-start")" --tail=-1 > "$AKS_STATE/turn-dns.log"
  python3 - "$AKS_STATE" <<'PY'
import re
import sys
from pathlib import Path
p=Path(sys.argv[1])
queries=[m[1].rstrip('.').lower() for m in re.finditer(r'"[A-Z0-9]+ IN ([^ ]+)',(p/'turn-dns.log').read_text())]
assert (p/'aoai-host').read_text().strip().lower() in queries, 'No AOAI DNS canary: evidence incomplete'
for q in queries:
    assert not (q=='anthropic.com' or q.endswith('.anthropic.com') or q=='openai.com' or q.endswith('.openai.com')), 'Forbidden DNS query: '+q
print('Turn-window DNS log has AOAI canary and no Anthropic/OpenAI.com lookups')
PY
  # Negative probe runs AFTER the evidence snapshot to avoid polluting its window.
  denied_ip=$(kubectl -n daimon exec coke-daimon-harness-0 -- python -c \
    'import socket; print(socket.gethostbyname("api.anthropic.com"))')
  host_pod=$(kubectl -n daimon get pods -l app=coke-daimon-mcp -o jsonpath='{.items[0].metadata.name}')
  for pod in coke-daimon-harness-0 "$host_pod"; do
    probe="egress-proof-$(date +%s)"
    kubectl -n daimon debug "pod/$pod" --image=curlimages/curl:8.16.0 \
      --container="$probe" --attach=false -- curl --noproxy '*' --connect-timeout 5 --max-time 10 \
      --resolve "api.anthropic.com:443:$denied_ip" -sS -o /dev/null https://api.anthropic.com
    kubectl -n daimon wait "pod/$pod" --timeout=90s \
      --for="jsonpath={.status.ephemeralContainerStatuses[?(@.name=='$probe')].state.terminated.exitCode}=28"
    kubectl -n daimon logs "$pod" -c "$probe"
    echo "curl in $pod timed out; no TLS/auth failure accepted"
  done
  for pod in coke-daimon-harness-0 "$host_pod"; do
    probe="imds-proof-$(date +%s)"
    kubectl -n daimon debug "pod/$pod" --image=curlimages/curl:8.16.0 \
      --container="$probe" --attach=false -- curl --noproxy '*' --connect-timeout 5 --max-time 10 \
      -H Metadata:true -sS -o /dev/null 'http://169.254.169.254/metadata/instance?api-version=2021-02-01'
    kubectl -n daimon wait "pod/$pod" --timeout=90s \
      --for="jsonpath={.status.ephemeralContainerStatuses[?(@.name=='$probe')].state.terminated.exitCode}=28"
    echo "IMDS curl in $pod timed out"
  done
  ;;
teardown)
  # Destructive pilot reset: delete app, all namespace PVCs, and AKS/node RG.
  helm uninstall coke -n daimon --ignore-not-found
  kubectl delete namespace daimon --wait=true --ignore-not-found
  az aks show -g "$RG" -n "$AKS" --query nodeResourceGroup -o tsv > "$AKS_STATE/node-rg"
  az aks delete -g "$RG" -n "$AKS" --yes -o none
  [[ $(az group exists -n "$(cat "$AKS_STATE/node-rg")" -o tsv) == false ]] || {
    echo 'Managed node resource group still exists: inspect billable disks/IPs before declaring teardown complete' >&2
    exit 1
  }
  ;;
reinstall)
  # Keep ACR images, AOAI deployment and protected operator state; no rebuild/quota wait.
  az aks create -g "$RG" -n "$AKS" --node-count 1 --node-vm-size Standard_D2s_v6 \
    --network-plugin azure --network-plugin-mode overlay --network-policy azure --attach-acr "$ACR" \
    --enable-managed-identity --generate-ssh-keys --tier free --node-osdisk-size 64 -o none
  bash "$chart/scripts/aks.sh" install
  ;;
remove)
  # Deletes the budget too. Check RG is dedicated before selecting this action.
  az group delete -n "$RG" --yes -o none
  ;;
*) echo 'Usage: aks.sh bootstrap|install|register|turn|probe|egress-start|egress-check|teardown|reinstall|remove' >&2; exit 2 ;;
esac
