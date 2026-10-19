# Gmail-Evidence-Import: Trust Boundary und Betrieb

## Gelieferter Evidence-Scope

Der read-only Collector besitzt drei strikt getrennte Befehle:

```text
python -m app.ingestion.gmail full       # verbrauchter Exactly-once-Pilotpfad
python -m app.ingestion.gmail bootstrap  # einmaliger operativer Baseline-Scan
python -m app.ingestion.gmail delta      # wiederkehrender Gmail-History-Lauf
```

Der Collector listet die vollständige Ergebnismenge mit unveränderter effektiver `GMAIL_IMPORT_QUERY`, lädt jede Nachricht über `messages.get(format=metadata)` und persistiert ausschließlich technische Gmail-IDs sowie `historyId`, `internalDate` und `labelIds`. Body, Header, Betreff, Teilnehmer und Anhänge werden weder angefordert noch protokolliert. Source Records, revisionsgenaue Metadaten-Hashes, Observations, der bereits durable beanspruchte FULL-Run, `PENDING_REVIEW`-Threadkandidaten und Checkpoint plus Query-Hash werden in einer Transaktion geschrieben. Ein Fehler verwirft den gesamten Lauf.

Der operative Bootstrap- und Delta-Pfad existiert zunächst **dormant**. Er mutiert wie FULL niemals `Coop`, Organisationen, Kontakte oder fachliche Audit-Events. Planning, Dry-run und Apply bleiben nicht verfügbar; es gibt weiterhin keine API-, Service- oder Domain-Mutationsfläche.

## OAuth- und Account-Gates

Erforderliche Secret-Keys sind `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN` und `GMAIL_ACCOUNT`. `gmail-oauth.secret.example.yaml` ist wertfrei und wird nicht gerendert. Das produktive `gmail-oauth.secret.sops.yaml` wird erst aus dem root-only Store erzeugt, mit SOPS verschlüsselt und explizit in die Kustomization aufgenommen.

Der Adapter erneuert das Access Token über `oauth2.googleapis.com`, verlangt im live zurückgelieferten Scope mindestens `https://www.googleapis.com/auth/gmail.readonly` und bindet den Lauf über Tokeninfo (falls dort `email` vorhanden ist) sowie zwingend über `users.getProfile.emailAddress` an `GMAIL_ACCOUNT`. Vor Provider- oder Datenbankzugriff muss außerdem der normalisierte Account zum unabhängig freigegebenen `GMAIL_ACCOUNT_SHA256` passen; der Null-Sentinel ist nicht ausführbar. Die Produktentscheidung vom 19.10.2026 erlaubt das vorhandene Credential mit zusätzlichen Gmail-/Drive-/Calendar-Scopes. Diese zusätzlichen Token-Rechte sind keine Aktionsfreigabe: Der Collector besitzt ausschließlich GET-basierte Gmail-Profil-, Nachrichten-, Metadaten- und History-Lesepfade; keine Mail-, Draft-, Label-, Calendar- oder Drive-Mutationsmethode ist implementiert. Senden bleibt ohne explizite Freigabe verboten.

## FULL und Checkpoint-Bindung

Die sichere Default-Query ist:

```text
in:anywhere -in:chats after:2026/01/01 {label:Legal to:legal@tsh-social.com from:legal@tsh-social.com}
```

Sie liegt nicht im Secret, sondern als `GMAIL_IMPORT_QUERY` in der CronJob-Konfiguration. Jede `messages.list`-Seite erhält exakt dieselbe effektive Query. FULL beziehungsweise Bootstrap liest vor dem paginierten Scan `historyId=H0` und nach dem Scan `H1`. Bei vollständigem Erfolg werden H0 und der kanonische Hash der effektiven Query atomar mit dem erfolgreichen Run gebunden.

### Operativer Bootstrap und Delta

`bootstrap` ist vom Pilotpfad getrennt. Er verwendet weder dessen deterministische Run-ID noch den immutable Consumed-Marker und verändert den historischen Claim nicht. Unter demselben PostgreSQL-Advisory-Lock verlangt er eine aktivierte Source und das vollständige Fehlen eines Checkpoints. Der query-beschränkte FULL-Scan und der Evidence-Commit erfolgen in einer Transaktion; ein vorhandener Checkpoint blockiert mit `bootstrap_consumed`.

`delta` verlangt einen vorhandenen Checkpoint, exakt passenden Query-Hash und `snapshot.start_history_id == checkpoint_value`. Gmail `history.list` wird ab diesem Cursor vollständig paginiert; `messages`, `messagesAdded`, `messagesDeleted`, `labelsAdded` und `labelsRemoved` werden dedupliziert. Anschließend wird die unveränderte fachliche Query erneut über `messages.list(q=...)` ausgewertet. Nur die Schnittmenge aus History-Änderungen und aktuell query-passenden IDs wird mit `messages.get(format=metadata)` geladen. Abwesenheit oder Label-Entfernung erzeugt keine Löschung und keine fachliche Mutation.

Erst Evidence, Observations, Review-Kandidaten, erfolgreicher Run und der terminale Gmail-`historyId` werden atomar committet. Provider-, Parse-, Query- oder DB-Fehler lassen den Checkpoint unverändert und erzeugen höchstens einen separaten terminalen `FAILED`-Run mit allowlist-basiertem Fehlercode. Ein von Gmail verworfener Cursor (`404`) wird ausschließlich als `full_required` behandelt. Der Scheduler bleibt in diesem Release suspendiert; ein neuer Baseline-Plan und jede spätere Aktivierung benötigen eine eigene Review- und Freigabestrecke.

Nach erfolgreich attestiertem operativem Bootstrap ist dessen commit-gebundener One-shot-Job aus dem GitOps Desired State entfernt. `gmail-import-full`, `gmail-import-bootstrap` und `gmail-import-delta` bleiben suspendiert und unarmed. Die Delta-Definition behält ihren Stundenplan, startet aber ohne separat reviewte least-privilege Credentials und Aktivierungsfreigabe keinen Job.

Operative Runs sind wiederholbare GET-only Läufe. Sie halten den Advisory Lock während Providerzugriff und Commit und werden erst atomar als `SUCCEEDED` sichtbar; dadurch koexistieren sie mit dem unveränderten historischen `RUNNING`-Pilotclaim, ohne dessen Exactly-once-Vertrag oder den Unique-Index aufzuweichen.

Der `IngestionSource.enabled` Kill-Switch wird unter einem PostgreSQL-Transaction-Advisory-Lock geprüft. Die Basismigration provisioniert `gmail-open-coops-v1` einmalig mit `enabled=false`; die separat reviewte Aktivierungsmigration `d7f4b2a96c31` sperrt und aktiviert ausschließlich diese vorhandene Source. Eine fehlende, mehrdeutige oder bereits aktivierte Source blockiert die Migration. Vor dem ersten erfolgreichen operativen Run ist der Downgrade reversibel; danach blockieren ein erfolgreicher Run oder Checkpoint jede automatische Deaktivierung. Aktivierung und spätere Recovery bleiben getrennte, read-back-verifizierte GitOps-Vorgänge. Identische Source-Revisionen und `PENDING_REVIEW`-Kandidaten werden durch Constraints beziehungsweise Hashvergleiche dedupliziert.

## PostgreSQL-Isolation

Der Collector erhält nicht die API-/Migrations-Credentials. `COLLECTOR_DATABASE_URL` muss exakt die dedizierte Login-Rolle `coop_evidence_collector` mit eigenem Passwort und demselben PostgreSQL-Ziel verwenden. Der Migrationsjob provisioniert diese fail-closed Rolle vor Alembic; die nachgelagerte Migration validiert ihre unprivilegierten Attribute. `ingestion_source` ist für den Collector strikt read-only; `INSERT` gilt nur für die übrigen sechs Evidence-Tabellen. `UPDATE` ist nur für die Run-Abschlussfelder und die Checkpoint-Felder erlaubt. `DELETE`, `TRUNCATE`, Schema-`CREATE`, DDL sowie jeder Zugriff auf `organization`, `contact`, `coop`, `coop_contact`, `audit_event` und `alembic_version` bleiben technisch verweigert.

Die Rollen-/ACL-Integrationstests verbinden sich mit den Collector-Credentials selbst und prüfen die Positiv- und Negativmatrix gegen PostgreSQL. Eine Python-Allowlist oder der suspendierte CronJob ist nicht der Sicherheitsanker; die Datenbank-ACL bleibt wirksam, falls Collector-Code später versehentlich eine fachliche Mutation ausführt.

## Pre-Evidence-Artefakt

Ein bereits adjudiziertes Manifest ohne revisionsgenaue Evidence-Hashes bleibt unverändert außerhalb von Git im root-only Store (`0700`, Dateien `0600`). Sein separater Status bleibt `BLOCKED_REQUIRES_EVIDENCE_ENRICHMENT`. Historische Adjudication wird nicht überschrieben. Erst ein späterer Meilenstein darf nach erfolgreicher FULL-Collection ein neues, immutable, revisionsgenaues Artefakt erzeugen und einen getrennt reviewten Apply-Prozess definieren.

## Kontrollierter Egress-Proxy

`gmail-egress-proxy` ist ein dedizierter Envoy Forward Proxy. Das rootless Distroless-Image ist immutable gepinnt. Der Proxy akzeptiert ausschließlich HTTP `CONNECT` mit exakt diesen Authorities:

- `oauth2.googleapis.com:443`
- `gmail.googleapis.com:443`

Es gibt keine Wildcards, IP-Literale, Admin-Schnittstelle oder Access-Logs. Der Pod läuft non-root/read-only ohne Service-Account-Token oder Linux-Capabilities, mit Seccomp und Ressourcenlimits. `HTTPS_PROXY` und `HTTP_PROXY` zeigen auf `http://gmail-egress-proxy:10000`; TLS bleibt Ende-zu-Ende zwischen Adapter und Google.

NetworkPolicies erlauben dem Importer ausschließlich DNS, PostgreSQL und den internen Proxy-Port. Nur der Proxy darf öffentliches TCP/443; private, Loopback, Link-local/Metadata, Dokumentations- und reservierte IPv4-Netze sind ausgeschlossen. Die exakte FQDN-Allowlist wird im Proxy erzwungen.

## Pilot-Readiness und Aktivierungsgates

Der einzelne FULL-CronJob bleibt deklarativ `suspend: true` und zusätzlich
`GMAIL_IMPORT_ARMED=false`. Die effektive Query ist über
`GMAIL_IMPORT_QUERY_SHA256` gebunden und derselbe freigegebene Hash zusätzlich
im Releasecode gepinnt. Der Collector prüft beide Gates vor
Credential-, Datenbank- oder Providerzugriff. Ein normales
`kubectl create job --from=cronjob/gmail-import-full ...` bleibt deshalb
unbewaffnet und scheitert fail-closed.

### Gate A – Secret und Image vorbereiten

Vor einem dormant Deployment sind zwingend:

1. ein dediziertes produktives Read-only-OAuth-Secret als SOPS-Ressource mit
   exakt `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN` und
   `GMAIL_ACCOUNT`; Secretwerte werden weder in Befehlsausgaben noch in
   Evidenzdateien übernommen,
2. ein nichtleerer `COLLECTOR_DATABASE_URL`-Wert für
   `coop_evidence_collector` mit eigenem Passwort im verschlüsselten
   dedizierten, immutable `gmail-collector-db`-Secret,
3. ein auf den finalen Git-Stand gebautes API-Image mit echtem Git-Tag,
   Registry-Digest und `image-source-revision`. `scripts/build-and-push-images.sh`
   aktualisiert diese drei Werte sowie den Migrationsjob koordiniert in den
   Manifesten, verweigert lokal geänderte Zielmanifeste und stellt bei einem
   Transformations- oder Renderfehler alle Zieldateien aus einem lokalen Backup
   wieder her; das Skript mutiert keinen Cluster,
4. ein eigener Promotion-PR mit grüner CI. Der Import-Render darf danach
   keinen `git-000000000000`-/Null-Digest-Sentinel und keinen
   `GMAIL_ACCOUNT_SHA256`-Null-Sentinel mehr enthalten.

Das Hinzufügen der SOPS-Ressource, das Mergen des Promotion-PRs und der dadurch
ausgelöste dormant Flux-Rollout benötigen jeweils die dafür erteilte
Freigabe. Dieser Readiness-Stand führt keinen dieser Schritte automatisch aus.

### Gate B – read-only Live-Preflight

Nach dem separat freigegebenen dormant Rollout wird der exakte
Produktionskontext immer ausdrücklich angegeben:

```text
./scripts/gmail_full_pilot.py preflight --context <exakter-produktionskontext>
```

Der Preflight verändert nichts. Er verlangt:

- den suspendierten und unbewaffneten CronJob,
- exakt gepinnte Query, Account-Fingerprint, API-Image-Revision,
  Registry-Digest und Build-Commit,
- ausschließlich `gmail-collector-db/COLLECTOR_DATABASE_URL` sowie die vier
  erwarteten `gmail-oauth`-SecretRefs,
- vorhandene, nichtleere Secret-Keynamen sowie UID und `resourceVersion` beider
  Secrets, ohne Secretwerte an den Client zurückzugeben oder auszugeben,
- die exakt erwarteten Flux-Kustomization- und GitRepository-Specs, `Ready=True`,
  aktuelle Generation, dieselbe vollständige Revision für `coop-root`,
  `coop-database`, `coop-migration` und `coop-import` sowie einen gültigen
  SHA-256-Digest des GitRepository-Artefakts,
- einen vollständig ausgerollten Egress-Proxy mit Ready-Service-Endpoint und die
  vollständige, exakt semantisch gepinnte Inventarliste aller sieben
  NetworkPolicies im Namespace; jede zusätzliche Policy blockiert den Lauf,
- keinen vorhandenen Pilotjob mit dem reservierten Namen/Label.
- eine dedizierte `gmail-full-pilot-operator`-Identität mit allen für Attestation
  und UID-sicheres Containment nötigen Rechten, aber ausdrücklich
  **ohne** `delete` auf ConfigMaps. Ein Cluster-Admin-Kontext wird deshalb abgewiesen.
  Für den Lauf ist aus einem kurzlebigen ServiceAccount-Token ein root-only
  Kubeconfig/Context zu erzeugen; Token oder Kubeconfig dürfen nicht ausgegeben
  oder in Git gespeichert werden.

Ein Erfolg liefert ausschließlich Image-Referenz, Build-Commit, Flux-Revision,
Query-Hash und `READY` als sanitisiertes JSON. Die Account-/Scope-Bindung wird anschließend im
bewaffneten Job vor dem ersten Gmail-List-Aufruf erneut live geprüft.

### Gate C – unabhängige Live-Grenzprüfung

Vor dem ersten FULL-Pilotlauf bleiben als separates, protokolliertes
Deployment-Gate verpflichtend:

1. Rollenprovisionierung, Alembic-Migration und ACL-Reconciliation sind auf
   derselben Flux-Revision erfolgreich,
2. eine negative fachliche Schreibprobe mit den Collector-Credentials wird
   von PostgreSQL abgewiesen,
3. aus einer realen Importer-Podidentität funktionieren DNS, PostgreSQL und
   der interne Proxy, direkter öffentlicher HTTPS-Egress dagegen nicht,
4. der Proxy erlaubt CONNECT nur zu `oauth2.googleapis.com:443` und
   `gmail.googleapis.com:443`; andere Authorities und private Ziele werden
   abgewiesen.

Diese Nachweise dürfen keine Connection Strings, Tokens oder Secretwerte
enthalten. Ein fehlender oder nicht eindeutig negativer Test ist kein PASS.

### Gate D – genau ein bewaffneter FULL-Pilot

Nur nach einer gesonderten Aktivierungsfreigabe:

```text
./scripts/gmail_full_pilot.py run \
  --context <exakter-produktionskontext> \
  --timeout 70m \
  --live-attestation <root-only-attestation.json> \
  --live-attestation-sha256 <separat-freigegebener-sha256> \
  --yes
```

`run` akzeptiert nur eine reguläre, vom aktuellen Benutzer besessene Datei mit
Modus `0600`, exakt passendem separat angegebenem SHA-256 und höchstens einer
Stunde Gültigkeit. Die Datei wird über einen einzelnen `O_NOFOLLOW`-Dateideskriptor
geöffnet und gelesen. Der streng versionierte JSON-Vertrag bindet Kontext,
Namespace, Image/Digest, Build-Commit, Flux-/Manifestrevision,
UID/`resourceVersion` beider Secrets und des Admission-Parametervertrags,
Query-/Account-Hash und verlangt positive
Source-/Collector-Nachweise sowie erfolgreiche DB-Schreibverweigerung,
Network-Allow/Deny- und Proxy-Allow/Deny-Proben. Zusätzliche oder fehlende
Felder, ein falscher Digest oder ein abgelaufener Beleg blockieren den Lauf.

Das Tool verwendet exakt den im Preflight geprüften CronJob-Snapshot und
wiederholt unmittelbar vor der Ausführung den vollständigen Preflight. Danach
verbraucht es atomar und dauerhaft den festen Slot über die immutable, mit einem
Retain-Finalizer versehene ConfigMap `gmail-import-full-pilot-consumed`. Die
dedizierte Ausführungsidentität besitzt weder Delete- noch Patch-Rechte darauf;
ein vorhandener oder in seinem Ausgang
ungewisser Marker blockiert jeden weiteren Lauf. Die beiden über UID und
`resourceVersion` attestierten Secrets `gmail-oauth` und `gmail-collector-db`
sind dediziert und immutable und werden vom Job direkt referenziert; die
Operatorrolle darf sie weder erzeugen, ersetzen noch löschen. Ihre Werte erscheinen
weder in argv noch in Logs oder Controller-Ausgaben. Zwei fail-closed
`ValidatingAdmissionPolicy`-Verträge begrenzen die technisch generischen
Kubernetes-Create-Rechte der Operator-SA auf exakt den reservierten suspendierten
Job und den immutable Consumed-Marker; die einzige erlaubte Jobänderung ist
`suspend: true` zu `false` bei ansonsten identischem Spec. Policy-Specs und
Bindings werden live gehasht, der Param-ConfigMap bindet Digest, Buildrevision,
Query und Account-Fingerprint und wird per UID/`resourceVersion` attestiert.
Der Pilot-Pod verwendet ausschließlich die ungebundene, tokenlose
`gmail-full-pilot-runtime`-ServiceAccount; injizierte `envFrom`-, Lifecycle-,
Port-, VolumeDevice- oder privilegierte ServiceAccount-Erweiterungen werden
serverseitig und beim Pod-Readback abgewiesen.
Die Jobkopie wird zunächst
mit `spec.suspend=true` serverseitig angelegt. Erst nach UID-/resourceVersion-
gebundenem Readback und vollständiger Prüfung des persistierten Image-, Command-,
Env-/SecretRef-, Label-, Retry-, Pod- und Security-Vertrags wird sie über einen
optimistisch versionierten Replace aktiviert; eine verzögert oder ungewiss
angelegte Jobkopie kann daher nie bewaffnet anlaufen. Nur in der Jobkopie wird
`GMAIL_IMPORT_ARMED=true` gesetzt. Der feste, reservierte Name
`gmail-import-full-pilot` macht zusätzlich `kubectl create` zur atomaren
Exklusivitätssperre. Es setzt `backoffLimit: 0`, `parallelism: 1` und
`completions: 1`, bindet alle Statusabfragen an die beim Create zurückgegebene
Job-UID und akzeptiert genau einen zugehörigen, restartfreien `Succeeded`-Pod
mit dem gepinnten Registry-Digest. Danach akzeptiert es genau eine sanitisierte
Collector-Erfolgszeile. Podname und Pod-UID werden vor und nach dem Logabruf
gegen dieselbe Job-UID geprüft. Die Löschung verwendet Kubernetes-`DeleteOptions`
mit serverseitiger UID-Precondition und Foreground-Cascading; Erfolg wird erst
bestätigt, wenn Job und alle UID-gebundenen Pods verschwunden sind. `SIGINT` und
`SIGTERM` laufen durch denselben Containment-Pfad. `AlreadyExists` löscht nie
nach Namen. Bei ungewissem Create wird der Job zurückgelesen und nur dann
als korrelierter, aber **nicht bestätigter** UID-Befund protokolliert. Auch bei
passendem zufälligem Ownership-Token wird eine UID aus einem verlorenen
Create-Response niemals adoptiert oder gelöscht; der Lauf stoppt mit
`pilot_job_uncertain` und darf nicht automatisch wiederholt werden. Der
Consumed-Marker bleibt als dauerhafter
Controller-Nachweis bestehen. Zusätzlich beansprucht der erste Collector-Pod
unter Advisory Lock eine deterministische `ingestion_run`-ID und committet diesen
DB-Claim **vor** OAuth-Refresh oder irgendeinem Gmail-Netzwerkzugriff. Ein
Kubernetes-Ersatzpod trifft denselben Primärschlüssel und stoppt vor Netzwerk;
ein Crash nach Claim verbraucht den Slot fail-closed und wird nie automatisch
wiederholt. Anschließend wird der vollständige Preflight
inklusive Flux, Source-Secrets, Proxy, Endpoints, Policies, Pilot-Pods und
CronJob-Fingerprint wiederholt. Die Live-Attestation muss auch nach Job-Wait,
Pod-Attestation und Logabruf noch gültig sein; ein zwischenzeitlicher Ablauf
verhindert jede Erfolgsmeldung und führt in den UID-gebundenen Containment-Pfad.
Scheitert eine Containment-Aktion, wird die
betroffene konkrete Ressource als unsicher ausgegeben. Das Tool
entsuspendiert den CronJob nie und richtet keinen Zeitplan ein.

Der FULL-Lauf selbst schreibt zuerst den dauerhaften DB-Claim, validiert danach
live OAuth-Scope und erwartetes Gmail-Konto, hält für Scan und Persistenz erneut
Advisory Lock und Kill-Switch bis zum atomaren Evidence-Commit und schreibt bei
Fehlern weder Teil-Evidence noch einen Checkpoint. Der Claim bleibt absichtlich
als nicht wiederholbarer Verbrauchsnachweis bestehen.

### Gate E – Abnahme und Rückfall

Für die Abnahme werden ohne Secretwerte mindestens festgehalten:

- Git-/Flux-Revision und exakte Image-Referenz,
- Jobname, UID, Start-/Endzeit und `Complete=True`,
- gepinnter Query-Hash,
- sanitisierter Status `SUCCEEDED` und Anzahl `pending_review`,
- erfolgreicher Run-/Checkpoint-Zusammenhang sowie Evidence-Zählwerte,
- erneuter Nachweis, dass `Coop`, Organisationen, Kontakte und fachliche
  Audit-Events unverändert blieben,
- weiterhin `suspend: true` und `GMAIL_IMPORT_ARMED=false`.

Der Controller liefert die ersten fünf technischen Nachweise einschließlich
Pod-UID, Build- und Flux-Revision. Run-/Checkpoint-Zusammenhang, Evidence-Zählwerte
und die fachliche Unverändertheit werden danach separat mit einer autorisierten
read-only Abfrage attestiert; ohne diesen Nachweis ist der Pilot nicht abgenommen.

Bei Unsicherheit wird zuerst der Pilotjob beendet. Die Jobbeendigung bricht die
laufende Transaktion ab. Anschließend wird der Source-Kill-Switch in einem
autorisierten Admin-Kontext unter demselben Advisory Lock auf `enabled=false`
gesetzt und read-back-verifiziert. Der CronJob bleibt suspendiert; weder ein
automatischer Retry noch eine Entsuspendierung ist zulässig. Ein späterer
Rückbau erfolgt deklarativ über einen eigenen Revert-PR. Nach Migration
`b6a4d9e2f170` darf ein Revert jedoch **nie** auf ein Migrationsimage zurückzeigen,
das diese Revision nicht kennt: Der Rückbau muss das zuletzt erfolgreiche,
vorwärtskompatible Migrationsimage (oder ein neueres Fix-Image mit demselben
Alembic-Graphen) beibehalten. Ein reiner Manifest-Revert auf das Vor-M3-Image
würde an einem unbekannten Datenbank-Head scheitern und ist kein zulässiger
Recovery-Pfad. Ein Alembic-Downgrade des Markers ist nur als separat
freigegebener, getesteter Datenbankeingriff erlaubt.

Der Restore-Ablauf suspendiert und read-back-verifiziert alle drei
Gmail-CronJobs unabhängig von deklarativem Soll oder Live-Drift. Für den
deklarativ dormanten Delta-CronJob setzt er zusätzlich
`kustomize.toolkit.fluxcd.io/reconcile=disabled` und verifiziert die Flux-Quarantäne
vor dem ersten Writer-Check sowie unmittelbar nach dem `coop-import`-Reconcile und
nach allen Reconciles. Zuvor wartet das Skript nach Suspension aller Flux-Grenzen
auf `Reconciling!=True` und verifiziert sämtliche Suspensionen erneut. Delta bleibt
dadurch suspendiert; die Quarantäne darf erst in einem separat reviewten
Post-Restore-Schritt nach DB-, Checkpoint- und Evidence-Attestation entfernt werden.
Das Entfernen der Restore-Quarantäne aktiviert Delta ausdrücklich nicht: Danach müssen
weiterhin `suspend=true` und `GMAIL_IMPORT_ARMED=false` read-back-verifiziert werden.
Eine spätere Aktivierung darf das ausdrücklich freigegebene vorhandene Credential verwenden,
setzt aber einen separaten Review-PR samt statischer und dynamischer GET-only-Live-Attestation
voraus. Jede Abweichung vom mutationsfreien Collectorpfad suspendiert `coop-import` und Delta
erneut. Full und Bootstrap bleiben suspendiert und unbewaffnet.
