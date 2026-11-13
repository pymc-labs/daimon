#!/usr/bin/env bash
# Run after install. No model calls; writes only credential-free IDs/grants.
set -euo pipefail
umask 077
: "${ALLOW_TEST_TARIFFS:?set true for TEST rates, false for verified production rates}" "${AKS_STATE:?}" "${SELFHOSTED_MODEL:?}" "${SELFHOSTED_MODELS:?JSON tariff catalogue required}"
chart=deploy/helm/daimon-selfhost
source "$chart/scripts/release.sh"
kubectl -n "$NAMESPACE" exec -i "deploy/$PREFIX-mcp" -- setpriv --reuid=1000 --regid=1000 --clear-groups --no-new-privs env HOME=/home/daimon \
  env SELFHOSTED_MODEL="$SELFHOSTED_MODEL" python - < "$chart/scripts/provision.py" > "$AKS_STATE/registration.json"
tenant=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["tenant_id"])' "$AKS_STATE/registration.json")
kubectl -n "$NAMESPACE" exec "deploy/$PREFIX-mcp" -- setpriv --reuid=1000 --regid=1000 --clear-groups --no-new-privs env HOME=/home/daimon daimon selfhosted init \
  --tenant-id "$tenant" --harness-url "https://$PREFIX-harness:8088" \
  --auth-mode bearer_env --auth-ref DAIMON_SELFHOSTED__HARNESS_TOKEN_PRIMARY \
  --model "$SELFHOSTED_MODEL" > "$AKS_STATE/selfhosted-ids"
python3 - "$AKS_STATE" "$PREFIX" "$SECRET_PREFIX" <<'PY'
import json
import os
import sys
from pathlib import Path
p, prefix, secret_prefix = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
registration = json.loads((p/'registration.json').read_text())
agent, environment = (p/'selfhosted-ids').read_text().split()
grant = dict(registration, profile='selfhosted.loop', platform='cli',
             channel_id='selfhosted-operator', agent_id=agent, environment_id=environment,
             infrastructure_usd='0', max_tool_calls=5, deadline_s=120)
(p/'runtime.json').write_text(json.dumps([grant]))
values = json.loads((p/'aks-values.json').read_text())
mcp_port = values.get('services', {}).get('mcp', {}).get('port', 8765)
model = os.environ['SELFHOSTED_MODEL']
catalogue = json.loads(os.environ['SELFHOSTED_MODELS'])
assert len(catalogue) == 1, 'Supply one explicitly priced accounting model'
values.setdefault('host', {}).update(runtimeSecret=secret_prefix+'-runtime')
values['host'].setdefault('env', {}).update({
    'DAIMON_SELFHOSTED__HARNESS_AUTH_BINDINGS': json.dumps([dict(
        name='DAIMON_SELFHOSTED__HARNESS_TOKEN_PRIMARY', origin=f'https://{prefix}-harness:8088',
        tenants=[registration['tenant_id']])]),
    'DAIMON_TURN__PATH':'mux', 'DAIMON_TURN__CHANNEL_BACKENDS':'true',
    'DAIMON_SELFHOSTED__ADMISSION_ENABLED':'true',
    'DAIMON_MCP__PUBLIC_URL':f'https://{prefix}-mcp:{mcp_port}/mcp',
    'DAIMON_BILLING__SELFHOSTED__DEPLOYMENTS':json.dumps({model:next(iter(catalogue))}),
    'DAIMON_BILLING__SELFHOSTED__MODELS':json.dumps(catalogue),
    'DAIMON_BILLING__SELFHOSTED__ALLOW_TEST_TARIFFS':os.environ['ALLOW_TEST_TARIFFS'],
})
values.setdefault('harness', {}).setdefault('env', {}).update({
    'HARNESS_DAIMON_MCP_URL':f'https://{prefix}-mcp:{mcp_port}/mcp',
})
# Remove obsolete grants when re-registering an older installation.
values['host']['env'].pop('DAIMON_SELFHOSTED__MCP_ALLOWED_HTTP_HOSTS', None)
values['harness']['env'].pop('HARNESS_ALLOW_INSECURE_DAIMON_MCP', None)
(p/'aks-values.json').write_text(json.dumps(values, indent=2)+'\n')
PY
kubectl create secret generic "$SECRET_PREFIX-runtime" -n "$NAMESPACE" \
  --from-file=runtime.json="$AKS_STATE/runtime.json" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
postrender_args=()
if [[ -n ${POST_RENDERER:-} ]]; then postrender_args=(--post-renderer "$POST_RENDERER"); fi
helm upgrade --install --atomic "$RELEASE" "$chart" -n "$NAMESPACE" "${HELM_VALUES[@]}" \
  "${postrender_args[@]}" --wait --wait-for-jobs --timeout 15m
# Secret volumes update eventually; force a fresh mount and cached runtime factory.
kubectl rollout restart -n "$NAMESPACE" "deploy/$PREFIX-mcp" "deploy/$PREFIX-scheduler"
kubectl rollout status -n "$NAMESPACE" "deploy/$PREFIX-mcp" --timeout=300s
kubectl rollout status -n "$NAMESPACE" "deploy/$PREFIX-scheduler" --timeout=300s
