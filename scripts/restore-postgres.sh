#!/usr/bin/env bash
set -Eeuo pipefail

NAMESPACE="${NAMESPACE:-coop-prod}"
DUMP="${1:-}"
CONFIRM="${2:-}"
RECONCILE_ATTEMPTS="${RECONCILE_ATTEMPTS:-120}"
RECONCILE_POLL_SECONDS="${RECONCILE_POLL_SECONDS:-5}"
LOCK_NAME="coop-postgres-restore-lock"
[[ "$DUMP" =~ ^(daily|weekly|pre-restore)-[0-9]{8}T[0-9]{6}Z\.dump$ ]] || {
  echo "Usage: $0 <daily|weekly|pre-restore>-YYYYMMDDTHHMMSSZ.dump --yes" >&2
  exit 2
}
[[ "$CONFIRM" == "--yes" ]] || { echo "Restore is destructive; pass --yes." >&2; exit 2; }
command -v kubectl >/dev/null || { echo "kubectl is required" >&2; exit 127; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 127; }

readonly suspend_order=(
  kustomization/coop-root
  gitrepository/coop-video-planning
  kustomization/coop-backup
  kustomization/coop-ingress
  kustomization/coop-apps
  kustomization/coop-migration
  kustomization/coop-import
  kustomization/coop-database
)
readonly resume_order=(
  gitrepository/coop-video-planning
  kustomization/coop-database
  kustomization/coop-migration
  kustomization/coop-import
  kustomization/coop-apps
  kustomization/coop-ingress
  kustomization/coop-backup
  kustomization/coop-root
)
declare -A was_suspended=()
pod=""
lock_acquired=false
restore_succeeded=false
maintenance_started=false

suspend_resource() {
  local resource="$1" kind name
  kind="${resource%%/*}"
  name="${resource#*/}"
  kubectl -n flux-system patch "$kind" "$name" --type=merge \
    -p '{"spec":{"suspend":true}}' >/dev/null
}

wait_for_flux_quiescence() {
  local resource="$1" kind name resource_json attempt
  kind="${resource%%/*}"
  name="${resource#*/}"
  for ((attempt = 1; attempt <= RECONCILE_ATTEMPTS; attempt++)); do
    resource_json="$(kubectl -n flux-system get "$kind" "$name" -o json)"
    if python3 -c '
import json, sys
obj = json.load(sys.stdin)
reconciling = any(
    condition.get("type") == "Reconciling" and condition.get("status") == "True"
    for condition in (obj.get("status", {}).get("conditions") or [])
)
raise SystemExit(1 if reconciling else 0)
' <<<"$resource_json"; then
      return 0
    fi
    sleep "$RECONCILE_POLL_SECONDS"
  done
  echo "Timed out waiting for Flux quiescence of ${resource}." >&2
  return 10
}

fail_closed() {
  local resource
  for resource in "${suspend_order[@]}"; do
    suspend_resource "$resource" >/dev/null 2>&1 || true
  done
  kubectl -n "$NAMESPACE" patch cronjob postgres-backup --type=merge \
    -p '{"spec":{"suspend":true}}' >/dev/null 2>&1 || true
  kubectl -n "$NAMESPACE" patch cronjob coop-seed --type=merge \
    -p '{"spec":{"suspend":true}}' >/dev/null 2>&1 || true
  for cronjob in gmail-import-full gmail-import-bootstrap gmail-import-delta; do
    kubectl -n "$NAMESPACE" patch cronjob "$cronjob" --type=merge \
      -p '{"spec":{"suspend":true}}' >/dev/null 2>&1 || true
  done
  kubectl -n "$NAMESPACE" scale deployment/coop-api --replicas=0 >/dev/null 2>&1 || true
}

cleanup() {
  local status=$?
  trap - EXIT
  if [[ -n "$pod" ]]; then
    kubectl -n "$NAMESPACE" delete pod "$pod" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  fi
  if [[ "$maintenance_started" == true && "$restore_succeeded" != true ]]; then
    fail_closed
    echo "Restore failed: Flux root, source, workload Kustomizations, and coop-api remain suspended/stopped." >&2
  fi
  if [[ "$lock_acquired" == true ]]; then
    kubectl -n "$NAMESPACE" delete configmap "$LOCK_NAME" --ignore-not-found >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

lock_holder="$(date -u +%Y%m%dT%H%M%S)-$$-${RANDOM}"
if ! kubectl -n "$NAMESPACE" create configmap "$LOCK_NAME" \
  --from-literal="holder=${lock_holder}" >/dev/null; then
  echo "Could not acquire cluster-wide PostgreSQL restore lock; another restore may be running." >&2
  exit 8
fi
lock_acquired=true

replicas="$(kubectl -n "$NAMESPACE" get deployment coop-api -o jsonpath='{.spec.replicas}')"
for resource in "${suspend_order[@]}"; do
  kind="${resource%%/*}"
  name="${resource#*/}"
  was_suspended["$resource"]="$(kubectl -n flux-system get "$kind" "$name" -o jsonpath='{.spec.suspend}')"
  if [[ "${was_suspended[$resource]}" == true ]]; then
    echo "Refusing restore because dependency ${resource} was already suspended." >&2
    exit 9
  fi
done
backup_cron_was_suspended="$(kubectl -n "$NAMESPACE" get cronjob postgres-backup -o jsonpath='{.spec.suspend}')"

# Enter maintenance before querying any active workload. If any operation below
# fails, the EXIT trap retries the complete fail-closed boundary.
maintenance_started=true
for resource in "${suspend_order[@]}"; do
  suspend_resource "$resource"
done
for resource in "${suspend_order[@]}"; do
  kind="${resource%%/*}"
  name="${resource#*/}"
  [[ "$(kubectl -n flux-system get "$kind" "$name" -o jsonpath='{.spec.suspend}')" == true ]] || {
    echo "Failed to verify suspension of ${resource}." >&2
    exit 5
  }
done
# spec.suspend prevents new reconciles but does not cancel one already applying
# objects. Drain every in-flight Flux operation, then re-read all suspension
# boundaries before touching CronJobs or querying PostgreSQL writers.
for resource in "${suspend_order[@]}"; do
  wait_for_flux_quiescence "$resource"
done
for resource in "${suspend_order[@]}"; do
  kind="${resource%%/*}"
  name="${resource#*/}"
  [[ "$(kubectl -n flux-system get "$kind" "$name" -o jsonpath='{.spec.suspend}')" == true ]] || {
    echo "Flux boundary ${resource} changed while draining reconciliation." >&2
    exit 5
  }
done
# CronJobs remain live objects after their owning Flux Kustomization is
# suspended. Close every schedule race explicitly before the first writer
# query; seed and every Gmail collector are never resumed here.
for cronjob in postgres-backup coop-seed gmail-import-full gmail-import-bootstrap gmail-import-delta; do
  kubectl -n "$NAMESPACE" patch cronjob "$cronjob" --type=merge \
    -p '{"spec":{"suspend":true}}' >/dev/null
  [[ "$(kubectl -n "$NAMESPACE" get cronjob "$cronjob" -o jsonpath='{.spec.suspend}')" == true ]] || {
    echo "Failed to verify suspension of cronjob/${cronjob}." >&2
    exit 5
  }
done

verify_delta_quarantine() {
  local delta_json
  delta_json="$(kubectl -n "$NAMESPACE" get cronjob gmail-import-delta -o json)"
  python3 -c '
import json, sys
obj = json.load(sys.stdin)
annotations = obj.get("metadata", {}).get("annotations") or {}
valid = (
    obj.get("spec", {}).get("suspend") is True
    and annotations.get("kustomize.toolkit.fluxcd.io/reconcile") == "disabled"
)
raise SystemExit(0 if valid else 1)
' <<<"$delta_json"
}

# The reviewed desired state normally keeps Delta active. During restore the
# live CronJob must remain suspended while coop-import is reconciled. Flux's
# resource-level quarantine prevents both apply and prune until a separately
# reviewed post-restore recovery removes this annotation.
kubectl -n "$NAMESPACE" patch cronjob gmail-import-delta --type=merge \
  -p '{"metadata":{"annotations":{"kustomize.toolkit.fluxcd.io/reconcile":"disabled"}},"spec":{"suspend":true}}' >/dev/null
verify_delta_quarantine || {
  echo "Failed to establish Flux quarantine for cronjob/gmail-import-delta." >&2
  exit 5
}

assert_no_active_writers() {
  local jobs_json dangerous_jobs
  if ! jobs_json="$(kubectl -n "$NAMESPACE" get jobs \
    -l coop.example.com/postgres-client=true -o json)"; then
    echo "Could not determine active PostgreSQL writer Jobs; refusing restore." >&2
    return 6
  fi
  if ! dangerous_jobs="$(python3 -c '
import json, sys
jobs = json.load(sys.stdin).get("items", [])
for job in jobs:
    conditions = job.get("status", {}).get("conditions") or []
    terminal = any(
        condition.get("type") in {"Complete", "Failed"} and condition.get("status") == "True"
        for condition in conditions
    )
    if not terminal:
        print(job.get("metadata", {}).get("name", "<unnamed-job>"))
' <<<"$jobs_json")"; then
    echo "Could not parse PostgreSQL writer Job status; refusing restore." >&2
    return 6
  fi
  if [[ -n "$dangerous_jobs" ]]; then
    echo "Refusing restore while an active or pending PostgreSQL writer Job exists: ${dangerous_jobs//$'\n'/, }." >&2
    return 4
  fi
}

assert_no_active_writers
kubectl -n "$NAMESPACE" scale deployment/coop-api --replicas=0
kubectl -n "$NAMESPACE" rollout status deployment/coop-api --timeout=180s
# Close the boundary against a Job which appeared while an already-running
# reconciliation was winding down after suspension.
assert_no_active_writers
[[ "$(kubectl -n "$NAMESPACE" get deployment coop-api -o jsonpath='{.spec.replicas}')" == 0 ]] || {
  echo "coop-api did not remain scaled to zero; refusing restore." >&2
  exit 7
}

pod="$(kubectl -n "$NAMESPACE" create -f - -o jsonpath='{.metadata.name}' <<'YAML'
apiVersion: v1
kind: Pod
metadata:
  generateName: coop-restore-
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
)"
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
  psql --dbname="$PGDATABASE" --set=ON_ERROR_STOP=1 --command="SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid();"
  pg_restore --clean --if-exists --no-owner --no-acl --exit-on-error --single-transaction --dbname="$PGDATABASE" "$dump"
' sh "$DUMP"

fresh_reconcile_observed() {
  local resource_json="$1" expected_generation="$2" requested_at="$3"
  python3 -c '
import json, sys
obj = json.load(sys.stdin)
status = obj.get("status") or {}
conditions = status.get("conditions") or []
ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)
try:
    observed = int(status.get("observedGeneration", 0))
    expected = int(sys.argv[1])
except (TypeError, ValueError):
    raise SystemExit(1)
request_handled = not sys.argv[2] or status.get("lastHandledReconcileAt") == sys.argv[2]
valid = observed >= expected and request_handled and ready
raise SystemExit(0 if valid else 1)
' "$expected_generation" "$requested_at" <<<"$resource_json"
}

resume_and_reconcile() {
  local resource="$1" kind name requested_at expected_generation resource_json attempt
  kind="${resource%%/*}"
  name="${resource#*/}"
  expected_generation="$(kubectl -n flux-system patch "$kind" "$name" --type=merge \
    -p '{"spec":{"suspend":false}}' -o jsonpath='{.metadata.generation}')"
  requested_at=""
  # coop-root manages its own manifest. A dynamic requestedAt annotation can
  # be removed by that self-reconcile before the controller records it. Its
  # suspend transition still increments generation, so fresh observedGeneration
  # plus Ready is the strict, race-free completion proof for this one resource.
  if [[ "$resource" != kustomization/coop-root ]]; then
    requested_at="$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)"
    kubectl -n flux-system annotate --overwrite "$resource" \
      "reconcile.fluxcd.io/requestedAt=${requested_at}" >/dev/null
  fi
  for ((attempt = 1; attempt <= RECONCILE_ATTEMPTS; attempt++)); do
    resource_json="$(kubectl -n flux-system get "$kind" "$name" -o json)"
    if fresh_reconcile_observed "$resource_json" "$expected_generation" "$requested_at"; then
      return 0
    fi
    sleep "$RECONCILE_POLL_SECONDS"
  done
  echo "Timed out waiting for fresh reconciliation of ${resource}." >&2
  return 10
}

for resource in "${resume_order[@]}"; do
  resume_and_reconcile "$resource"
  if [[ "$resource" == kustomization/coop-import ]]; then
    verify_delta_quarantine || {
      echo "Flux quarantine for cronjob/gmail-import-delta was lost during coop-import reconciliation." >&2
      exit 5
    }
  fi
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
verify_delta_quarantine || {
  echo "Flux quarantine for cronjob/gmail-import-delta did not survive reconciliation." >&2
  exit 5
}
restore_succeeded=true
echo "Restore completed from ${DUMP}; API replicas restored to ${replicas}."
