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
export AOAI_DEPLOYMENT=${AOAI_DEPLOYMENT:-gpt-5.4-mini}
export SELFHOSTED_MODEL=${SELFHOSTED_MODEL:-$AOAI_DEPLOYMENT}
verify_aoai_model() {
  AOAI_VERIFIED_MODEL=$(az cognitiveservices account deployment show -g "$RG" -n "$AOAI" \
    --deployment-name "$AOAI_DEPLOYMENT" --query properties.model.name -o tsv) || {
    echo "Existing AOAI account is missing or cannot read deployment $AOAI_DEPLOYMENT; provision it before bootstrap/probe" >&2
    exit 1
  }
  if [[ ${AOAI_VERIFIED_MODEL,,} == *sol* || "$AOAI_VERIFIED_MODEL" != gpt-5.4-mini ]]; then
    echo 'AOAI deployment must be backed by gpt-5.4-mini; Sol models are refused' >&2
    exit 1
  fi
  export AOAI_VERIFIED_MODEL
}
bind_aks() {
  # A fresh, private file prevents inherited contexts or stale cluster credentials.
  kube_state=$(mktemp -d "$AKS_STATE/kubeconfig.XXXXXX")
  trap 'rm -r -- "$kube_state"' EXIT
  export KUBECONFIG="$kube_state/config"
  unset HELM_KUBEAPISERVER HELM_KUBECONTEXT
  az aks get-credentials -g "$RG" -n "$AKS" --file "$KUBECONFIG" --overwrite-existing -o none
  [[ -s "$KUBECONFIG" ]] || { echo 'AKS credentials file is missing or empty' >&2; exit 1; }
  metadata=$(az aks show -g "$RG" -n "$AKS" \
    --query '{fqdn:fqdn,privateFqdn:privateFqdn,azurePortalFqdn:azurePortalFqdn}' -o json)
  server=$(kubectl config view --minify -o 'jsonpath={.clusters[0].cluster.server}')
  python3 - "$metadata" "$server" <<'PYVERIFY'
import json
import sys
from urllib.parse import urlsplit
try:
    hosts = {v.lower() for v in json.loads(sys.argv[1]).values() if isinstance(v, str) and v}
    server = urlsplit(sys.argv[2])
    valid = (server.scheme == 'https' and server.hostname in hosts
             and server.port in (None, 443) and server.username is None and server.password is None
             and server.path in ('', '/') and not server.query and not server.fragment)
except (ValueError, AttributeError):
    valid = False
if not valid:
    raise SystemExit('Refusing cluster operation: kubeconfig API server does not match the target AKS')
PYVERIFY
}
case ${1:-} in
install|register|turn|egress-start|egress-check|teardown) bind_aks ;;
esac
case ${1:-} in
bootstrap)
  command -v az >/dev/null 2>&1 || {
    echo 'bootstrap requires Azure CLI (az) with ACR Tasks support; install it and az login first' >&2
    exit 1
  }
  az acr run --help >/dev/null 2>&1 || {
    echo 'bootstrap requires Azure CLI with az acr run (ACR Tasks); update Azure CLI' >&2
    exit 1
  }
  az group create -n "$RG" -l "$LOCATION" -o none
  az acr create -g "$RG" -n "$ACR" --sku Basic --admin-enabled false -o none
  # Stream remote builder diagnostics and wait for BOTH pushes before AKS creation.
  # Do not use --no-logs: operators need the run ID and first failing step.
  az acr run -r "$ACR" -f "$chart/scripts/acr-build.yaml" \
    --set "tag=$TAG" --timeout 7200 . || {
    echo 'ACR Tasks BuildKit build/push failed; inspect the run ID above. The remote builder must support DOCKER_BUILDKIT=1; AKS has not been created.' >&2
    exit 1
  }
  # A detached/interrupted client may return before a remote push is available.
  # Confirm BOTH requested tags before any downstream provisioning.
  for image in "daimon:$TAG" "daimon-harness:$TAG"; do
    az acr repository show -n "$ACR" --image "$image" -o none || {
      echo "Required ACR image $image is missing or unreadable; stopping before Postgres import and AKS creation. Check the ACR run above." >&2
      exit 1
    }
  done
  az acr import -n "$ACR" --source docker.io/library/postgres:18-alpine --image postgres:18-alpine --force -o none
  az acr repository show -n "$ACR" --image postgres:18-alpine --query digest -o tsv > "$AKS_STATE/postgres-digest"
  az aks create -g "$RG" -n "$AKS" --node-count 1 --node-vm-size Standard_D2s_v6 \
    --network-plugin azure --network-plugin-mode overlay --network-policy azure --attach-acr "$ACR" \
    --enable-managed-identity --generate-ssh-keys --tier free --node-osdisk-size 64 -o none
  accounts=$(az cognitiveservices account list -g "$RG" --query '[].name' -o tsv)
  if grep -Fxq -- "$AOAI" <<< "$accounts"; then
    verify_aoai_model
    echo "Reusing existing AOAI account and deployment $AOAI_DEPLOYMENT"
  else
    az cognitiveservices account create -g "$RG" -n "$AOAI" -l "$LOCATION" \
      --kind OpenAI --sku S0 --custom-domain "$AOAI" -o none
    az cognitiveservices account deployment create -g "$RG" -n "$AOAI" \
      --deployment-name "$AOAI_DEPLOYMENT" --model-name gpt-5.4-mini --model-version 2026-03-17 \
      --model-format OpenAI --sku-name DataZoneStandard --sku-capacity 1 -o none
  fi
  ;;
install)
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
# Upgrade saved operator state too: envFrom requires the dedicated variable to
# be an actual daimon-host Secret key, not a Helm value or secretKeyRef mapping.
host_env = p/'host.env'
if not any(line.startswith('DAIMON_SELFHOSTED__HARNESS_TOKEN_PRIMARY=')
           for line in host_env.read_text().splitlines()):
    harness_token = next(line.split('=', 1)[1] for line in (p/'harness.env').read_text().splitlines()
                         if line.startswith('HARNESS_TOKEN='))
    with host_env.open('a') as output:
        output.write('\nDAIMON_SELFHOSTED__HARNESS_TOKEN_PRIMARY='+harness_token+'\n')
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
    'azureOpenAI': {'endpoint':endpoint, 'deployment':values.get('azureOpenAI', {}).get('deployment', os.environ['AOAI_DEPLOYMENT']),
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
  kubectl -n daimon exec deployment/coke-daimon-mcp -- setpriv --reuid=1000 --regid=1000 --clear-groups --no-new-privs env HOME=/home/daimon python -c \
    'import ssl,sys,urllib.request; ctx=ssl.create_default_context(); assert urllib.request.urlopen(sys.argv[1], context=ctx).status == 200; print("MCP HTTPS healthy")' "https://coke-daimon-mcp:$mcp_port/healthz"
  ;;
register)
  bash "$chart/scripts/register.sh"
  ;;
turn)
  tenant=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["tenant_id"])' "$AKS_STATE/registration.json")
  kubectl -n daimon exec deployment/coke-daimon-mcp -- setpriv --reuid=1000 --regid=1000 --clear-groups --no-new-privs env HOME=/home/daimon daimon selfhosted turn \
    --tenant-id "$tenant" --thread aks-proof --message "${MESSAGE:?export MESSAGE}"
  ;;
probe)
  if [[ ${SELFHOSTED_MODEL,,} == *sol* || ${AOAI_DEPLOYMENT,,} == *sol* || "$SELFHOSTED_MODEL" != "$AOAI_DEPLOYMENT" ]]; then
    echo 'Probe requires the configured AOAI_DEPLOYMENT; Sol models are refused' >&2
    exit 1
  fi
  verify_aoai_model
  bind_aks
  kubectl -n daimon exec -i deployment/coke-daimon-mcp -- setpriv --reuid=1000 --regid=1000 --clear-groups --no-new-privs env HOME=/home/daimon env AOAI_VERIFIED_MODEL="$AOAI_VERIFIED_MODEL" AOAI_DEPLOYMENT="$AOAI_DEPLOYMENT" SELFHOSTED_MODEL="$SELFHOSTED_MODEL" python - < "$chart/scripts/live-c3.py"
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
