#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT"
CONFIG_DIR="${COOP_CONFIG_DIR:-/root/.config/coop-dashboard}"
REGISTRY_DATA="${REGISTRY_DATA:-/var/lib/coop-dashboard/registry}"
K3S_CONFIG_FILE="${K3S_CONFIG_FILE:-/etc/rancher/k3s/config.yaml}"
K3S_REGISTRIES_FILE="${K3S_REGISTRIES_FILE:-/etc/rancher/k3s/registries.yaml}"
REGISTRY_IMAGE='docker.io/library/registry:3.1.2@sha256:ddf754342cfc8acc51a56d5d0ab6af06826461864460636d8bd5c546dab2a7b8'
EXPECTED_DOCKER='29.9.1'
EXPECTED_K3S='v1.36.5+k3s1'
EXPECTED_FLUX='2.9.6'
EXPECTED_SOPS='3.13.3'
EXPECTED_AGE='v1.3.2'
GITHUB_KNOWN_HOSTS_FILE="${GITHUB_KNOWN_HOSTS_FILE:-$ROOT/config/github-known-hosts}"
GITHUB_ED25519_FINGERPRINT='SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU'

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null || fail "missing command: $1"; }
kubectl() { k3s kubectl "$@"; }

verify_github_host_key() {
  [[ -f "$GITHUB_KNOWN_HOSTS_FILE" ]] || fail "missing versioned GitHub known_hosts file: $GITHUB_KNOWN_HOSTS_FILE"
  [[ "$(grep -cve '^$' "$GITHUB_KNOWN_HOSTS_FILE")" -eq 1 ]] || \
    fail 'versioned GitHub known_hosts file must contain exactly one key'
  grep -Eq '^github\.com ssh-ed25519 [A-Za-z0-9+/=]+$' "$GITHUB_KNOWN_HOSTS_FILE" || \
    fail 'versioned GitHub known_hosts entry is not an Ed25519 github.com key'
  actual_fingerprint="$(ssh-keygen -lf "$GITHUB_KNOWN_HOSTS_FILE" -E sha256 2>/dev/null | awk 'NR == 1 {print $2}')" || \
    fail 'could not calculate GitHub Ed25519 host key fingerprint'
  [[ "$actual_fingerprint" == "$GITHUB_ED25519_FINGERPRINT" ]] || \
    fail "GitHub Ed25519 host key fingerprint mismatch: $actual_fingerprint"
}

verify_firewall() {
  need ufw
  need python3
  firewall_status="$(ufw status verbose)" || fail 'could not read UFW status'
  python3 - "$firewall_status" <<'PY' || fail 'UFW policy or allow-rule set differs from the production contract'
import re
import sys

status = sys.argv[1]
if "Status: active" not in status:
    raise SystemExit("UFW is not active")
if "Default: deny (incoming), allow (outgoing), deny (routed)" not in status:
    raise SystemExit("UFW defaults differ")
expected = {
    "22/tcp ALLOW IN Anywhere",
    "80/tcp ALLOW IN 192.168.2.0/24",
    "443/tcp ALLOW IN 192.168.2.0/24",
    "Anywhere ALLOW IN 10.42.0.0/16",
    "Anywhere ALLOW IN 10.43.0.0/16",
    "22/tcp (v6) ALLOW IN Anywhere (v6)",
    "Anywhere ALLOW FWD 10.42.0.0/16",
    "Anywhere ALLOW FWD 10.43.0.0/16",
}
actual = []
for raw_line in status.splitlines():
    line = re.sub(r"\s+#.*$", "", raw_line).strip()
    line = re.sub(r"\s+", " ", line)
    if " ALLOW " in f" {line} ":
        actual.append(line)
if len(actual) != len(set(actual)) or set(actual) != expected:
    raise SystemExit(f"allow rules differ: {actual!r}")
PY
  printf 'Firewall configuration verified read-only; no rules were changed.\n'
}

wait_flux_ready() {
  kubectl wait --for=condition=Established --timeout=120s customresourcedefinition/buckets.source.toolkit.fluxcd.io \
    customresourcedefinition/externalartifacts.source.toolkit.fluxcd.io \
    customresourcedefinition/gitrepositories.source.toolkit.fluxcd.io \
    customresourcedefinition/helmcharts.source.toolkit.fluxcd.io \
    customresourcedefinition/helmrepositories.source.toolkit.fluxcd.io \
    customresourcedefinition/ocirepositories.source.toolkit.fluxcd.io \
    customresourcedefinition/kustomizations.kustomize.toolkit.fluxcd.io \
    customresourcedefinition/helmreleases.helm.toolkit.fluxcd.io \
    customresourcedefinition/alerts.notification.toolkit.fluxcd.io \
    customresourcedefinition/providers.notification.toolkit.fluxcd.io \
    customresourcedefinition/receivers.notification.toolkit.fluxcd.io >/dev/null
  kubectl wait -n flux-system --for=condition=Available --timeout=180s deployment/source-controller >/dev/null
  kubectl wait -n flux-system --for=condition=Available --timeout=180s deployment/kustomize-controller >/dev/null
  kubectl wait -n flux-system --for=condition=Available --timeout=180s deployment/helm-controller >/dev/null
  kubectl wait -n flux-system --for=condition=Available --timeout=180s deployment/notification-controller >/dev/null
}

MODE="${1:-}"
if [[ $# -gt 1 ]] || [[ -n "$MODE" && "$MODE" != '--install-flux-controllers' && "$MODE" != '--bootstrap-coop-root' && "$MODE" != '--verify-firewall' ]]; then
  fail 'usage: bootstrap-production-host.sh [--verify-firewall|--install-flux-controllers|--bootstrap-coop-root]'
fi
if [[ "$MODE" == '--verify-firewall' ]]; then
  verify_firewall
  exit 0
fi
if [[ "$MODE" == '--bootstrap-coop-root' ]]; then
  need ssh-keygen
  verify_github_host_key
fi

for cmd in docker k3s kubectl flux sops age curl python3; do need "$cmd"; done
[[ "$(docker version --format '{{.Server.Version}}')" == "$EXPECTED_DOCKER" ]] || fail 'unexpected Docker version'
[[ "$(k3s --version | sed -n '1s/^k3s version \([^ ]*\).*/\1/p')" == "$EXPECTED_K3S" ]] || fail 'unexpected k3s version'
[[ "$(flux --version | awk '{print $3}')" == "$EXPECTED_FLUX" ]] || fail 'unexpected Flux CLI version'
[[ "$(sops --version | awk 'NR==1{print $2}')" == "$EXPECTED_SOPS" ]] || fail 'unexpected SOPS version'
[[ "$(age --version)" == "$EXPECTED_AGE" ]] || fail 'unexpected age version'

[[ -f "$K3S_CONFIG_FILE" ]] || fail "missing $K3S_CONFIG_FILE"
[[ -f "$K3S_REGISTRIES_FILE" ]] || fail "missing $K3S_REGISTRIES_FILE"
grep -qx 'bind-address: 192.168.2.94' "$K3S_CONFIG_FILE" || fail 'unexpected k3s API bind address'
grep -q 'http://127.0.0.1:5000' "$K3S_REGISTRIES_FILE" || fail 'k3s registry mirror missing'

validate_registry_container() {
  python3 -c '
import json
import sys

expected_image, expected_source = sys.argv[1:]
containers = json.load(sys.stdin)
if len(containers) != 1:
    raise SystemExit(1)
container = containers[0]
config = container.get("Config") or {}
host = container.get("HostConfig") or {}
controlled_env = {}
for item in config.get("Env") or []:
    key, separator, value = item.partition("=")
    if separator and (key.startswith("REGISTRY_") or key.startswith("OTEL_")):
        controlled_env[key] = value
expected_env = {
    "OTEL_TRACES_EXPORTER": "none",
    "REGISTRY_STORAGE_FILESYSTEM_ROOTDIRECTORY": "/var/lib/registry",
}
mounts = [
    {
        "Type": mount.get("Type"),
        "Source": mount.get("Source"),
        "Destination": mount.get("Destination"),
        "RW": mount.get("RW"),
    }
    for mount in container.get("Mounts") or []
]
valid = (
    config.get("Image") == expected_image
    and controlled_env == expected_env
    and config.get("Entrypoint") == ["/entrypoint.sh"]
    and config.get("Cmd") == ["/etc/distribution/config.yml"]
    and mounts == [{"Type": "bind", "Source": expected_source, "Destination": "/var/lib/registry", "RW": True}]
    and host.get("PortBindings") == {"5000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5000"}]}
    and host.get("RestartPolicy") == {"Name": "unless-stopped", "MaximumRetryCount": 0}
)
if not valid:
    raise SystemExit(1)
print("running" if (container.get("State") or {}).get("Running") is True else "stopped")
' "$REGISTRY_IMAGE" "$REGISTRY_DATA"
}

install -d -o root -g root -m 0700 "$REGISTRY_DATA"
registry_inspect_file="$(mktemp)"
registry_inspect_error="$(mktemp)"
trap 'rm -f "$registry_inspect_file" "$registry_inspect_error"' EXIT INT TERM
if docker container inspect coop-registry >"$registry_inspect_file" 2>"$registry_inspect_error"; then
  registry_state="$(validate_registry_container <"$registry_inspect_file")" || \
    fail 'existing coop-registry configuration differs; refusing replacement'
  if [[ "$registry_state" == stopped ]]; then
    docker start coop-registry >/dev/null
  fi
elif grep -Eq '^(Error response from daemon: |Error: )?No such (object|container): coop-registry$' "$registry_inspect_error"; then
  docker pull "$REGISTRY_IMAGE"
  docker run -d --name coop-registry --restart unless-stopped \
    -p 127.0.0.1:5000:5000 \
    -e OTEL_TRACES_EXPORTER=none \
    -e REGISTRY_STORAGE_FILESYSTEM_ROOTDIRECTORY=/var/lib/registry \
    -v "$REGISTRY_DATA:/var/lib/registry" \
    --entrypoint /entrypoint.sh \
    "$REGISTRY_IMAGE" /etc/distribution/config.yml >/dev/null
else
  registry_inspect_message="$(<"$registry_inspect_error")"
  fail "could not inspect coop-registry: ${registry_inspect_message:-unknown Docker error}"
fi
rm -f "$registry_inspect_file" "$registry_inspect_error"
trap - EXIT INT TERM
curl -fsS http://127.0.0.1:5000/v2/ >/dev/null || fail 'registry health probe failed'

systemctl is-enabled --quiet docker || fail 'Docker is not enabled'
systemctl is-active --quiet docker || fail 'Docker is not active'
systemctl is-enabled --quiet k3s || fail 'k3s is not enabled'
systemctl is-active --quiet k3s || fail 'k3s is not active'
kubectl wait --for=condition=Ready node --all --timeout=90s >/dev/null

if [[ "${1:-}" == '--install-flux-controllers' ]]; then
  [[ $# -eq 1 ]] || fail 'unexpected arguments'
  kubectl apply -k clusters/production/flux-system >/dev/null
  wait_flux_ready
  printf 'Flux CRDs and controllers installed and ready; no source or workload was reconciled.\n'
elif [[ "$MODE" == '--bootstrap-coop-root' ]]; then
  [[ $# -eq 1 ]] || fail 'unexpected arguments'
  wait_flux_ready
  [[ -s "$CONFIG_DIR/sops-age.key" ]] || fail 'missing SOPS age private key'
  [[ -s "$CONFIG_DIR/flux-github-readonly" ]] || fail 'missing Flux deploy private key'
  kubectl -n flux-system create secret generic sops-age \
    --from-file=age.agekey="$CONFIG_DIR/sops-age.key" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl -n flux-system create secret generic coop-git-readonly \
    --from-file=identity="$CONFIG_DIR/flux-github-readonly" \
    --from-file=known_hosts="$GITHUB_KNOWN_HOSTS_FILE" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl apply -f clusters/production/source.yaml >/dev/null
  kubectl wait -n flux-system --for=condition=Ready \
    gitrepository/coop-video-planning --timeout=5m >/dev/null
  kubectl apply -f clusters/production/root-kustomization.yaml >/dev/null
  printf 'Flux bootstrap secrets, source, and self-managed coop-root created.\n'
fi

printf 'Host prerequisites verified.\n'
