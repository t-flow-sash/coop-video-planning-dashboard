# Gmail-FULL-Evidence: Trust Boundary und Betrieb

## Gelieferter M3-Scope

M3 liefert ausschließlich einen read-only FULL-Evidence-Unterbau. Der einzige operative Befehl ist:

```text
python -m app.ingestion.gmail full
```

Der Collector listet die vollständige Ergebnismenge mit unveränderter effektiver `GMAIL_IMPORT_QUERY`, lädt jede Nachricht über `messages.get(format=metadata)` und persistiert ausschließlich technische Gmail-IDs sowie `historyId`, `internalDate` und `labelIds`. Body, Header, Betreff, Teilnehmer und Anhänge werden weder angefordert noch protokolliert. Source Records, revisionsgenaue Metadaten-Hashes, Observations, ein erfolgreicher FULL-Run, `PENDING_REVIEW`-Threadkandidaten und Checkpoint plus Query-Hash werden in einer Transaktion geschrieben. Ein Fehler verwirft den gesamten Lauf.

Dieser Meilenstein liefert **keinen** Gmail-History-Sync und **keine** fachliche Manifest-Mutation. Delta-Collection, Planning, Dry-run und Apply sind spätere, separat zu entwerfende Meilensteine. Es gibt dafür in M3 weder CLI/API/Service-Symbole noch Jobs oder Kustomize-Ressourcen. FULL mutiert niemals `Coop`, Organisationen, Kontakte oder fachliche Audit-Events.

## OAuth- und Account-Gates

Erforderliche Secret-Keys sind `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN` und `GMAIL_ACCOUNT`. `gmail-oauth.secret.example.yaml` ist wertfrei und wird nicht gerendert. Das produktive `gmail-oauth.secret.sops.yaml` wird erst aus dem root-only Store erzeugt, mit SOPS verschlüsselt und explizit in die Kustomization aufgenommen.

Der Adapter erneuert das Access Token über `oauth2.googleapis.com`, validiert den live zurückgelieferten Scope über Tokeninfo und bindet den Lauf über Tokeninfo (falls dort `email` vorhanden ist) sowie zwingend über `users.getProfile.emailAddress` an `GMAIL_ACCOUNT`. Als Gmail-Scope ist exakt `https://www.googleapis.com/auth/gmail.readonly` erlaubt. Zusätzlich toleriert werden nur die dokumentierten Identity-Scopes `openid`, `email`, `profile`, `https://www.googleapis.com/auth/userinfo.email` und `https://www.googleapis.com/auth/userinfo.profile`; Drive- oder andere API-Scopes sowie breitere Gmail-Rechte werden fail-closed abgelehnt.

## FULL und Checkpoint-Bindung

Die sichere Default-Query ist:

```text
in:anywhere -in:chats after:2026/01/01 {label:Legal to:legal@tsh-social.com from:legal@tsh-social.com}
```

Sie liegt nicht im Secret, sondern als `GMAIL_IMPORT_QUERY` in der CronJob-Konfiguration. Jede `messages.list`-Seite erhält exakt dieselbe effektive Query. FULL liest vor dem paginierten Scan `historyId=H0` und nach dem Scan `H1`. Bei vollständigem Erfolg werden H0 und der kanonische Hash der effektiven Query atomar mit dem erfolgreichen Run gebunden. Diese Bindung ist lediglich eine sichere Grundlage für einen späteren Delta-Entwurf und behauptet keine gelieferte Delta-Funktion.

Der `IngestionSource.enabled` Kill-Switch wird unter einem PostgreSQL-Transaction-Advisory-Lock geprüft. Identische Source-Revisionen und `PENDING_REVIEW`-Kandidaten werden durch Constraints beziehungsweise Hashvergleiche dedupliziert.

## PostgreSQL-Isolation

Der Collector erhält nicht die API-/Migrations-Credentials. `COLLECTOR_DATABASE_URL` muss exakt die dedizierte Login-Rolle `coop_evidence_collector` mit eigenem Passwort und demselben PostgreSQL-Ziel verwenden. Der Migrationsjob provisioniert diese fail-closed Rolle vor Alembic; die nachgelagerte Migration validiert ihre unprivilegierten Attribute und vergibt anschließend ausschließlich `SELECT`/`INSERT` auf den sieben Evidence-Tabellen. `UPDATE` ist nur für die Run-Abschlussfelder und die Checkpoint-Felder erlaubt. `DELETE`, `TRUNCATE`, Schema-`CREATE`, DDL sowie jeder Zugriff auf `organization`, `contact`, `coop`, `coop_contact`, `audit_event` und `alembic_version` bleiben technisch verweigert.

Die Rollen-/ACL-Integrationstests verbinden sich mit den Collector-Credentials selbst und prüfen die Positiv- und Negativmatrix gegen PostgreSQL. Eine Python-Allowlist oder der suspendierte CronJob ist nicht der Sicherheitsanker; die Datenbank-ACL bleibt wirksam, falls Collector-Code später versehentlich eine fachliche Mutation ausführt.

## Pre-Evidence-Artefakt

Ein bereits adjudiziertes Manifest ohne revisionsgenaue Evidence-Hashes bleibt unverändert außerhalb von Git im root-only Store (`0700`, Dateien `0600`). Sein separater Status bleibt `BLOCKED_REQUIRES_EVIDENCE_ENRICHMENT`. Historische Adjudication wird nicht überschrieben. Erst ein späterer Meilenstein darf nach erfolgreicher FULL-Collection ein neues, immutable, revisionsgenaues Artefakt erzeugen und einen getrennt reviewten Apply-Prozess definieren.

## Kontrollierter Egress-Proxy

`gmail-egress-proxy` ist ein dedizierter Envoy Forward Proxy. Das rootless Distroless-Image ist immutable gepinnt. Der Proxy akzeptiert ausschließlich HTTP `CONNECT` mit exakt diesen Authorities:

- `oauth2.googleapis.com:443`
- `gmail.googleapis.com:443`

Es gibt keine Wildcards, IP-Literale, Admin-Schnittstelle oder Access-Logs. Der Pod läuft non-root/read-only ohne Service-Account-Token oder Linux-Capabilities, mit Seccomp und Ressourcenlimits. `HTTPS_PROXY` und `HTTP_PROXY` zeigen auf `http://gmail-egress-proxy:10000`; TLS bleibt Ende-zu-Ende zwischen Adapter und Google.

NetworkPolicies erlauben dem Importer ausschließlich DNS, PostgreSQL und den internen Proxy-Port. Nur der Proxy darf öffentliches TCP/443; private, Loopback, Link-local/Metadata, Dokumentations- und reservierte IPv4-Netze sind ausgeschlossen. Die exakte FQDN-Allowlist wird im Proxy erzwungen.

## Aktivierung und Betrieb

Der einzelne FULL-CronJob bleibt deklarativ `suspend: true`. Vor einer Aktivierung sind zwingend:

1. ein dediziertes produktives Read-only-OAuth-Secret als SOPS-Ressource,
2. ein `COLLECTOR_DATABASE_URL`-Wert für `coop_evidence_collector` mit eigenem Passwort im verschlüsselten `coop-runtime`-Secret,
3. ein auf den finalen Git-Stand gebautes/promoviertes API-Image mit echtem Tag und Digest,
4. erfolgreiche Rollenprovisionierung, Alembic-ACL-Migration und negative fachliche Schreibprobe mit den Collector-Credentials,
5. unabhängige Live-Prüfung der Account-/Scope-Bindung,
6. positive und negative Live-Probes für NetworkPolicy und Proxy aus den realen Pod-Identitäten.

Der Restore-Ablauf suspendiert und read-back-verifiziert den FULL-CronJob unabhängig von deklarativem Soll oder Live-Drift und lässt ihn nach erfolgreichem Restore suspendiert.
