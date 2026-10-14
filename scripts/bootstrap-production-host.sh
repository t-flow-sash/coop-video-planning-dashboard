#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
CONFIG_DIR="${COOP_CONFIG_DIR:-/root/.config/coop-dashboard}"
REGISTRY_DATA="${REGISTRY_DATA:-/var/lib/coop-dashboard/registry}"
REGISTRY_IMAGE='docker.io/library/registry@sha256:21078ecfcba8a0cd7e1a384f75b3509899661dd21823bf1da21b5a4674aaceb9'
EXPECTED_DOCKER='29.9.1'
EXPECTED_K3S='v1.36.5+k3s1'
EXPECTED_FLUX='2.9.6'
EXPECTED_SOPS='3.13.3'
EXPECTED_AGE='v1.3.2'

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null || fail "missing command: $1"; }

for cmd in docker k3s kubectl flux sops age curl ssh-keyscan; do need "$cmd"; done
[[ "$(docker version --format '{{.Server.Version}}')" == "$EXPECTED_DOCKER" ]] || fail 'unexpected Docker version'
[[ "$(k3s --version | sed -n '1s/^k3s version \([^ ]*\).*/\1/p')" == "$EXPECTED_K3S" ]] || fail 'unexpected k3s version'
[[ "$(flux --version | awk '{print $3}')" == "$EXPECTED_FLUX" ]] || fail 'unexpected Flux CLI version'
[[ "$(sops --version | awk 'NR==1{print $2}')" == "$EXPECTED_SOPS" ]] || fail 'unexpected SOPS version'
[[ "$(age --version)" == "$EXPECTED_AGE" ]] || fail 'unexpected age version'

[[ -f /etc/rancher/k3s/config.yaml ]] || fail 'missing /etc/rancher/k3s/config.yaml'
[[ -f /etc/rancher/k3s/registries.yaml ]] || fail 'missing /etc/rancher/k3s/registries.yaml'
grep -qx 'bind-address: 192.168.2.94' /etc/rancher/k3s/config.yaml || fail 'unexpected k3s API bind address'
grep -q 'http://127.0.0.1:5000' /etc/rancher/k3s/registries.yaml || fail 'k3s registry mirror missing'

install -d -o root -g root -m 0700 "$REGISTRY_DATA"
if ! docker container inspect coop-registry >/dev/null 2>&1; then
  docker pull "$REGISTRY_IMAGE"
  docker run -d --name coop-registry --restart unless-stopped \
    -p 127.0.0.1:5000:5000 -e OTEL_TRACES_EXPORTER=none \
    -v "$REGISTRY_DATA:/var/lib/registry" "$REGISTRY_IMAGE" >/dev/null
fi
[[ "$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' coop-registry)" == 'unless-stopped' ]] || fail 'registry restart policy differs'
[[ "$(docker port coop-registry 5000/tcp)" == '127.0.0.1:5000' ]] || fail 'registry is not loopback-only'
curl -fsS http://127.0.0.1:5000/v2/ >/dev/null || fail 'registry health probe failed'

systemctl is-enabled --quiet docker || fail 'Docker is not enabled'
systemctl is-active --quiet docker || fail 'Docker is not active'
systemctl is-enabled --quiet k3s || fail 'k3s is not enabled'
systemctl is-active --quiet k3s || fail 'k3s is not active'
k3s kubectl wait --for=condition=Ready node --all --timeout=90s >/dev/null

if [[ "${1:-}" == '--create-flux-secrets' ]]; then
  [[ $# -eq 1 ]] || fail 'unexpected arguments'
  [[ -s "$CONFIG_DIR/sops-age.key" ]] || fail 'missing SOPS age private key'
  [[ -s "$CONFIG_DIR/flux-github-readonly" ]] || fail 'missing Flux deploy private key'
  k3s kubectl create namespace flux-system --dry-run=client -o yaml | k3s kubectl apply -f - >/dev/null
  known_hosts="$(mktemp)"
  trap 'shred -u "$known_hosts"' EXIT
  ssh-keyscan -t ed25519 github.com >"$known_hosts" 2>/dev/null
  [[ -s "$known_hosts" ]] || fail 'could not obtain GitHub host key'
  k3s kubectl -n flux-system create secret generic sops-age \
    --from-file=age.agekey="$CONFIG_DIR/sops-age.key" \
    --dry-run=client -o yaml | k3s kubectl apply -f - >/dev/null
  flux create secret git coop-git-readonly --namespace=flux-system \
    --url=ssh://git@github.com/t-flow-sash/coop-video-planning-dashboard.git \
    --private-key-file="$CONFIG_DIR/flux-github-readonly" \
    --known-hosts-file="$known_hosts" --export | k3s kubectl apply -f - >/dev/null
  trap - EXIT
  shred -u "$known_hosts"
  printf 'Flux bootstrap secrets created/updated; no source or workload was reconciled.\n'
elif [[ $# -ne 0 ]]; then
  fail 'usage: bootstrap-production-host.sh [--create-flux-secrets]'
fi

printf 'Host prerequisites verified. No Flux source, workload, seed, or application was applied.\n'
