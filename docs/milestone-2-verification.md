# Milestone 2 – Produktions- und GitOps-Verifikation

- **Verifikationsdatum:** 15. Oktober 2026 (UTC)
- **Status:** technisch vollständig, zur Nutzerabnahme bereit
- **Nutzerabnahme:** noch nicht erteilt
- **Ziel:** `https://192.168.2.94/` mit Same-Origin-API unter `/api/v1`

## 1. Zweck, Scope und Architektur

Dieses Dokument fasst die abschließende, gegen Git, GitHub CI, den Live-Cluster und die
root-only Evidenz geprüfte M2-Verifikation zusammen. M2 umfasst die gehärtete
Single-Node-k3s-Plattform, pull-basiertes Flux GitOps, immutable Container-Images,
PostgreSQL-Persistenz, Migration, HTTPS/BasicAuth, NetworkPolicies sowie getestete
Backup-/Restore- und Rollback-Pfade.

M1 bleibt fachlich und betrieblich unverändert. Insbesondere ist der bereits vorhandene
M1-PostgreSQL-Listener auf `127.0.0.1:5432` eine absichtlich getrennte Instanz und war
von allen M2-Aktionen ausgeschlossen.

```text
GitHub main (private Repository, read-only Deploy Key)
  └─ Flux GitRepository + selbstverwalteter coop-root
      └─ Storage → PostgreSQL → Migration → API/Web → Ingress → Backup
                                      │
LAN → Traefik 80 (301) / 443 (TLS + BasicAuth) → Web bzw. Same-Origin /api/v1
```

Die Installation ist bewusst für genau einen Node ausgelegt. App, Datenbank und lokale
Registry sind nicht öffentlich exponiert; nur Traefik bedient im LAN HTTP/HTTPS.

## 2. Verifizierte Versionen und unveränderliche Artefakte

| Komponente              | Verifizierter Stand                                                                        |
| ----------------------- | ------------------------------------------------------------------------------------------ |
| k3s / Kubernetes        | `v1.36.5+k3s1` / `v1.36.5`                                                                 |
| Flux CLI/Distribution   | `v2.9.6`                                                                                   |
| source-controller       | `v1.9.6@sha256:6a6693172589f8ff26123a231d5fa6ceb194a6efb4dc647cdf057c959f76a2e3`           |
| kustomize-controller    | `v1.9.6@sha256:2ebeaa341da77d52b6abbbba5efcee0450d47f8b42f0e6f33b08f9020262d606`           |
| helm-controller         | `v1.6.5@sha256:0d52fff5c4d476277b8fcb6beb9041e269adb5db943fe69f5a806ea0c92b1511`           |
| notification-controller | `v1.9.4@sha256:840f318265ee26f0d2c48a158bf7896b22aa4e998e320a18646309f0e40b15da`           |
| Docker Engine           | `29.9.1`                                                                                   |
| SOPS / age              | `3.13.3` / `1.3.2`                                                                         |
| PostgreSQL              | `17-alpine@sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24`        |
| API                     | `git-a4b4d9f030ee@sha256:ebe650d1273c88c6a5223be5d2e1a55a8a5a7207153e14849f78d99e95beca5a` |
| Web                     | `git-a4b4d9f030ee@sha256:e987d6bb0796ea1c53108862adfd147d8660aca32eea6fcbbe717b6ce06ad0ff` |
| Alembic-Head            | `0fe02ef55c8a`                                                                             |

Keine deploybare Image-Referenz verwendet `latest`; Images sind per Digest fixiert.

## 3. Git-, PR- und CI-Linie

Der verifizierte M2-Workloadstand ist
`1201ef407c77dff51a0d95875259ec232582cb6a`. Vor Erstellung dieses Abschlussdokuments
waren lokales `main`, `origin/main`, Flux Source und alle sieben Kustomizations exakt auf
diesem SHA. Die Main-CI
[38673618863](https://github.com/t-flow-sash/coop-video-planning-dashboard/actions/runs/38673618863)
war vollständig erfolgreich; der einzige Job `quality` hatte 40 erfolgreiche Steps und
keine fehlgeschlagenen oder übersprungenen Produkt-Gates.

Die relevante Entwicklungslinie:

| PR                                                                          | Zweck                                                                           | Merge-SHA / Ergebnis                       |
| --------------------------------------------------------------------------- | ------------------------------------------------------------------------------- | ------------------------------------------ |
| [#1](https://github.com/t-flow-sash/coop-video-planning-dashboard/pull/1)   | M2-Produktionsbasis, Container, GitOps, Security, Backup/Restore                | `660346c6bf2bcbaadd79c59fe5e9793768d2c4fa` |
| [#7](https://github.com/t-flow-sash/coop-video-planning-dashboard/pull/7)   | Flux-2.9-kompatible Erstellung des SSH-Secrets ohne entfernten CLI-Parameter    | `ebe4711a8b5857fd4fb48bfdd2e380d5e867875d` |
| [#8](https://github.com/t-flow-sash/coop-video-planning-dashboard/pull/8)   | begrenztes Readiness-Warten gegen den NetworkPolicy-Start-Race des Backup-Pods  | `d0d5202295e33027245c851fe3dfee9909fb6aa4` |
| [#9](https://github.com/t-flow-sash/coop-video-planning-dashboard/pull/9)   | eindeutiger Traefik-`TLSStore/default` und ECDSA-Leaf                           | `0452a48fc297927eb2e1d6e4615462a87b2f6baa` |
| [#10](https://github.com/t-flow-sash/coop-video-planning-dashboard/pull/10) | Rotation auf browserkompatible ECDSA-CA mit korrekter kritischer Key Usage      | `dabe4b96644ae600560de41dabaa000583f6de82` |
| [#11](https://github.com/t-flow-sash/coop-video-planning-dashboard/pull/11) | deklarativer Rollout-Probe über PR → main → Flux                                | `9e0e52d850b31622dadf688a884b4ca508adbc98` |
| [#12](https://github.com/t-flow-sash/coop-video-planning-dashboard/pull/12) | exakter Inhalts-Revert der Rollout-Probe                                        | `9ad6e3e746fe8668015b5467a0ced6ac9fc14d6c` |
| [#13](https://github.com/t-flow-sash/coop-video-planning-dashboard/pull/13) | frischer Generation-/Ready-Nachweis für den selbstverwalteten Root nach Restore | `1201ef407c77dff51a0d95875259ec232582cb6a` |

Die bei Rollout-Prüfungen entdeckten Fehler sind behoben und Regressionstests sind in CI.
Historische rote Zwischenläufe werden nicht verschwiegen: Der erste PR-#8-Stand scheiterte
an einer unvollständigen Testsimulation, danach waren korrigierter Push, PR und Main grün.

## 4. Flux-Bootstrap, Verschlüsselung und aktueller Graph

Der Bootstrap ist zweistufig und fail-closed:

1. Der gepinnte Flux-v2.9.6-Export installiert nur CRDs und vier Controller und wartet auf
   `Established`/`Available`.
2. Erst nach erneuter Prüfung werden der clusterseitige SOPS-age-Schlüssel, das Git-Secret,
   `GitRepository/coop-video-planning` und `Kustomization/coop-root` angelegt.

Flux greift per SSH mit einem repository-begrenzten **read-only Deploy Key** zu. Der private
Schlüssel und der private age-Key liegen ausschließlich außerhalb des Repositorys bzw. als
Cluster-Secret vor. Produktions-Secrets sind mit SOPS/age verschlüsselt; Klartextwerte,
Passwörter und private Schlüssel sind nicht Bestandteil dieses Dokuments oder des Git-Verlaufs.

Aktueller Graph: Source Ready, nicht suspendiert; `coop-root`, `coop-storage`,
`coop-database`, `coop-migration`, `coop-apps`, `coop-ingress` und `coop-backup` sind
7/7 Ready, nicht suspendiert und auf derselben exakten Revision. Die Reihenfolge wird durch
`dependsOn`, `wait`, Retry-Intervalle und Timeouts erzwungen. `coop-root` wartet bewusst
nicht auf sich selbst.

## 5. Workloads, Daten, Migration und Storage

| Ressource              | Finalzustand                                                        |
| ---------------------- | ------------------------------------------------------------------- |
| `Deployment/coop-api`  | 2/2 Ready/Available, RestartCount 0                                 |
| `Deployment/coop-web`  | 2/2 Ready/Available, RestartCount 0                                 |
| `StatefulSet/postgres` | 1/1 Ready, RestartCount 0                                           |
| Migration              | `coop-migration-a4b4d9f030ee`, Complete 1/1                         |
| manueller Seed         | Complete 1/1; `CronJob/coop-seed` weiterhin `suspend=true`          |
| Backup                 | `CronJob/postgres-backup`, `suspend=false`, keine aktive Ausführung |
| Services               | API, Web und PostgreSQL ausschließlich `ClusterIP`                  |

Die Seed-Daten sind im finalen Restore-Zustand: `organization=3`, `contact=3`, `coop=6`,
`coop_contact=6`, `audit_event=7`. Das Prüfobjekt
`00000000-0000-4000-8000-000000000304` (`Maple scheduled campaign`) steht auf
`REVIEW`, Version 2, mit exakt zwei eigenen Audit-Ereignissen.

`postgres-data` und `postgres-backups` sind getrennte 10-Gi-RWO-PVCs. Beide sind Bound,
gegen Flux-Prune geschützt und verwenden `coop-local-path` mit `Retain` und
`WaitForFirstConsumer`:

| Zweck   | PVC UID                                | PV UID                                 |
| ------- | -------------------------------------- | -------------------------------------- |
| Daten   | `535b6163-3947-431f-9955-215059f8b7b1` | `df415f63-6353-4301-9b7d-89e5c3080fa0` |
| Backups | `90864d6b-d182-41e2-a309-5335fdc73711` | `522ed028-982e-46ed-baed-875b90c97394` |

Die lokalen Hostpfade wurden in der root-only Evidenz identitätsgeprüft, werden hier aber
nicht veröffentlicht. `Retain` bewahrt Bytes, ersetzt jedoch kein kontrolliertes
Namespace-Loss-Recovery; das entsprechende fail-closed Runbook ist in `docs/gitops-m2.md`.

## 6. HTTPS, Authentifizierung, CORS und TLS

Aktuelle Live-Matrix:

| Prüfung                                                | Ergebnis                           |
| ------------------------------------------------------ | ---------------------------------- |
| `GET /api/v1/health/live` ohne Auth                    | 200                                |
| `GET /api/v1/health/ready` ohne Auth                   | 200                                |
| geschützte API ohne Auth                               | 401                                |
| geschützte API mit falschem Secret                     | 401                                |
| geschützte API mit Nutzer `sascha` und gültigem Secret | 200                                |
| `/docs` und `/openapi.json` mit gültiger Auth          | 404; API-Dokumentation deaktiviert |
| HTTP `/`                                               | 301 auf HTTPS                      |
| CORS-Origin `https://192.168.2.94`                     | exakt erlaubt                      |
| fremde Origin                                          | kein `Access-Control-Allow-Origin` |

Nur die beiden exakten Health-Routen sind öffentlich. API-BasicAuth behält den
`Authorization`-Header für die zweite Auth-Prüfung der API; die Web-Middleware entfernt ihn
vor dem Web-Upstream. Es gibt keine Wildcard-Origin.

Aktuelle ECDSA-P-256-Kette:

| Zertifikat | Gültigkeit (UTC)                            | SHA-256-Fingerprint                                                                               |
| ---------- | ------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| lokale CA  | 2026-10-15 01:15:54 bis 2036-10-12 01:15:54 | `F9:B4:F4:62:90:50:7D:B9:BF:53:0D:A0:56:88:60:15:6A:21:5D:C4:37:D3:3A:80:96:05:BF:62:7F:58:50:55` |
| Serverleaf | 2026-10-15 01:15:54 bis 2029-01-17 01:15:54 | `9D:6E:AE:45:72:39:52:71:61:5E:B9:A4:CF:63:84:8F:CC:3A:B5:56:05:17:2C:A5:10:59:15:85:A8:90:B7:59` |

Die CA hat kritische `CA:TRUE`, `keyCertSign` und `cRLSign`; das Leaf hat `serverAuth` und
die SANs `IP:192.168.2.94`, `DNS:zionhermes`, `DNS:coop.zionhermes.local`. Kubernetes
Secret, IP-SNI und kein SNI lieferten dasselbe Leaf; strikte OpenSSL-, Python-SSL- und
curl-Prüfungen waren erfolgreich.

Google Chrome `155.0.8059.79` wurde mit einer temporären Benutzer-NSS-Datenbank und dort
importierter öffentlicher CA geprüft. Root und API luden über HTTP/2/TLS 1.3, die UI war
vollständig, und es gab weder Zertifikats-, Console- noch Page-Fehler. Es wurden **keine**
Bypässe wie `--ignore-certificate-errors`, `ignoreHTTPSErrors`, SPKI-Ausnahmen oder
`--allow-insecure-localhost` verwendet. Temporäre Profile wurden entfernt.

Die früheren TLS-Befunde sind **keine aktuellen Einschränkungen**: PR #9 reparierte die
Default-Zertifikatsauswahl und Leaf-Kompatibilität; PR #10 ersetzte die noch nicht
abgenommene CA ohne kritische Issuance-Key-Usage. Die aktuelle Kette ist der reparierte Stand.

## 7. NetworkPolicy-Verifikation

`coop-prod` erzwingt Default-Deny für Ingress und Egress. Die final erneut geprüfte Matrix:

| Quelle/Ziel                                                                                                | Erwartung | Live-Ergebnis                    |
| ---------------------------------------------------------------------------------------------------------- | --------- | -------------------------------- |
| beliebiger Pod in `coop-prod` → CoreDNS UDP/TCP 53                                                         | erlaubt   | DNS-Auflösung erfolgreich        |
| unmarkierter Pod in `coop-prod` → PostgreSQL 5432                                                          | blockiert | Verbindung nicht möglich         |
| realistisch gelabelter Client in `coop-prod` mit `coop.example.com/postgres-client=true` → PostgreSQL 5432 | erlaubt   | `accepting connections`          |
| gleich markierter Pod aus anderem Namespace → PostgreSQL 5432                                              | blockiert | Verbindung nicht möglich         |
| unmarkierter Pod in `coop-prod` → externe HTTPS-Ziele                                                      | blockiert | Verbindung nicht möglich         |
| Traefik in `kube-system` → API 8000/Web 3000                                                               | erlaubt   | beide Service-Probes erfolgreich |

Migration, API, Seed, Backup und Restore tragen den PostgreSQL-Client-Selektor. Der beim
Anlegen neuer Backup-Pods beobachtete kube-router-Policy-Start-Race wird seit PR #8 durch ein
begrenztes `pg_isready`-Warten behandelt; ein dauerhaft nicht erreichbares PostgreSQL endet
weiterhin fail-closed.

Flux-Controller besitzen eine getrennte eingeschränkte Egress-Policy für DNS, Git SSH,
HTTPS, k3s API und interne Controller-Kommunikation.

## 8. Browser-Transition, Persistenz und Drift

Die einmalige Browser-Transition setzte das Prüfobjekt von `SCHEDULED v1` auf `REVIEW v2`.
Das Event `b861b638-c52f-40c6-8d16-cffeccb533de` blieb nach API- und kontrolliertem
PostgreSQL-Neustart identisch erhalten; der PostgreSQL-Pod wechselte dabei von UID
`4b23c13a-89c1-4598-9658-3ee592c22246` auf
`6abd369d-da40-4653-baf7-1655c1e29c34`, während PVC/PV-Identität und Alembic-Head
unverändert blieben. Der aktuelle Zustand ist weiterhin `REVIEW v2` mit zwei Audit-Ereignissen.

Für den Drift-Test wurde `coop-web` am 15. Oktober 2026 um 01:44:07 UTC live von zwei auf
eine Replica abweichend gesetzt. `flux reconcile kustomization coop-apps --with-source`
stellte um 01:44:16 UTC wieder 2/2 her: neun Sekunden bis zur beobachteten Rückführung,
Revision unverändert, ohne Probe-Annotation oder Repository-Änderung.

## 9. Deklarativer PR-Rollout und Revert

Stage B1 änderte ausschließlich eine statische Pod-Template-Annotation und lieferte sie über
GitHub PR, `main` und Flux aus; es gab kein direktes Apply/Patch/Edit am Workload.

**Rollout über PR #11:** Feature-Commit
`5bbae9d97f80e073da4a25ae1ace36819ed8518f`, Merge-SHA
`9e0e52d850b31622dadf688a884b4ca508adbc98`; Deployment-Generation 3→4,
Template-Hash `6bb6688d5d`→`647fbc5d5f`, neue ReplicaSet-/Pod-Identitäten, 2/2 Ready.
Feature-Push-, PR- und Main-CI waren grün; Source, Root, Apps und alle sieben
Kustomizations erreichten exakt den Merge-SHA.

**Revert über PR #12:** exakter Revert des Feature-Commits als
`036c2b2280de60e26d261cc6f9d131d3e0e8a0a3`, Merge-SHA
`9ad6e3e746fe8668015b5467a0ced6ac9fc14d6c`; Generation 4→5, Hash zurück auf
`6bb6688d5d`, aber neue Pod-UIDs, 2/2 Ready. Wieder waren Push-, PR- und Main-CI grün und
der gesamte Flux-Graph erreichte exakt den Revert-SHA. Die Probe ist final abwesend.

## 10. Backup- und Restore-Nachweis

Der Quell-Dump `daily-20261015T022119Z.dump` ist 15.753 Byte groß und hat SHA-256
`e140ec383b5a00c643395289098703a7262816573f7f05ce86168b4fc1caa9c3`. Er wurde vor
und nach Restore in einem non-root Validierungspod mit read-only Backup-PVC als regulär,
nichtleer und durch `pg_restore --list` lesbar geprüft.

### Erster fail-closed Lauf und Reparatur

Der erste Sentinel erzeugte exakt die Transition `REVIEW v2 → IN_PRODUCTION v3`. Der erste
Restore beendete sich mit Exit 10, weil die bis dahin verwendete
`requestedAt`-Bestätigung beim selbstverwalteten `coop-root` nicht zuverlässig fortschritt.
Die Sicherheitsgrenze funktionierte: Source, Root, Kinder und Schedules blieben suspendiert,
API blieb auf 0, Lock und eigener Restore-Pod wurden entfernt. Es wird daher ausdrücklich
**nicht** behauptet, dass es historisch nie fehlgeschlagene Jobs oder Läufe gab.

Safety-Dump 1: `pre-restore-20261015T022245Z.dump`, 15.844 Byte,
SHA-256 `a57c840d7f06832407e7b38a6cf5f1c7e2b8c6ec7d4b79eca4df180251ea53d4`.
PR #13 ersetzte für Root den ungeeigneten Marker durch frische Generation plus Ready.
Anschließend erfolgte die kontrollierte Recovery in der Reihenfolge Source → Datenbank →
Migration → Apps → API 2/2 → Ingress → Backup → Root.

### Finaler erfolgreicher Lauf

Ein zweiter, genau einmal ausgeführter Sentinel erzeugte Event
`c70aa816-7bef-41f8-b3fa-bd966cf549e6` und erneut exakt `REVIEW v2 → IN_PRODUCTION v3`.
Der finale Aufruf
`scripts/restore-postgres.sh daily-20261015T022119Z.dump --yes` lief von 03:09:48 bis
03:10:39 UTC und endete mit **Exit 0**.

Vor destruktiver Arbeit waren Root, Source, Datenbank, Migration, Apps, Ingress und Backup
sowie Backup- und Seed-CronJobs nachweislich suspendiert; API stand auf 0/0. In 28
Monitoring-Samples wurde kein nichtterminaler Writer beobachtet. Safety-Dump 2:
`pre-restore-20261015T030953Z.dump`, 16.004 Byte,
SHA-256 `b0ad03be89d2902c188dd695d2de224b1f6f6363364aa8eeb713f7354c5bfb0c`.

Die Wiederaufnahme erfolgte nachweislich frisch und geordnet: Source (Generation 9),
Datenbank (9), Migration (9), Apps (9, danach API 2/2), Ingress (9), Backup (9), zuletzt
Root (Generation 10, `observedGeneration=10`, Ready ohne `requestedAt`). Danach war der
gesamte Graph wieder auf dem exakten Main-SHA.

Der Restore entfernte beide Sentinel-Ereignisse und stellte das Prüfobjekt auf `REVIEW v2`
mit zwei Audit-Ereignissen sowie die globalen Counts `3/3/6/6/7` zurück. Es blieben kein
Restore-Lock, kein Restore-/Validierungspod und kein aktiver oder fehlgeschlagener Job des
finalen Laufs zurück. Historische Fehlversuche wurden nach Evidenzsicherung archiviert bzw.
bereinigt; normale Job-History-Retention bleibt zulässig.

Backup-Zeitplan: täglich `17 2 * * *`, `Europe/Berlin`, sieben Daily und vier Weekly,
`concurrencyPolicy: Forbid`. Ein Backup auf demselben Node/PVC ist keine zweite Failure
Domain.

## 11. Security, Firewall und Listener

- Namespace-Pod-Security: `restricted` für enforce/audit/warn.
- API, Web, Migration, Seed und Backup laufen non-root, ohne Service-Account-Token,
  `RuntimeDefault`-Seccomp, `allowPrivilegeEscalation=false`, Drop `ALL` und read-only
  Root-FS. PostgreSQL läuft non-root als UID/GID 70, ebenfalls ohne Token, mit Seccomp,
  ohne Privilege Escalation und ohne Capabilities; nur benötigte Mounts sind schreibbar.
- UFW ist aktiv: Default deny incoming und routed; erlaubt sind SSH sowie LAN 80/443 und
  die erforderlichen k3s Pod-/Service-CIDRs. Es gibt keine Internet-/WAN-Freigabe.
- Die OCI-Registry lauscht nur auf `127.0.0.1:5000`.
- Für PostgreSQL gibt es keinen nicht-loopback Host-Listener. Der separate M1-Listener auf
  `127.0.0.1:5432` ist absichtlich vorhanden und unverändert.
- API-, Web- und PostgreSQL-Services sind ClusterIP; LAN-Zugriff erfolgt ausschließlich
  über Traefik.
- API-Dokumentation ist in Produktion deaktiviert; CORS ist nicht wildcard-basiert.
- SOPS-/Git-/TLS-Privatmaterial und Credential-Werte werden nicht dokumentiert.

Andere Host-Dienste außerhalb des M2-Scopes sind keine M2-Expositionsfläche und werden
nicht als Teil dieser Anwendung freigegeben.

## 12. Finalinventar und Evidenz

Final verifiziert: Source + 7/7 Kustomizations Ready; API 2/2, Web 2/2, PostgreSQL 1/1;
keine CrashLoop-/Error-/Probe-Fehler-Pods; Backup-Cron aktiv, Seed suspendiert; alle
fachlichen Services ClusterIP; Health live/ready über die konfigurierte CA jeweils 200;
Repository vor der Dokumentationsänderung sauber und synchron.

Die vollständige Roh- und strukturierte Evidenz bleibt im **root-only evidence store**.
Dazu gehören die Stage-A-Gesamtevidenz, der native Chrome-Trust-Nachweis,
PostgreSQL-Neustart, Flux-Drift, Stage-B1-Rollout/Revert, Stage-B2-Backup/Restore und dessen
Rohlogs. Zugangsdaten liegen unter der generischen, dokumentierten root-only Location
`/root/.config/coop-dashboard/` mit restriktiven Rechten; Secret-Werte sind nicht Teil dieses
Dokuments.

## 13. Restmaßnahmen und Risikoklassifikation

### Vor Nutzung erforderlich, aber kein technischer M2-Blocker

1. Sascha bzw. jeder Client muss `docs/certificates/coop-local-ca.crt` als vertrauenswürdige
   lokale CA installieren und den oben angegebenen Fingerprint über einen unabhängigen Kanal
   vergleichen. TLS-Warnungen dürfen nicht umgangen werden.
2. Der konfigurierte Benutzername ist `sascha`; das Secret bleibt ausschließlich im
   root-only Credential Store und wird getrennt übergeben.

### Akzeptierte betriebliche Restrisiken

- Single Node: keine Hochverfügbarkeit; Node-Ausfall betrifft Anwendung und lokale Backups.
- Backups benötigen fortlaufendes Monitoring, Alarmierung, Restore-Übungen und zusätzlich
  einen verschlüsselten Export in eine andere Failure Domain.
- Serverleaf spätestens vor dem 17. Januar 2029 und CA spätestens vor dem 12. Oktober 2036
  kontrolliert erneuern; Clients bei CA-Rotation neu ausrollen.
- Die Installation ist nur für das private LAN freigegeben. Eine öffentliche Exposition
  erfordert eine neue Security-/DNS-/Zertifikats-/Firewall-Abnahme.

**Offene technische M2-Blocker:** keine.

## 14. Nutzerabnahme

Die technische Verifikation erteilt keine stellvertretende Produktabnahme.

- [ ] Ich, Sascha, habe Ziel-URL, Browserzugriff, CA-Installation und den fachlichen
      Zustand geprüft und nehme Milestone 2 ab.
- [ ] Ich bestätige die akzeptierten Restrisiken und die genannten laufenden
      Betriebsaufgaben.

- **Name:** `____________________`
- **Datum:** `____________________`
- **Kommentar:** `______________________________________________________________`
