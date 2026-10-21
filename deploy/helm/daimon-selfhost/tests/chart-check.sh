#!/usr/bin/env bash
set -euo pipefail
chart=$(cd "$(dirname "$0")/.." && pwd)
scratch=$(mktemp -d)
trap 'rm -r "$scratch"' EXIT
helm lint --strict "$chart"
helm template coke "$chart" > "$scratch/default.yaml"
helm template coke "$chart" --set postgres.bundled=false --set ingress.enabled=true \
  --set ingress.className=nginx --set services.discord.enabled=true \
  --set services.slack.enabled=true \
  --set azureOpenAI.apiKeySecretRef.name=existing-aoai > "$scratch/external.yaml"
helm template coke "$chart" --set harness.miProxy.enabled=true \
  --set harness.miProxy.image=example.invalid/proxy@sha256:fixture \
  --set harness.miProxy.clientId=fixture --set harness.miProxy.tenantId=fixture \
  --set 'networkPolicy.azureOpenAICidrs={10.0.0.1/32}' \
  --set 'networkPolicy.blobCidrs={10.0.0.2/32}' \
  --set 'networkPolicy.entraCidrs={10.0.0.3/32}' \
  --set keyVaultCSI.enabled=true --set keyVaultCSI.secretProviderClass=fixture \
  > "$scratch/identity.yaml"
for option in 'harness.sessions.mode=single' 'harness.miProxy.enabled=true' \
  'networkPolicy.azureOpenAICidrs={169.254.169.254/32}' \
  'networkPolicy.azureOpenAICidrs={0.0.0.0/0}'; do
  if helm template coke "$chart" --set "$option" > /dev/null 2>&1; then
    echo "Expected refusal for $option" >&2
    exit 1
  fi
done
if command -v kubeconform >/dev/null; then
  kubeconform -strict -summary "$scratch"/*.yaml
fi
echo 'Chart variants and fail-closed guards passed'
