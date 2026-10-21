#!/usr/bin/env bash
# Poll the exact queued run; shared with the operator's build helper.
set -euo pipefail
umask 077
: "${ACR:?export ACR}" "${TAG:?export TAG}" "${AKS_STATE:?export AKS_STATE}"
run=${1:?supply the queued ACR run ID}
[[ $run =~ ^[[:alnum:]-]+$ ]] || { echo 'Invalid ACR run ID' >&2; exit 1; }
wait_seconds=${ACR_BUILD_WAIT_SECONDS:-7200}
poll_seconds=${ACR_BUILD_POLL_SECONDS:-15}
for value in "$wait_seconds" "$poll_seconds"; do
  [[ $value =~ ^[1-9][0-9]{0,5}$ ]] || {
    echo 'ACR build wait/poll seconds must be positive integers of at most six digits' >&2
    exit 1
  }
done
deadline=$((SECONDS + wait_seconds))
while true; do
  status=$(az acr task show-run -r "$ACR" --run-id "$run" --query status -o tsv) || {
    echo "Cannot verify ACR run $run status; stopping before provisioning" >&2
    exit 1
  }
  echo "ACR run $run: $status"
  case "$status" in
    Succeeded) break ;;
    Queued|Started|Running) ;;
    *)
      echo "ACR run $run did not succeed (status: $status); stopping before provisioning" >&2
      az acr task logs -r "$ACR" --run-id "$run" || true
      exit 1
      ;;
  esac
  remaining=$((deadline - SECONDS))
  if (( remaining <= 0 )); then
    echo "Timed out waiting for ACR run $run to succeed; stopping before provisioning. The remote run may still be active." >&2
    exit 1
  fi
  delay=$poll_seconds
  (( delay <= remaining )) || delay=$remaining
  sleep "$delay"
done
# Log retrieval is diagnostic; only the authoritative status above permits progress.
az acr task logs -r "$ACR" --run-id "$run" || echo "Could not fetch logs for successful ACR run $run" >&2
for image in "daimon:$TAG" "daimon-harness:$TAG"; do
  az acr repository show -n "$ACR" --image "$image" -o none || {
    echo "Required ACR image $image is missing or unreadable; stopping before Postgres import and AKS creation. Check ACR run $run." >&2
    exit 1
  }
done
printf '%s\n' "$TAG" > "$AKS_STATE/image-tag"
