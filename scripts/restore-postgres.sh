#!/usr/bin/env bash
set -Eeuo pipefail

NAMESPACE="${NAMESPACE:-coop-prod}"
DUMP="${1:-}"
CONFIRM="${2:-}"
[[ "$DUMP" =~ ^(daily|weekly|pre-restore)-[0-9]{8}T[0-9]{6}Z\.dump$ ]] || {
  echo "Usage: $0 <daily|weekly|pre-restore>-YYYYMMDDTHHMMSSZ.dump --yes" >&2
  exit 2
}
[[ "$CONFIRM" == "--yes" ]] || { echo "Restore is destructive; pass --yes." >&2; exit 2; }
command -v kubectl >/dev/null || { echo "kubectl is required" >&2; exit 127; }

pod="coop-restore-$(date -u +%s)"
replicas="$(kubectl -n "$NAMESPACE" get deployment coop-api -o jsonpath='{.spec.replicas}')"
readonly suspend_order=(
  kustomization/coop-root
  gitrepository/coop-video-planning
  kustomization/coop-backup
  kustomization/coop-ingress
  kustomization/coop-apps
  kustomization/coop-migration
  kustomization/coop-database
)
readonly resume_order=(
  gitrepository/coop-video-planning
  kustomization/coop-database
  kustomization/coop-migration
  kustomization/coop-apps
  kustomization/coop-ingress
  kustomization/coop-backup
  kustomization/coop-root
)
declare -A was_suspended=()
for resource in "${suspend_order[@]}"; do
  kind="${resource%%/*}"
  name="${resource#*/}"
  was_suspended["$resource"]="$(kubectl -n flux-system get "$kind" "$name" -o jsonpath='{.spec.suspend}')"
done
backup_cron_was_suspended="$(kubectl -n "$NAMESPACE" get cronjob postgres-backup -o jsonpath='{.spec.suspend}')"

restore_succeeded=false
maintenance_started=false
suspend_resource() {
  local resource="$1" kind name
  kind="${resource%%/*}"
  name="${resource#*/}"
  kubectl -n flux-system patch "$kind" "$name" --type=merge \
    -p '{"spec":{"suspend":true}}' >/dev/null
}

fail_closed() {
  local resource
  for resource in "${suspend_order[@]}"; do
    suspend_resource "$resource" >/dev/null 2>&1 || true
  done
  kubectl -n "$NAMESPACE" patch cronjob postgres-backup --type=merge \
    -p '{"spec":{"suspend":true}}' >/dev/null 2>&1 || true
  kubectl -n "$NAMESPACE" scale deployment/coop-api --replicas=0 >/dev/null 2>&1 || true
}

cleanup() {
  local status=$?
  trap - EXIT
  kubectl -n "$NAMESPACE" delete pod "$pod" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  if [[ "$maintenance_started" == true && "$restore_succeeded" != true ]]; then
    fail_closed
    echo "Restore failed: Flux root, source, workload Kustomizations, and coop-api remain suspended/stopped." >&2
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Enter maintenance before querying any active workload. If any operation below
# fails, the EXIT trap retries the complete fail-closed boundary.
maintenance_started=true
for resource in "${suspend_order[@]}"; do
  suspend_resource "$resource"
done
kubectl -n "$NAMESPACE" patch cronjob postgres-backup --type=merge \
  -p '{"spec":{"suspend":true}}' >/dev/null
for resource in "${suspend_order[@]}"; do
  kind="${resource%%/*}"
  name="${resource#*/}"
  [[ "$(kubectl -n flux-system get "$kind" "$name" -o jsonpath='{.spec.suspend}')" == true ]] || {
    echo "Failed to verify suspension of ${resource}." >&2
    exit 5
  }
done
[[ "$(kubectl -n "$NAMESPACE" get cronjob postgres-backup -o jsonpath='{.spec.suspend}')" == true ]] || {
  echo "Failed to verify suspension of cronjob/postgres-backup." >&2
  exit 5
}

assert_no_active_writers() {
  local active_jobs
  if ! active_jobs="$(kubectl -n "$NAMESPACE" get jobs -l coop.example.com/postgres-client=true \
    -o jsonpath='{range .items[?(@.status.active==1)]}{.metadata.name}{"\n"}{end}')"; then
    echo "Could not determine active PostgreSQL writer Jobs; refusing restore." >&2
    return 6
  fi
  if [[ -n "$active_jobs" ]]; then
    echo "Refusing restore while an active PostgreSQL writer Job exists." >&2
    return 4
  fi
}

assert_no_active_writers
kubectl -n "$NAMESPACE" scale deployment/coop-api --replicas=0
kubectl -n "$NAMESPACE" rollout status deployment/coop-api --timeout=180s
# Close the boundary against a Job which became active while an already-running
# Flux reconciliation was winding down after suspension.
assert_no_active_writers
[[ "$(kubectl -n "$NAMESPACE" get deployment coop-api -o jsonpath='{.spec.replicas}')" == 0 ]] || {
  echo "coop-api did not remain scaled to zero; refusing restore." >&2
  exit 7
}

kubectl -n "$NAMESPACE" apply -f - <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: ${pod}
  labels:
    app.kubernetes.io/name: coop-restore
    coop.example.com/postgres-client: "true"
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  securityContext:
    runAsNonRoot: true
    runAsUser: 70
    runAsGroup: 70
    fsGroup: 70
    seccompProfile: {type: RuntimeDefault}
  containers:
    - name: restore
      image: docker.io/library/postgres:17-alpine@sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24
      command: ["sleep", "3600"]
      env:
        - {name: PGHOST, value: postgres}
        - name: PGDATABASE
          valueFrom:
            secretKeyRef: {name: coop-runtime, key: POSTGRES_DB}
        - name: PGUSER
          valueFrom:
            secretKeyRef: {name: coop-runtime, key: POSTGRES_USER}
        - name: PGPASSWORD
          valueFrom:
            secretKeyRef: {name: coop-runtime, key: POSTGRES_PASSWORD}
      resources:
        requests: {cpu: 100m, memory: 128Mi}
        limits: {cpu: "1", memory: 512Mi}
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities: {drop: ["ALL"]}
      volumeMounts:
        - {name: backups, mountPath: /backups}
        - {name: tmp, mountPath: /tmp}
  volumes:
    - name: backups
      persistentVolumeClaim: {claimName: postgres-backups}
    - name: tmp
      emptyDir: {}
YAML
kubectl -n "$NAMESPACE" wait --for=condition=Ready "pod/${pod}" --timeout=180s

# The single-quoted script is intentionally evaluated inside the restore pod.
# shellcheck disable=SC2016
kubectl -n "$NAMESPACE" exec "$pod" -- sh -ec '
  set -eu
  dump="/backups/$1"
  test -f "$dump"
  pg_restore --list "$dump" >/dev/null
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  tmp="/backups/.pre-restore-${stamp}.dump.tmp"
  trap '\''rm -f "$tmp"'\'' EXIT INT TERM
  pg_dump --format=custom --no-owner --no-acl --file="$tmp"
  pg_restore --list "$tmp" >/dev/null
  mv "$tmp" "/backups/pre-restore-${stamp}.dump"
  trap - EXIT INT TERM
  psql --dbname="$PGDATABASE" --set=ON_ERROR_STOP=1 --command="SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid();" || true
  pg_restore --clean --if-exists --no-owner --no-acl --exit-on-error --single-transaction --dbname="$PGDATABASE" "$dump"
' sh "$DUMP"

resume_and_reconcile() {
  local resource="$1" kind name requested_at
  [[ "${was_suspended[$resource]}" != true ]] || return 0
  kind="${resource%%/*}"
  name="${resource#*/}"
  requested_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  kubectl -n flux-system patch "$kind" "$name" --type=merge \
    -p '{"spec":{"suspend":false}}' >/dev/null
  kubectl -n flux-system annotate --overwrite "$resource" \
    "reconcile.fluxcd.io/requestedAt=${requested_at}" >/dev/null
  kubectl -n flux-system wait --for=condition=Ready "$resource" --timeout=10m >/dev/null
}

for resource in "${resume_order[@]}"; do
  resume_and_reconcile "$resource"
  if [[ "$resource" == kustomization/coop-apps ]]; then
    kubectl -n "$NAMESPACE" scale deployment/coop-api --replicas="${replicas:-2}"
    kubectl -n "$NAMESPACE" rollout status deployment/coop-api --timeout=300s
  fi
done
if [[ "$backup_cron_was_suspended" == true ]]; then
  cron_suspend=true
else
  cron_suspend=false
fi
kubectl -n "$NAMESPACE" patch cronjob postgres-backup --type=merge \
  -p "{\"spec\":{\"suspend\":${cron_suspend}}}" >/dev/null
restore_succeeded=true
echo "Restore completed from ${DUMP}; API replicas restored to ${replicas}."
