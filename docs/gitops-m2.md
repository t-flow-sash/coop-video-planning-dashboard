# GitOps M2: Produktionsbasis

## Ziel und Grenzen

Diese Basis beschreibt die deklarative Auslieferung nach `coop-prod` auf dem dedizierten Single-Node-Host `192.168.2.94`. Phase B1 installiert und prüft nur Hostplattform, lokale Registry, Schlüsselmaterial, verschlüsselte Manifeste und immutable Images. Sie führt **kein** Flux-Reconcile der Anwendung, keinen Seed und keinen Merge nach `main` aus. Erst Phase B2 installiert in einem ersten, getrennten Schritt die versionierten Flux-Controller und aktiviert nach bestätigten CRDs/Controllern in einem zweiten Schritt Bootstrap-Secrets, Source und `coop-root` für den bereits geprüften Stand aus `main`.

Die Registry ist ausschließlich auf `127.0.0.1:5000` gebunden; Datenbank und Anwendungsservices bleiben ClusterIP. Traefik ist der einzige spätere LAN-Einstieg auf 80/443. Diese Loopback-Registry ist absichtlich nur für einen einzelnen k3s-Node geeignet.

Ein reines k3s-API-Binding auf `127.0.0.1` wurde verworfen: dadurch war das Kubernetes-Service-IP-Ziel `10.43.0.1:443` aus Pods nicht erreichbar und CoreDNS, local-path sowie Traefik blieben ungesund. Der API-Server bindet deshalb an `192.168.2.94:6443` (zusätzlich erzeugt k3s lokale Listener). UFW ist aktiv. Die verifizierte Grundpolicy lautet `Default: deny (incoming), allow (outgoing), deny (routed)`; die konkret erlaubten Pfade stehen unten. Das ist eine LAN-Basis, keine Freigabe für Internet-/WAN-Exposition.

## Tatsächliche B1-Hostbasis

| Komponente                   | Gepinnte Version                                                                                  |
| ---------------------------- | ------------------------------------------------------------------------------------------------- |
| Docker Engine                | 29.9.1 (`docker-ce` 5:29.9.1-1~ubuntu.26.04~resolute)                                             |
| containerd.io (Docker)       | 2.4.2-2                                                                                           |
| k3s / Kubernetes             | v1.36.5+k3s1 / v1.36.5                                                                            |
| Flux CLI / Controller-Export | v2.9.6                                                                                            |
| SOPS                         | v3.13.3                                                                                           |
| age                          | v1.3.2                                                                                            |
| Distribution Registry        | v3.1.2, Multiarch-Index `sha256:ddf754342cfc8acc51a56d5d0ab6af06826461864460636d8bd5c546dab2a7b8` |

Private Bootstrap-Dateien und Zugangsdaten liegen ausschließlich in einem root-only Verzeichnis außerhalb des Repositorys (Verzeichnis `0700`, Dateien `0600`). Private Schlüssel oder Passwörter dürfen niemals in Tickets, Logs oder Git kopiert werden. Die öffentliche lokale CA liegt zusätzlich unter `docs/certificates/coop-local-ca.crt` und kann auf Clients in den jeweiligen System-Truststore importiert werden. Danach Browser vollständig neu starten und die Kette gegen `https://192.168.2.94/` prüfen.

## Architektur und Reihenfolge

```text
GitRepository (read-only SSH deploy key)
  └─ coop-root (verwaltet clusters/production einschließlich sich selbst)
      └─ coop-storage (local-path StorageClass, Retain, WaitForFirstConsumer)
          └─ coop-database (Namespace, Default-Deny, PostgreSQL 17, 10 Gi)
              └─ coop-migration (releasebezogener Alembic Job, Seed suspendiert)
                  ├─ coop-import (read-only FULL suspendiert; Evidence-only Trust Boundary)
                  └─ coop-apps (API + Web, je Deployment und ClusterIP Service)
                      └─ coop-ingress (Traefik HTTPS + HTTP→HTTPS)
                          └─ coop-backup (täglicher pg_dump auf separatem 10-Gi-PVC)
```

`clusters/production/flux-kustomizations.yaml` bildet diese Kette mit `dependsOn`, `wait` und Timeouts ab. `coop-root` besitzt bewusst kein `dependsOn` auf sich selbst; der initial direkt angelegte Root und seine Git-Source vermeiden damit eine zirkuläre Startabhängigkeit. Eine fehlgeschlagene Migration verhindert Apps, Ingress und Backup der neuen Revision. Das Migrations-Job-Objekt trägt die Git-SHA im Namen; das Promotion-Skript erzeugt für jede Revision einen neuen Job, statt einen abgeschlossenen Job wiederzuverwenden.

PostgreSQL läuft als einzelnes StatefulSet mit ClusterIP und bindet den expliziten PVC `postgres-data`; Backups verwenden den getrennten expliziten PVC `postgres-backups`. Beide Claims und der Namespace tragen `kustomize.toolkit.fluxcd.io/prune: disabled`. Die repo-eigene StorageClass `coop-local-path` verwendet den vorhandenen k3s-Provisioner `rancher.io/local-path`, `reclaimPolicy: Retain` und `volumeBindingMode: WaitForFirstConsumer`. PVC-Prune-Schutz und PV-Reclaim-Schutz bleiben zwei getrennte Sicherungsebenen.

## Vorbereitungen und Bootstrap

Vorausgesetzt werden ein bereits betriebener Cluster mit funktionierendem CNI/NetworkPolicy und Traefik-CRDs, eine erreichbare Registry auf `127.0.0.1:5000` aus Sicht des Build-Hosts **und der Cluster-Nodes**, der k3s-local-path-Provisioner sowie lokal `git`, `docker`/Buildx, `flux` v2.9.6, `sops`, `age` und `kubectl` oder `kustomize`.

1. `clusters/production/source.yaml` auf die echte SSH-Repository-URL und den gewünschten Branch setzen. Der SSH Deploy Key erhält im Git-Provider ausschließlich Leserechte.
2. Den privaten Deploy Key nur im Cluster anlegen, nie im Repository. `scripts/bootstrap-production-host.sh --bootstrap-coop-root` verwendet ausschließlich den versionierten offiziellen Ed25519-Eintrag aus `config/github-known-hosts` und prüft vor jeder Cluster-Mutation dessen offiziellen GitHub-Fingerprint `SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU`. Es gibt kein ungeprüftes `ssh-keyscan`-Trust-on-first-use. `clusters/production/flux-git-auth.secret.example.yaml` dokumentiert lediglich Form und erforderliche Keys und enthält absichtlich keine Werte/private Keys.
3. Ein Age-Keypaar offline erzeugen. Den **öffentlichen** Empfänger in `.sops.yaml` ersetzen; den privaten Schlüssel nur als `sops-age` in `flux-system` hinterlegen:
   ```bash
   age-keygen -o age.key
   age-keygen -y age.key
   kubectl -n flux-system create secret generic sops-age --from-file=age.agekey=age.key
   ```
4. Die produktiven Secrets `coop-runtime`, `coop-basic-auth` und `coop-tls` liegen bereits SOPS-age-verschlüsselt im Repository. Nur Quelldaten aus dem root-only, repository-externen Secretspeicher dürfen zur Rotation verwendet werden. Klartextdateien niemals committen.
5. Ingress ist bewusst host-unabhängig und path-basiert; `/api/v1` hat Vorrang vor `/`. `CORS_ORIGIN` ist exakt `https://192.168.2.94`.
6. Vor jedem B2-Schritt die bereits aktive Firewall ausschließlich lesend und fail-closed prüfen; dieser Modus ändert weder UFW-Regeln noch Registry oder Cluster:
   ```bash
   scripts/bootstrap-production-host.sh --verify-firewall
   ```
7. Mit `kubectl kustomize` lokal rendern. Erst nach Review, grüner CI und Merge läuft Phase B2 explizit zweistufig und fail-closed:
   ```bash
   scripts/bootstrap-production-host.sh --install-flux-controllers
   scripts/bootstrap-production-host.sh --bootstrap-coop-root
   ```
   Stufe 1 wendet ausschließlich den gepinnten v2.9.6-Controller-Export an und wartet auf `Established` für die Flux-CRDs sowie `Available` für alle vier Controller. Stufe 2 prüft dieselben Bedingungen erneut, bevor sie Secrets mutiert, und legt anschließend nur GitRepository und den selbstverwalteten `coop-root` direkt an. Scheitert eine Prüfung, werden Source und Root nicht angelegt.

## Security-Modell

- Namespace `coop-prod` erzwingt Pod Security `restricted`.
- Workloads laufen ohne Service-Account-Token, non-root, mit `RuntimeDefault`-Seccomp, ohne Linux-Capabilities und ohne Privilege Escalation. App-/Job-Dateisysteme sind read-only; nur explizite `emptyDir`/PVC-Mounts sind schreibbar.
- CPU-/Memory-Requests und Limits sowie Startup-, Readiness- und Liveness-Probes sind definiert.
- `default-deny` blockiert Ingress und Egress. Explizit erlaubt sind DNS zu CoreDNS, Traefik→API/Web und mit `coop.example.com/postgres-client=true` markierte API-/Migration-/Seed-/Backup-/Restore-Pods→PostgreSQL.
- Die deklarative Flux-Overlay-Policy ersetzt das exportierte `egress: - {}` ohne Änderung an `gotk-components.yaml`: erlaubt bleiben CoreDNS (UDP/TCP 53), Git SSH (TCP 22), Git/OCI/API über HTTPS (TCP 443), der k3s-API-Endpunkt `192.168.2.94:6443` und controller-interne Kommunikation innerhalb `flux-system`.
- Traefik bedient HTTPS auf `websecure` (Port 443 in der Clusterinstallation) und leitet HTTP permanent auf HTTPS um. `/api/v1` hat höhere Priorität als `/`.
- BasicAuth liegt auf beiden fachlichen HTTPS-Routen. Nur die höher priorisierten Exact-Routen für `/api/v1/health/live` und `/api/v1/health/ready` bleiben ohne Middleware. API: `removeHeader: false`, damit der `Authorization`-Header an die API weitergereicht wird. Web: `removeHeader: true`, damit Browser-Credentials nicht beim Web-Upstream verbleiben. Ein zweites Bearer-Schema kann nicht gleichzeitig denselben API-Header nutzen und müsste später über ForwardAuth/SSO gelöst werden.
- TLS referenziert ausschließlich Secret `coop-tls`. Ein clusterweit eindeutiger Traefik-`TLSStore/default` verwendet dasselbe Secret auch für Clients ohne SNI. CA und Leaf wurden am 15. Oktober 2026 gemeinsam auf ECDSA P-256 rotiert; Metadaten und Installationshinweise stehen im folgenden Abschnitt. Zertifikate und htpasswd-Daten stehen nicht im Klartext im Git.
- PostgreSQL/Backup/Restore/CI verwenden den live verifizierten Multiarch-Index `postgres:17-alpine@sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24`. Dockerfile-Frontend und alle Dockerfile-Basen sind ebenfalls per Multiarch-Index gepinnt.
- Die App-Images wurden reproduzierbar aus dem sauberen Zwischencommit `a4b4d9f030ee` gebaut und in die lokale Registry gepusht: API `git-a4b4d9f030ee@sha256:ebe650d1273c88c6a5223be5d2e1a55a8a5a7207153e14849f78d99e95beca5a`, Web `git-a4b4d9f030ee@sha256:e987d6bb0796ea1c53108862adfd147d8660aca32eea6fcbbe717b6ce06ad0ff`.
- `gotk-components.yaml` bleibt bytegleich zum `flux install --version=v2.9.6 --export` (SHA-256 `e0b32861e2701c26336b1a25e5d2896d9f1bc0eed68c2115e2a80277a93cdf03`). Die vier Controller-Digests werden ausschließlich deklarativ in `flux-system/kustomization.yaml` ergänzt.
- Alle GitHub Actions sind auf vollständige 40-stellige Commit-SHAs gepinnt; Versionskommentare halten die Release-Zuordnung lesbar. `.github/dependabot.yml` lässt Dependabot wöchentlich geprüfte SHA-Aktualisierungen für `github-actions` vorschlagen.

### Lokale CA-Rotation vom 15. Oktober 2026

Die vorherige lokale CA besaß keine kritische `keyUsage`-Erweiterung. Dadurch akzeptierten curl und nicht-strikte OpenSSL-Prüfungen die Kette, NSS/Google Chrome lehnten sie jedoch mit unzureichender Zertifikatsverwendung ab. Da die CA noch nicht produktiv verteilt oder abgenommen war, wurde der Trust Anchor vor M2-Abnahme ersetzt; es gibt keine Übergangs- oder Dual-Trust-Zusage.

Öffentliche Metadaten der neuen Kette:

| Zertifikat | Schlüssel / Signatur | Gültigkeit (UTC) | SHA-256-Fingerprint |
| --- | --- | --- | --- |
| Lokale CA | ECDSA P-256 / ECDSA-SHA256 | 2026-10-15 01:15:54 bis 2036-10-12 01:15:54 | `F9:B4:F4:62:90:50:7D:B9:BF:53:0D:A0:56:88:60:15:6A:21:5D:C4:37:D3:3A:80:96:05:BF:62:7F:58:50:55` |
| Serverleaf | ECDSA P-256 / ECDSA-SHA256 | 2026-10-15 01:15:54 bis 2029-01-17 01:15:54 | `9D:6E:AE:45:72:39:52:71:61:5E:B9:A4:CF:63:84:8F:CC:3A:B5:56:05:17:2C:A5:10:59:15:85:A8:90:B7:59` |

Die CA hat `basicConstraints = critical, CA:TRUE, pathlen:0`, `keyUsage = critical, keyCertSign,cRLSign` sowie passende SKI/AKI. Das Leaf hat `basicConstraints = critical, CA:FALSE`, `keyUsage = critical, digitalSignature`, `extendedKeyUsage = serverAuth`, passende SKI/AKI und exakt die SANs `IP:192.168.2.94`, `DNS:zionhermes`, `DNS:coop.zionhermes.local`.

Clients müssen ein gegebenenfalls bereits importiertes altes Zertifikat mit gleichem Anzeigenamen entfernen und anschließend **diese** Datei `docs/certificates/coop-local-ca.crt` als vertrauenswürdige Zertifizierungsstelle importieren. Vor dem Import ist der SHA-256-Fingerprint über einen unabhängigen Kanal mit der obigen Angabe zu vergleichen. Danach alle Browserprozesse beenden und neu starten. Zertifikatswarnungen, globale Ignore-Flags oder SPKI-Ausnahmen sind kein zulässiger Ersatz für den Truststore-Import.

### Aktive UFW-Regeln und read-only Prüfung

Der Host wurde mit folgenden Allow-Regeln geprüft; jede nicht aufgeführte zusätzliche Allow-Regel lässt `--verify-firewall` fehlschlagen:

```text
22/tcp                     ALLOW IN    Anywhere
80/tcp                     ALLOW IN    192.168.2.0/24
443/tcp                    ALLOW IN    192.168.2.0/24
Anywhere                   ALLOW IN    10.42.0.0/16
Anywhere                   ALLOW IN    10.43.0.0/16
22/tcp (v6)                ALLOW IN    Anywhere (v6)
Anywhere                   ALLOW FWD   10.42.0.0/16
Anywhere                   ALLOW FWD   10.43.0.0/16
```

Manuell kontrollieren: `ufw status verbose` und `ufw status numbered`. Automatisiert ausschließlich lesen: `scripts/bootstrap-production-host.sh --verify-firewall`. Der Prüfer verlangt UFW `active`, exakt die Default-Policy und exakt obige Allow-Menge; bei Lesefehler oder Drift endet er ungleich null, ohne `ufw allow`, `ufw delete`, `ufw reset` oder `ufw enable` aufzurufen.

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
6. aktualisiert die `images`-Einträge in `infrastructure/apps/kustomization.yaml`, `infrastructure/migration/kustomization.yaml` und `infrastructure/import/kustomization.yaml` deterministisch auf `Tag@Digest`,
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
2. Aktuelle Git-/Image-/Migrationsrevision und Replica-Zahl dokumentieren. Das Skript suspendiert fail-closed zuerst `coop-root`, die Git-Source sowie `coop-database`, `coop-migration`, `coop-import`, `coop-apps`, `coop-ingress` und `coop-backup` und verifiziert diese Sperren. Danach suspendiert und verifiziert es die weiterhin eigenständig aktiven CronJobs `postgres-backup`, `coop-seed`, `gmail-import-full`, `gmail-import-bootstrap` und `gmail-import-delta`, bevor es zum ersten Mal aktive oder pending PostgreSQL-Writer prüft.
3. Bei einem aktiven Writer-Job oder einer nicht verifizierbaren Abfrage bricht das Skript ab; Flux und alle fünf Scheduler bleiben suspendiert und die API wird auf null gehalten. Auch nach Erfolg bleiben `coop-seed` und alle drei Gmail-Collector gemäß deklarativem Sollwert suspendiert; nur der Backup-CronJob wird kontrolliert auf seinen vorherigen Zustand gesetzt.
4. Ausführen:
   ```bash
   scripts/restore-postgres.sh daily-20260101T011700Z.dump --yes
   ```
5. Das Skript skaliert die API auf null, startet einen eingeschränkten PostgreSQL-17-Restore-Pod mit dem Backup-PVC, validiert den Dump, erstellt vorab atomar einen `pre-restore-*`-Sicherungsdump, beendet DB-Verbindungen und führt `pg_restore --clean --if-exists --exit-on-error --single-transaction` aus. Nach erfolgreichem Restore setzt es Source, Database, Migration, Import-Kustomization, Apps, Ingress, Backup und zuletzt Root kontrolliert fort, fordert jeweils einen Reconcile an und wartet auf `Ready`; die Gmail-CronJobs selbst bleiben suspendiert. Erst danach ist die Wartung beendet. Bei Fehlern bleiben alle Sperren und API null fail-closed bestehen.
6. Danach prüfen:
   ```bash
   kubectl -n coop-prod rollout status deployment/coop-api
   kubectl -n coop-prod exec statefulset/postgres -- pg_isready
   curl https://192.168.2.94/api/v1/health/ready
   ```
7. Bei Fehlern bleiben API auf null, Root/Source/Kinder sowie Backup-, Seed- und der Gmail-FULL-CronJob suspendiert. Logs und den automatisch erzeugten Pre-Restore-Dump sichern, Ursache beheben und erst danach Flux bewusst in Abhängigkeitsreihenfolge fortsetzen; nicht blind erneut migrieren.

## Retained-PV-Recovery nach Namespaceverlust

Dieses Runbook gilt für den tatsächlichen Verlust von `coop-prod`, wenn die local-path-PVs wegen `Retain` noch existieren. Ein normaler Reconcile ist jetzt gefährlich: fehlende PVCs können automatisch **leere neue Volumes** erhalten. Recovery ist deshalb fail-closed, Backup-First und wird bei jeder unklaren Identität abgebrochen. Alle Angaben in spitzen Klammern sind bewusst nicht ausführbare Platzhalter; insbesondere gibt es hier keinen blind kopierbaren Befehl zum Ändern einer `claimRef`.

1. **Unveränderlichen Checkpoint vor Suspension erstellen:** Noch vor jeder PV- oder `claimRef`-Mutation die relevanten PVs und alle vorhandenen PVCs clusterweit inventarisieren; auch das Fehlen erwarteter Objekte festhalten. Für `postgres-data` und `postgres-backups` mindestens Objekt-YAML/JSON, PV-/PVC-Namen, UIDs, alte `claimRef` (Namespace, Name, UID), Phase, Kapazität, StorageClass, `spec.local.path`/hostPath und NodeAffinity sowie Namespace-, Node- und StorageClass-Metadaten und Events exportieren. Den Export mit UTC-Zeit, Clusterkennung und kryptographischem Hash in einem schreibgeschützten/WORM-Ziel außerhalb beider local-path-PVs ablegen. Dieser unveränderliche Checkpoint ist die Vergleichsbasis; kann er nicht vollständig und unverändert gesichert werden: **Abbruch**.
2. **Source und alle Kustomizations suspendieren:** Vor jeder PV-/`claimRef`-Aktion zuerst die Git-Source `coop-git` (das aktuelle Objekt `GitRepository/coop-video-planning`) und ausnahmslos die Kustomizations `coop-root`, `coop-database`, `coop-migration`, `coop-import`, `coop-apps`, `coop-ingress` und `coop-backup` suspendieren. Für jedes einzelne Objekt den API-read-back von `spec.suspend=true` protokollieren; eine fehlende Source, Kustomization, API-Antwort oder Bestätigung ist ein **Abbruch**. Die CronJobs `postgres-backup`, `coop-seed` und `gmail-import-full` werden separat suspendiert und read-back-verifiziert. Alle verbliebenen App-, Migration-, Import-, Seed-, Backup- und PostgreSQL-Workloads bleiben aus. Bis Schritt 8 wird kein normales Reconcile ausgelöst.
3. **Echte Flux-Quieszenz beweisen:** `spec.suspend=true` allein genügt nicht, weil ein bereits laufender Reconcile noch Ressourcen anwenden kann. Für Source und jede der sieben Kustomizations noch laufende Controller-Operationen erkennen, auf deren Ende warten und danach aus dem jeweils frisch gelesenen Status belegen, dass **keine aktive `Reconciling=True`-Condition** vorhanden ist. Anschließend alle acht `spec.suspend=true`-Werte und die vier CronJob-Suspensionswerte erneut read-back-verifizieren. Bei API-Unklarheit, neuer `Reconciling=True`-Condition oder Timeout: **Abbruch**, keine Storage-Mutation.
4. **Inventur nach Quieszenz wiederholen:** Nach nachgewiesener Quieszenz dieselben PV-/PVC-, Namespace-, Node-, StorageClass- und Eventdaten erneut vollständig exportieren, hashen und unveränderlich neben dem ersten Checkpoint sichern. Noch immer keine PV-Metadaten ändern und keinen PVC anlegen. Vor jeder Metadatenänderung außerdem ein geprüftes externes Backup oder einen read-only Snapshot beider Hostpfade erstellen; wenn Datenbank- oder Backuppfad nicht konsistent lesbar gesichert werden kann: **Abbruch**.
5. **Storage-Drift fail-closed ausschließen:** Beide Inventuren maschinell und im Vier-Augen-Prinzip vergleichen. Neue oder ersetzte PVCs/PVs, geänderte UIDs oder `claimRefs`, abweichende Kapazität, StorageClass, hostPath oder NodeAffinity sowie unerwartete Claims clusterweit bedeuten: **Abbruch**. Released-PVs nur gemeinsam anhand alter Claim-Identität, UID, Kapazität, `coop-local-path`, Pfad und Node zuordnen; die Hostpfade ausschließlich read-only auf Typ, Besitzer/Rechte, Belegung und erwartete PostgreSQL- beziehungsweise Dump-Struktur prüfen. Zwei unabhängige Prüfer bestätigen `postgres-data` ↔ Datenpfad und `postgres-backups` ↔ Backuppfad; sie dürfen **niemals vertauscht** werden. Die automatische Neuprovisionierung bleibt blockiert: alle Flux-Grenzen bleiben suspendiert, normale deklarative PVCs bleiben unapplied, und es wird ausschließlich ein geprüftes, explizit vorgebundenes Recoverymanifest außerhalb des Kustomize-Pfads vorbereitet.
6. **Unmittelbaren Pre-Mutation-Gate wiederholen:** Noch einmal unmittelbar vor dem Lösen einer `claimRef` Source/Kustomization-Suspension, Abwesenheit von `Reconciling=True`, aktuelle PV-/PVC-Liste und den vollständigen Identitätssatz des genau einen Kandidaten erneut direkt aus der API lesen. Er muss byte-/wertgleich zum freigegebenen Post-Quieszenz-Checkpoint sein; auch jetzt sind unerwartete Claims oder eine nicht eindeutig lesbare API ein **Abbruch**. Das freigegebene Recovery-PVC muss `spec.volumeName: <BESTÄTIGTER-ALTER-PV>` enthalten; ein Manifest mit leerem/falschem Platzhalter darf nicht angewendet werden.
7. **claimRef kontrolliert lösen und gezielt binden:** Erst nach allen Gates die stale `claimRef` genau eines bestätigten Released-PV mit einem separat geprüften, auf dessen Ressourcenname und erwartete UID begrenzten Verfahren lösen; die konkrete Mutation wird absichtlich nicht als Copy-and-paste-Befehl dokumentiert. Unmittelbar danach ausschließlich das paarige Recovery-PVC mit `spec.volumeName` erzeugen. Für `postgres-data` ist nur der bestätigte Daten-PV, für `postgres-backups` nur der bestätigte Backup-PV zulässig. Auf exakt `Bound` warten und neue PVC-UID gegen PV-`claimRef.uid` sowie Kapazität, StorageClass, hostPath und NodeAffinity gegen den Checkpoint prüfen. Bei Ersatz-PV, `Pending`, falscher UID, Pfad-/Node-Abweichung oder Mehrdeutigkeit: **Abbruch**, Workloads bleiben aus. Erst nach vollständigem Nachweis darf derselbe Ablauf für das zweite Paar beginnen.
8. **Daten vor Start validieren:** Beide Bound-Zuordnungen erneut unveränderlich exportieren. Daten- und Backup-Pfad read-only gegen die bestätigten Identitäten prüfen. Zuerst den Backup-PVC mit einem eingeschränkten read-only Prüfpod, danach PostgreSQL ohne App-/Migration-/Seed-Writer kontrolliert validieren. Vor Schreibzugriff muss ein wiederherstellbarer Dump außerhalb beider local-path-PVs liegen. Jede Inkonsistenz ist ein Abbruchpunkt.
9. **Vollständig und einzeln fortsetzen:** Temporäre Recoverymanifeste archivieren, aber nicht in den normalen Kustomize-Pfad übernehmen. Genau in dieser Reihenfolge jeweils nur eine Grenze entsuspendieren, einen frischen reconcile anfordern und deren `Ready=True` für die aktuelle Generation abwarten: **Source** (`coop-git`/`GitRepository/coop-video-planning`) → `coop-database` → `coop-migration` → `coop-import` → `coop-apps` → `coop-ingress` → `coop-backup` → zuletzt `coop-root`. Root bleibt bis zum Abschluss aller fachlichen Kinder suspendiert, damit sein selbstverwaltender Reconcile die gestufte Freigabe nicht unterlaufen kann. Vor jedem nächsten Schritt müssen die bereits gestarteten Komponenten zusätzlich fachlich gesund sein. **Stop on failure:** Bei fehlendem `Ready`, Timeout oder jeder Abweichung nichts Weiteres fortsetzen, die bereits geöffneten Grenzen wieder suspendieren und den Fehler untersuchen. `coop-seed` und `gmail-import-full` bleiben suspendiert.

## Rollback

- **App-only:** In Git die vorherigen geprüften `newTag`+`digest`-Werte wiederherstellen. Flux rollt Deployments deklarativ zurück.
- **Vorwärtskompatible DB-Migration:** App zurückrollen, Schema auf neuerem Stand belassen. Das ist der bevorzugte Migrationsstil.
- **Inkompatible/zerstörerische Migration:** Deployment stoppen und den vor Release erstellten Dump nach obigem Runbook wiederherstellen; anschließend gemeinsam App- und Migrationsrevision zurücksetzen. Ein bloßes Git-Revert macht Datenänderungen nicht rückgängig.
- PostgreSQL-/Backup-PVC niemals als Teil eines normalen Rollbacks löschen. Löschung erfordert separates Vier-Augen-Verfahren und ein verifiziertes Offsite-Backup.

## Akzeptanzmatrix

| Anforderung                              | Umsetzung / Nachweis                                                                                                              |
| ---------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| Namespace                                | `infrastructure/database/namespace.yaml`: `coop-prod`, restricted PSS                                                             |
| PostgreSQL 17, ClusterIP, 10 Gi retained | StatefulSet/Service; expliziter `postgres-data`-PVC; `coop-local-path` mit PV-Reclaim `Retain`                                    |
| API/Web Deploy+Svc                       | `infrastructure/apps/{api,web}.yaml`, je 2 Replicas, ClusterIP                                                                    |
| HTTPS, Redirect, Routing                 | Traefik IngressRoutes: `websecure`, `/api/v1` vor `/`, `web`→HTTPS                                                                |
| BasicAuth + Authorization                | API `removeHeader: false`, Web `removeHeader: true`; nur Exact-Health-Routen öffentlich                                           |
| TLS Secret                               | `tls.secretName: coop-tls`; leeres Secret-Template ohne Werte                                                                     |
| Hardening                                | Resources, drei Probe-Typen, non-root, seccomp, drop ALL, read-only FS                                                            |
| Netzwerk                                 | Default-deny; DNS; Traefik→Apps; markierte Clients→PostgreSQL; eingeschränkter Flux-Egress                                        |
| Migration vor Apps                       | releasebezogener SHA-Job; Flux `dependsOn` und `wait`                                                                             |
| Seed bewusst aus                         | suspendierter CronJob; ausschließlich manueller One-off-Start                                                                     |
| Gmail-Import Trust Boundary              | `infrastructure/import`; read-only FULL suspendiert; Details in [`gmail-import-operations.md`](gmail-import-operations.md)          |
| Flux-Reihenfolge                         | Storage→DB→Migration→Import/Apps→Ingress→Backup in `clusters/production`                                                          |
| Backup                                   | täglich; eigener 10-Gi-PVC; validiert + atomic rename; 7 daily/4 weekly                                                           |
| Restore                                  | `scripts/restore-postgres.sh` plus Wartungs-/Verifikationsrunbook                                                                 |
| SOPS Age                                 | `.sops.yaml`, Flux-Decryption, keine echten oder Fake-Secrets im Repo                                                             |
| Read-only Git SSH                        | GitRepository mit SecretRef; private Key nur clusterseitig                                                                        |
| Immutable Images                         | App-SHA-Tag plus verifizierter Digest; Third-party-, Flux- und Dockerfile-Basen per live geprüftem Multiarch-Index; kein `latest` |
| Fresh Bootstrap                          | Zwei explizite Stufen; CRD-/Controller-Waits; selbstverwalteter `coop-root` ohne zyklisches `dependsOn`                           |
| Storage-Schutz                           | `Retain`/`WaitForFirstConsumer`; beide PVCs und Namespace gegen Flux-Prune geschützt                                              |
| Keine Cluster-Push-Automation            | Build-Skript endet nach Manifestupdate/Rendercheck                                                                                |

## Lokale Validierung

```bash
bash -n scripts/bootstrap-production-host.sh scripts/build-and-push-images.sh scripts/restore-postgres.sh
uv run --with pyyaml==6.0.3 -- python3 -m unittest scripts.tests.test_gitops_static scripts.tests.test_operational_scripts
for d in infrastructure/{storage,database,migration,import,apps,ingress,backup} clusters/production; do
  kubectl kustomize "$d" >/dev/null   # oder: kustomize build "$d"
done
git diff --check
git status --short
```

CI baut jedes echte Produktions-Dockerimage genau einmal und startet API/Web non-root mit read-only Root-FS. Render-Verträge prüfen die exakten öffentlichen Traefik-Health-Routen, BasicAuth-/Authorization-Header-Semantik, TLS-Secret, NetworkPolicies und alle drei Probe-Typen. Ein globaler semantischer Render-Gate verwirft jede `image`-Referenz ohne `@sha256` sowie unpromotete Null-Sentinels.

### Abgeschlossenes B2-Deployment-Gate

Das zuvor ausstehende B2-Deployment-Gate wurde am 15. Oktober 2026 auf dem Zielcluster vollständig durchgeführt. Flux-Reconcile und Controller-Readiness, TLS-Kette und nativer Browser-Trust, Auth-/CORS-Grenzen, NetworkPolicies, Workload-Probes, Persistenz, Drift-Reversion, PR-Rollout/Revert sowie Backup und Restore sind protokolliert grün. Die auditierbare Zusammenfassung, exakten Revisionen und verbleibenden Betriebsaufgaben stehen in [`milestone-2-verification.md`](milestone-2-verification.md).
