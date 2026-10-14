# GitOps M2: Produktionsbasis

## Ziel und Grenzen

Diese Basis beschreibt die deklarative Auslieferung nach `coop-prod` auf dem dedizierten Single-Node-Host `192.168.2.94`. Phase B1 installiert und prüft nur Hostplattform, lokale Registry, Schlüsselmaterial, verschlüsselte Manifeste und immutable Images. Sie führt **kein** Flux-Reconcile der Anwendung, keinen Seed und keinen Merge nach `main` aus. Erst Phase B2 installiert bzw. aktiviert die versionierten Flux-Controller und Bootstrap-Secrets und reconciliiert den bereits geprüften Stand aus `main`.

Die Registry ist ausschließlich auf `127.0.0.1:5000` gebunden; Datenbank und Anwendungsservices bleiben ClusterIP. Traefik ist der einzige spätere LAN-Einstieg auf 80/443. Diese Loopback-Registry ist absichtlich nur für einen einzelnen k3s-Node geeignet.

Ein reines k3s-API-Binding auf `127.0.0.1` wurde verworfen: dadurch war das Kubernetes-Service-IP-Ziel `10.43.0.1:443` aus Pods nicht erreichbar und CoreDNS, local-path sowie Traefik blieben ungesund. Der API-Server bindet deshalb an `192.168.2.94:6443` (zusätzlich erzeugt k3s lokale Listener). UFW bleibt in B1 bewusst inaktiv; vor jeder Internet-/WAN-Exposition muss B2 Port 6443 auf Loopback bzw. das vertrauenswürdige Management-LAN beschränken und anschließend Pod-/Service-CIDRs erneut testen.

## Tatsächliche B1-Hostbasis

| Komponente | Gepinnte Version |
|---|---|
| Docker Engine | 29.9.1 (`docker-ce` 5:29.9.1-1~ubuntu.26.04~resolute) |
| containerd.io (Docker) | 2.4.2-2 |
| k3s / Kubernetes | v1.36.5+k3s1 / v1.36.5 |
| Flux CLI / Controller-Export | v2.9.6 |
| SOPS | v3.13.3 |
| age | v1.3.2 |
| Distribution Registry | v3.1.2, OCI-Digest `sha256:21078ecfcba8a0cd7e1a384f75b3509899661dd21823bf1da21b5a4674aaceb9` |

Private Bootstrap-Dateien liegen root-only unter `/root/.config/coop-dashboard/`; Zugangsdaten stehen in `/root/.config/coop-dashboard/credentials.env` (Benutzer `sascha`). Private Schlüssel oder Passwörter dürfen niemals in Tickets, Logs oder Git kopiert werden. Die öffentliche lokale CA liegt zusätzlich unter `docs/certificates/coop-local-ca.crt` und kann auf Clients in den jeweiligen System-Truststore importiert werden. Danach Browser vollständig neu starten und die Kette gegen `https://192.168.2.94/` prüfen.

## Architektur und Reihenfolge

```text
GitRepository (read-only SSH deploy key)
  └─ coop-database (Namespace, Default-Deny, PostgreSQL 17, 10 Gi)
      └─ coop-migration (releasebezogener Alembic Job, Seed suspendiert)
          └─ coop-apps (API + Web, je Deployment und ClusterIP Service)
              └─ coop-ingress (Traefik HTTPS + HTTP→HTTPS)
                  └─ coop-backup (täglicher pg_dump auf separatem 10-Gi-PVC)
```

`clusters/production/flux-kustomizations.yaml` bildet diese Kette mit `dependsOn`, `wait` und Timeouts ab. Eine fehlgeschlagene Migration verhindert damit Apps, Ingress und Backup der neuen Revision. Das Migrations-Job-Objekt trägt die Git-SHA im Namen; das Promotion-Skript erzeugt für jede Revision einen neuen Job, statt einen abgeschlossenen Job wiederzuverwenden.

PostgreSQL läuft als einzelnes StatefulSet mit ClusterIP. `persistentVolumeClaimRetentionPolicy` behält den Daten-PVC bei Scale-down und Löschen des StatefulSets. Der Backup-PVC trägt zusätzlich `kustomize.toolkit.fluxcd.io/prune: disabled`. Die StorageClass/PV-Reclaim-Policy muss der Betreiber ebenfalls auf `Retain` prüfen; diese repo-lokale Basis kann die clusterweite StorageClass nicht festlegen.

## Vorbereitungen und Bootstrap

Vorausgesetzt werden ein bereits betriebener Cluster mit funktionierendem CNI/NetworkPolicy, Traefik-CRDs, Flux-Controllern, einer erreichbaren Registry auf `127.0.0.1:5000` aus Sicht des Build-Hosts **und der Cluster-Nodes**, einer dynamischen RWO-StorageClass sowie lokal `git`, `docker`/Buildx, `sops`, `age` und `kubectl` oder `kustomize`.

1. `clusters/production/source.yaml` auf die echte SSH-Repository-URL und den gewünschten Branch setzen. Der SSH Deploy Key erhält im Git-Provider ausschließlich Leserechte.
2. Den privaten Deploy Key nur im Cluster anlegen, nie im Repository:
   ```bash
   flux create secret git coop-git-readonly \
     --namespace=flux-system \
     --url=ssh://git@github.com/t-flow-sash/coop-video-planning-dashboard.git \
     --private-key-file="/root/.config/coop-dashboard/flux-github-readonly"
   ```
   `clusters/production/flux-git-auth.secret.example.yaml` dokumentiert lediglich Form und erforderliche Keys und enthält absichtlich keine Werte/private Keys.
3. Ein Age-Keypaar offline erzeugen. Den **öffentlichen** Empfänger in `.sops.yaml` ersetzen; den privaten Schlüssel nur als `sops-age` in `flux-system` hinterlegen:
   ```bash
   age-keygen -o age.key
   age-keygen -y age.key
   kubectl -n flux-system create secret generic sops-age --from-file=age.agekey=age.key
   ```
4. Die produktiven Secrets `coop-runtime`, `coop-basic-auth` und `coop-tls` liegen bereits SOPS-age-verschlüsselt im Repository. Nur die root-only Quelldaten unter `/root/.config/coop-dashboard/` dürfen zur Rotation verwendet werden. Klartextdateien niemals committen.
5. Ingress ist bewusst host-unabhängig und path-basiert; `/api/v1` hat Vorrang vor `/`. `CORS_ORIGIN` ist exakt `https://192.168.2.94`.
6. Mit `kubectl kustomize` lokal rendern. Erst nach Review, grüner CI und Merge installiert Phase B2 die Controller, erzeugt die beiden Flux-Bootstrap-Secrets mit `scripts/bootstrap-production-host.sh --create-flux-secrets` und wendet die Root-Kustomization an.

## Security-Modell

- Namespace `coop-prod` erzwingt Pod Security `restricted`.
- Workloads laufen ohne Service-Account-Token, non-root, mit `RuntimeDefault`-Seccomp, ohne Linux-Capabilities und ohne Privilege Escalation. App-/Job-Dateisysteme sind read-only; nur explizite `emptyDir`/PVC-Mounts sind schreibbar.
- CPU-/Memory-Requests und Limits sowie Startup-, Readiness- und Liveness-Probes sind definiert.
- `default-deny` blockiert Ingress und Egress. Explizit erlaubt sind DNS zu CoreDNS, Traefik→API/Web und mit `coop.example.com/postgres-client=true` markierte API-/Migration-/Seed-/Backup-/Restore-Pods→PostgreSQL.
- Traefik bedient HTTPS auf `websecure` (Port 443 in der Clusterinstallation) und leitet HTTP permanent auf HTTPS um. `/api/v1` hat höhere Priorität als `/`.
- BasicAuth liegt auf beiden fachlichen HTTPS-Routen. Nur die höher priorisierten Exact-Routen für `/api/v1/health/live` und `/api/v1/health/ready` bleiben ohne Middleware. `removeHeader: false` sorgt dafür, dass der `Authorization`-Header an API/Web weitergereicht wird. Damit sieht die API bei BasicAuth denselben Basic-Header; ein zweites Bearer-Schema kann nicht gleichzeitig denselben HTTP-Header nutzen und müsste später über ForwardAuth/SSO gelöst werden.
- TLS referenziert ausschließlich Secret `coop-tls`. Zertifikate und htpasswd-Daten stehen nicht im Klartext im Git.
- Die PostgreSQL- und Backup-Images sind auf Major `17` begrenzt; für einen streng reproduzierbaren Betrieb sollten Betreiber diese nach eigener Supply-Chain-Prüfung zusätzlich auf freigegebene Digests pinnen.

## Build und Promote

```bash
scripts/build-and-push-images.sh
```

Das Skript:

1. verweigert Builds mit nicht committed Änderungen an App-/Buildinputs,
2. baut API und Web aus `apps/api/Dockerfile` und `apps/web/Dockerfile` mit der vollständigen Git-Revision als OCI-Label,
3. taggt ausschließlich `git-<12-stellige-SHA>` (nie `latest`),
4. pusht nach `127.0.0.1:5000`,
5. liest und validiert beide Registry-Digests,
6. aktualisiert die `images`-Einträge in `infrastructure/apps/kustomization.yaml` und `infrastructure/migration/kustomization.yaml` deterministisch auf `Tag@Digest`,
7. setzt den Migrations-Jobnamen auf dieselbe SHA und rendert Kustomize, sofern ein Renderer verfügbar ist.

Das Skript führt kein `kubectl apply`, kein Flux-Reconcile und keinen Git-Commit/Push aus. Promotion ist: Diff prüfen, lokal rendern/testen, Manifeständerung reviewen und über den normalen Git-Prozess veröffentlichen. Die Cluster-Nodes müssen die Loopback-Registry unter genau dieser Adresse erreichen können; bei verteilten Nodes ist stattdessen eine explizit geplante Registry-Adresse nötig und Skript/Manifeste gemeinsam anzupassen.

Seed ist absichtlich kein Release-Schritt. `coop-seed` bleibt `suspend: true`. Falls fachlich genehmigt, einmalig starten:

```bash
kubectl -n coop-prod create job --from=cronjob/coop-seed "coop-seed-$(date -u +%Y%m%d%H%M%S)"
```

## Backup

`postgres-backup` läuft täglich um 02:17 Uhr `Europe/Berlin`, erzeugt mit PostgreSQL-17-Tools einen Custom-Format-Dump, validiert dessen Inhaltsverzeichnis und benennt die temporäre Datei erst danach atomar auf dem gleichen PVC um. Sonntags wird zusätzlich ein Weekly-Dump erzeugt. Aufbewahrung: die neuesten **7 Daily** und **4 Weekly** Dumps. Der separate RWO-PVC hat 10 Gi.

Operative Kontrolle:

```bash
kubectl -n coop-prod get cronjob postgres-backup
kubectl -n coop-prod create job --from=cronjob/postgres-backup postgres-backup-manual-$(date +%s)
kubectl -n coop-prod logs job/<JOBNAME>
```

Ein PVC ist kein Offsite-Backup. Produktiv ist zusätzlich ein verschlüsselter, regelmäßig getesteter Export in eine andere Failure Domain erforderlich. Kapazität und CronJob-Alarme überwachen.

## Restore-Runbook

Restore ist destruktiv und benötigt ein Wartungsfenster.

1. Gewünschten Dump anhand Job-Logs/Bestandsliste identifizieren; nur Dateinamen der Form `daily-*`, `weekly-*` oder `pre-restore-*` akzeptieren.
2. Aktuelle Git-/Image-/Migrationsrevision und Replica-Zahl dokumentieren. Schreibzugriffe am Ingress organisatorisch sperren; das Skript suspendiert `coop-apps` und skaliert die API auf null.
3. Sicherstellen, dass kein Migrationsjob aktiv ist und ausreichend Platz auf dem Backup-PVC vorhanden ist.
4. Ausführen:
   ```bash
   scripts/restore-postgres.sh daily-20260101T011700Z.dump --yes
   ```
5. Das Skript startet einen eingeschränkten PostgreSQL-17-Restore-Pod mit dem Backup-PVC, validiert den Dump, erstellt vorab atomar einen `pre-restore-*`-Sicherungsdump, beendet DB-Verbindungen und führt `pg_restore --clean --if-exists --exit-on-error --single-transaction` aus. Erst nach erfolgreichem Restore, API-Rollout und Readiness wird Flux wieder freigegeben; bei Fehlern bleiben API und Reconciliation fail-closed gestoppt.
6. Danach prüfen:
   ```bash
   kubectl -n coop-prod rollout status deployment/coop-api
   kubectl -n coop-prod exec statefulset/postgres -- pg_isready
   curl https://192.168.2.94/api/v1/health/ready
   ```
7. Bei Fehlern bleiben API auf null und `coop-apps` suspendiert. Logs und den automatisch erzeugten Pre-Restore-Dump sichern, Ursache beheben und erst danach Flux bewusst fortsetzen; nicht blind erneut migrieren.

## Rollback

- **App-only:** In Git die vorherigen geprüften `newTag`+`digest`-Werte wiederherstellen. Flux rollt Deployments deklarativ zurück.
- **Vorwärtskompatible DB-Migration:** App zurückrollen, Schema auf neuerem Stand belassen. Das ist der bevorzugte Migrationsstil.
- **Inkompatible/zerstörerische Migration:** Deployment stoppen und den vor Release erstellten Dump nach obigem Runbook wiederherstellen; anschließend gemeinsam App- und Migrationsrevision zurücksetzen. Ein bloßes Git-Revert macht Datenänderungen nicht rückgängig.
- PostgreSQL-/Backup-PVC niemals als Teil eines normalen Rollbacks löschen. Löschung erfordert separates Vier-Augen-Verfahren und ein verifiziertes Offsite-Backup.

## Akzeptanzmatrix

| Anforderung | Umsetzung / Nachweis |
|---|---|
| Namespace | `infrastructure/database/namespace.yaml`: `coop-prod`, restricted PSS |
| PostgreSQL 17, ClusterIP, 10 Gi retained | StatefulSet/Service; PVC-Template 10 Gi; RetentionPolicy `Retain` |
| API/Web Deploy+Svc | `infrastructure/apps/{api,web}.yaml`, je 2 Replicas, ClusterIP |
| HTTPS, Redirect, Routing | Traefik IngressRoutes: `websecure`, `/api/v1` vor `/`, `web`→HTTPS |
| BasicAuth + Authorization weiter | Middleware auf beiden fachlichen HTTPS-Routen, `removeHeader: false`; nur Exact-Health-Routen öffentlich |
| TLS Secret | `tls.secretName: coop-tls`; leeres Secret-Template ohne Werte |
| Hardening | Resources, drei Probe-Typen, non-root, seccomp, drop ALL, read-only FS |
| Netzwerk | Default-deny; DNS; Traefik→Apps; markierte Clients→PostgreSQL |
| Migration vor Apps | releasebezogener SHA-Job; Flux `dependsOn` und `wait` |
| Seed bewusst aus | suspendierter CronJob; ausschließlich manueller One-off-Start |
| Flux-Reihenfolge | DB→Migration→Apps→Ingress→Backup in `clusters/production` |
| Backup | täglich; eigener 10-Gi-PVC; validiert + atomic rename; 7 daily/4 weekly |
| Restore | `scripts/restore-postgres.sh` plus Wartungs-/Verifikationsrunbook |
| SOPS Age | `.sops.yaml`, Flux-Decryption, keine echten oder Fake-Secrets im Repo |
| Read-only Git SSH | GitRepository mit SecretRef; private Key nur clusterseitig |
| Immutable Images | SHA-Tag plus verifizierter Digest; kein `latest` |
| Keine Cluster-Push-Automation | Build-Skript endet nach Manifestupdate/Rendercheck |

## Lokale Validierung

```bash
bash -n scripts/build-and-push-images.sh scripts/restore-postgres.sh
for d in infrastructure/{database,migration,apps,ingress,backup} clusters/production; do
  kubectl kustomize "$d" >/dev/null   # oder: kustomize build "$d"
done
git diff --check
git status --short
```

Serverseitige OpenAPI-/CRD-Validierung erfordert einen passenden Cluster und erfolgt bewusst erst im kontrollierten Bootstrap-/Reviewprozess.
