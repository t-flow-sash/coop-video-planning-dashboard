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
apps_was_suspended="$(kubectl -n flux-system get kustomization coop-apps -o jsonpath='{.spec.suspend}')"
restore_succeeded=false
maintenance_started=false
cleanup() {
  kubectl -n "$NAMESPACE" delete pod "$pod" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  if [[ "$maintenance_started" != true ]]; then
    return
  fi
  if [[ "$restore_succeeded" == true ]]; then
    if [[ "$apps_was_suspended" != true ]]; then
      kubectl -n flux-system patch kustomization coop-apps --type=merge \
        -p '{"spec":{"suspend":false}}' >/dev/null 2>&1 || true
    fi
  else
    kubectl -n "$NAMESPACE" scale deployment/coop-api --replicas=0 >/dev/null 2>&1 || true
    echo "Restore failed: coop-api remains at zero and Flux coop-apps remains suspended." >&2
  fi
}
trap cleanup EXIT INT TERM

if kubectl -n "$NAMESPACE" get jobs -l app.kubernetes.io/component=migration \
  -o jsonpath='{range .items[?(@.status.active==1)]}{.metadata.name}{"\n"}{end}' | grep -q .; then
  echo "Refusing restore while a migration Job is active." >&2
  exit 4
fi
kubectl -n flux-system patch kustomization coop-apps --type=merge \
  -p '{"spec":{"suspend":true}}' >/dev/null
maintenance_started=true
kubectl -n "$NAMESPACE" scale deployment/coop-api --replicas=0
kubectl -n "$NAMESPACE" rollout status deployment/coop-api --timeout=180s

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
      image: postgres:17-alpine
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
  pg_dump --format=custom --no-owner --no-acl --file="$tmp"
  pg_restore --list "$tmp" >/dev/null
  mv "$tmp" "/backups/pre-restore-${stamp}.dump"
  psql --dbname="$PGDATABASE" --set=ON_ERROR_STOP=1 --command="SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid();" || true
  pg_restore --clean --if-exists --no-owner --no-acl --exit-on-error --single-transaction --dbname="$PGDATABASE" "$dump"
' sh "$DUMP"

kubectl -n "$NAMESPACE" scale deployment/coop-api --replicas="${replicas:-2}"
kubectl -n "$NAMESPACE" rollout status deployment/coop-api --timeout=300s
restore_succeeded=true
echo "Restore completed from ${DUMP}; API replicas will be restored to ${replicas}."
