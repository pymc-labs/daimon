#!/usr/bin/env bash
# Queue once, then trust the run status API rather than the log stream exit code.
set -euo pipefail
umask 077
cd "$(git rev-parse --show-toplevel)"
: "${ACR:?export ACR}" "${TAG:?export TAG}" "${AKS_STATE:?export AKS_STATE}"
# Azure CLI's --no-wait path can suppress returned JSON. Its queue receipt
# includes the exact run ID on stderr; do not select the registry's latest run.
queue_output=$(AZURE_CORE_ONLY_SHOW_ERRORS=false az acr run -r "$ACR" \
  -f deploy/helm/daimon-selfhost/scripts/acr-build.yaml --set "tag=$TAG" \
  --timeout 7200 --no-wait -o none . 2>&1) || {
  printf '%s\n' "$queue_output" >&2
  echo 'ACR Tasks BuildKit build/push failed to queue; stopping before provisioning' >&2
  exit 1
}
printf '%s\n' "$queue_output"
if [[ $queue_output =~ Queued\ a\ run\ with\ ID:\ ([[:alnum:]-]+) ]]; then
  run=${BASH_REMATCH[1]}
else
  echo 'ACR returned no valid run ID in its queue receipt; stopping before provisioning' >&2
  exit 1
fi
printf '%s\n' "$run" > "$AKS_STATE/acr-run-id"
echo "ACR run $run queued for tag $TAG"
bash deploy/helm/daimon-selfhost/scripts/wait-build.sh "$run"
