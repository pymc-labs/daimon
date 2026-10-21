#!/usr/bin/env bash
set -euo pipefail
scratch=$(mktemp -d)
trap 'rm -r "$scratch"' EXIT
cd "$scratch"
mkdir -p "$HOME/.local/bin"
curl -fsSLO https://github.com/kubernetes-sigs/kind/releases/download/v0.31.0/kind-linux-amd64
curl -fsSLO https://github.com/kubernetes-sigs/kind/releases/download/v0.31.0/kind-linux-amd64.sha256sum
sha256sum -c kind-linux-amd64.sha256sum
install -m755 kind-linux-amd64 "$HOME/.local/bin/kind"
curl -fsSLO https://get.helm.sh/helm-v3.19.0-linux-amd64.tar.gz
curl -fsSLO https://get.helm.sh/helm-v3.19.0-linux-amd64.tar.gz.sha256sum
sha256sum -c helm-v3.19.0-linux-amd64.tar.gz.sha256sum
tar xzf helm-v3.19.0-linux-amd64.tar.gz
install -m755 linux-amd64/helm "$HOME/.local/bin/helm"
curl -fsSLo kubectl https://dl.k8s.io/release/v1.35.0/bin/linux/amd64/kubectl
curl -fsSLo kubectl.sha256 https://dl.k8s.io/release/v1.35.0/bin/linux/amd64/kubectl.sha256
printf '%s  kubectl\n' "$(cat kubectl.sha256)" | sha256sum -c -
install -m755 kubectl "$HOME/.local/bin/kubectl"
