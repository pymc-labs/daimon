#!/usr/bin/env bash
# Run after install. No model calls; writes only credential-free IDs/grants.
set -euo pipefail
umask 077
: "${AKS_STATE:?}" "${SELFHOSTED_MODEL:?}" "${SELFHOSTED_MODELS:?JSON tariff catalogue required}"
release=${RELEASE:-coke}
chart=deploy/helm/daimon-selfhost
kubectl -n daimon exec -i "deploy/$release-daimon-mcp" -- \
  env SELFHOSTED_MODEL="$SELFHOSTED_MODEL" python - < "$chart/scripts/provision.py" > "$AKS_STATE/registration.json"
tenant=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["tenant_id"])' "$AKS_STATE/registration.json")
kubectl -n daimon exec "deploy/$release-daimon-mcp" -- daimon selfhosted init \
  --tenant-id "$tenant" --harness-url "http://$release-daimon-harness:8088" \
  --allow-insecure-http --auth-mode bearer_env --auth-ref DAIMON_SELFHOSTED__HARNESS_TOKEN_PRIMARY \
  --model "$SELFHOSTED_MODEL" > "$AKS_STATE/selfhosted-ids"
python3 - "$AKS_STATE" "$release" <<'PY'
import json
import os
import sys
from pathlib import Path
p, release = Path(sys.argv[1]), sys.argv[2]
registration = json.loads((p/'registration.json').read_text())
agent, environment = (p/'selfhosted-ids').read_text().split()
grant = dict(registration, profile='selfhosted.loop', platform='cli',
             channel_id='selfhosted-operator', agent_id=agent, environment_id=environment,
             infrastructure_usd='0', max_tool_calls=5, deadline_s=120)
(p/'runtime.json').write_text(json.dumps([grant]))
values = json.loads((p/'aks-values.json').read_text())
model = os.environ['SELFHOSTED_MODEL']
catalogue = json.loads(os.environ['SELFHOSTED_MODELS'])
assert len(catalogue) == 1, 'Supply one explicitly priced accounting model'
values.setdefault('host', {}).update(runtimeSecret='daimon-runtime')
values['host'].setdefault('env', {}).update({
    'DAIMON_SELFHOSTED__HARNESS_AUTH_BINDINGS': json.dumps([dict(
        name='DAIMON_SELFHOSTED__HARNESS_TOKEN_PRIMARY', origin=f'http://{release}-daimon-harness:8088',
        tenants=[registration['tenant_id']], allow_insecure_http=True)]),
    'DAIMON_TURN__PATH':'mux', 'DAIMON_TURN__CHANNEL_BACKENDS':'true',
    'DAIMON_SELFHOSTED__ADMISSION_ENABLED':'true',
    'DAIMON_MCP__PUBLIC_URL':f'https://{release}-daimon-mcp:8765/mcp',
    'DAIMON_BILLING__SELFHOSTED__DEPLOYMENTS':json.dumps({model:next(iter(catalogue))}),
    'DAIMON_BILLING__SELFHOSTED__MODELS':json.dumps(catalogue),
    'DAIMON_BILLING__SELFHOSTED__ALLOW_TEST_TARIFFS':os.environ.get('ALLOW_TEST_TARIFFS', 'false'),
})
values.setdefault('harness', {}).setdefault('env', {}).update({
    'HARNESS_DAIMON_MCP_URL':f'https://{release}-daimon-mcp:8765/mcp',
})
# Remove obsolete grants when re-registering an older installation.
values['host']['env'].pop('DAIMON_SELFHOSTED__MCP_ALLOWED_HTTP_HOSTS', None)
values['harness']['env'].pop('HARNESS_ALLOW_INSECURE_DAIMON_MCP', None)
(p/'aks-values.json').write_text(json.dumps(values, indent=2)+'\n')
PY
kubectl create secret generic daimon-runtime -n daimon \
  --from-file=runtime.json="$AKS_STATE/runtime.json" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
postrender_args=()
if [[ -n ${POST_RENDERER:-} ]]; then postrender_args=(--post-renderer "$POST_RENDERER"); fi
helm upgrade --install "$release" "$chart" -n daimon -f "$AKS_STATE/aks-values.json" \
  "${postrender_args[@]}" --wait --wait-for-jobs --timeout 15m
# Secret volumes update eventually; force a fresh mount and cached runtime factory.
kubectl rollout restart -n daimon "deploy/$release-daimon-mcp" "deploy/$release-daimon-scheduler"
kubectl rollout status -n daimon "deploy/$release-daimon-mcp" --timeout=300s
kubectl rollout status -n daimon "deploy/$release-daimon-scheduler" --timeout=300s
