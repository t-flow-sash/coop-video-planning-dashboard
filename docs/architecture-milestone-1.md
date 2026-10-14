# Technische Architekturentscheidung – Meilenstein 1

- **Status:** Vorgeschlagen zur Meilenstein-Abnahme
- **Geltungsbereich:** Meilenstein 1 – lauffähige lokale Basis mit Seed-Daten und Statusänderung
- **Nicht Bestandteil:** echter Gmail-Zugriff, automatische Klassifikation, produktive Authentifizierung, Deployment
- **Entscheidungsprinzipien:** pragmatisch, lokal reproduzierbar, auditierbar, später ohne Architekturbruch um Gmail und den Influencer-Agent erweiterbar

## 1. Ziel und Abnahmekriterien

Meilenstein 1 liefert ein vertikales, lokal ausführbares Grundgerüst:

1. Next.js zeigt eine Liste aller offenen beziehungsweise nicht abgeschlossenen Kooperationen aus der API.
2. Eine Kooperation kann geöffnet und ihr Status über eine explizite Statusaktion geändert werden.
3. FastAPI validiert die Status-Transition, speichert Änderung und Audit-Ereignis atomar in PostgreSQL.
4. Seed-Daten enthalten mehrere Kooperationen mit wiederkehrenden Kontakten und Organisationen, sodass diese Zusammenhänge in der UI sichtbar gemacht werden können.
5. Frontend, Backend und Datenbank sind separat testbar; die API besitzt eine versionierte OpenAPI-Spezifikation.
6. Es werden keine echten Gmail-Daten und keine Secrets eingecheckt.

Die technische Abnahme erfolgt erst, wenn Migration, Seed, API-Tests, Frontend-Tests und ein E2E-Test „Status ändern und nach Reload wiedersehen“ erfolgreich laufen.

## 2. Stack-Entscheidung

### Entscheidung

Der vorgeschlagene Stack wird angenommen:

| Schicht | Entscheidung | Begründung |
|---|---|---|
| Frontend | Next.js mit TypeScript, App Router | Gute Basis für ein responsives Dashboard, serverseitiges Laden und spätere Authentifizierung; großes Ökosystem und klare UX-Komponenten-Grenze. |
| Backend | FastAPI, SQLAlchemy 2.x, Alembic, Pydantic 2 | Explizite REST-/OpenAPI-Verträge, gute Validierung, asynchron erweiterbar und passend für spätere Agent-/Ingestion-Aufgaben. |
| Datenbank | PostgreSQL via Docker Compose | Relationen für Kontakte/Organisationen, Transaktionen für Audit-Log und Domänenänderung sowie JSONB für begrenzte variable Metadaten. |
| Paketverwaltung | `pnpm` für Node, `uv` für Python | Schnelle, reproduzierbare Installationen und saubere Lockfiles. |
| API-Vertrag | OpenAPI aus FastAPI; generierter TypeScript-Client beziehungsweise generierte Typen | Kein manuell doppelt gepflegtes Datenmodell zwischen Frontend und Backend. |

### Bewertung und verworfene Alternativen

- **Kein Next.js-Backend als zweite Fachlogik-Schicht:** Next.js darf serverseitig rendern oder als dünne BFF-Grenze für Browser-Authentifizierung dienen, enthält aber keine Coop-Regeln. Status-Transitionen und Auditierung liegen ausschließlich in FastAPI.
- **Kein SQLite als Laufzeit- oder Integrationstest-Ersatz:** PostgreSQL-spezifische Constraints, Transaktionen und JSONB sollen nicht durch abweichendes SQLite-Verhalten kaschiert werden.
- **Kein Microservice-Schnitt in Meilenstein 1:** Ein modularer FastAPI-Monolith ist einfacher zu betreiben. Gmail-Ingestion wird als Port/Adapter vorbereitet, nicht als eigener Dienst implementiert.
- **Keine GraphQL-Schicht:** Die Listen-, Detail- und Transition-Anforderungen sind mit wenigen REST-Endpunkten klarer und günstiger abzubilden.
- **Kein Redux als Standard:** Serverzustand wird über einen kleinen API-Client und gegebenenfalls TanStack Query verwaltet; lokaler UI-Zustand bleibt in React. TanStack Query ist nur einzuführen, wenn Mutation/Invalidierung im konkreten UI davon profitiert.

### Versionspolitik

Während der Implementierung werden konkrete kompatible Versionen in `pnpm-lock.yaml` und `uv.lock` fixiert. Major-Versionen werden nicht ungeprüft automatisch aktualisiert. Die OpenAPI-Spezifikation wird in CI generiert und auf unbeabsichtigte Änderungen geprüft.

## 3. Systemkontext und Verantwortungsgrenzen

```text
Browser
  |
  | Dashboard-Aufrufe (später authentifizierte Session)
  v
Next.js Web --------------------> FastAPI /api/v1 ----------------> PostgreSQL
                                         ^                              |
                                         |                              |
                              Influencer-Agent (später)          Audit Events
                                         ^
                                         |
                              Gmail-Ingestion-Adapter (später)
```

- **Next.js:** Darstellung, Interaktionszustand, barrierearme Bedienung; keine verbindlichen Workflow-Regeln.
- **FastAPI:** einzige fachliche Schreibgrenze, Validierung, Autorisierung, Statusautomat, Optimistic Concurrency, Auditierung.
- **PostgreSQL:** persistente Wahrheit; DB-Constraints ergänzen die Anwendungsvalidierung.
- **Influencer-Agent:** später ein eigener API-Client mit eigener Identität und eingeschränkten Scopes; kein direkter DB-Zugriff.
- **Gmail-Adapter:** später verantwortlich für OAuth/Gmail-Kommunikation und normalisierte, idempotente Übergabe; entscheidet nicht allein über Fachstatus.

## 4. Monorepo-Struktur

```text
coop-video-planning-dashboard/
├── apps/
│   ├── web/
│   │   ├── app/                    # Next.js App Router
│   │   ├── components/             # Präsentation und UI-Komponenten
│   │   ├── features/coops/         # Coop-spezifische UI, Hooks, Mapper
│   │   ├── lib/api/                # generierter Client + dünne Adapter
│   │   └── tests/
│   └── api/
│       ├── app/
│       │   ├── api/v1/             # Router und HTTP-Schemas
│       │   ├── core/               # Konfiguration, Auth-Kontext, Logging
│       │   ├── db/                 # Engine, Sessions, Basisklassen
│       │   ├── models/             # SQLAlchemy-Modelle
│       │   ├── repositories/       # Persistenzzugriffe
│       │   ├── services/           # Use Cases, Transitionen, Auditierung
│       │   ├── integrations/gmail/ # zunächst nur Port/Vertrag, keine Anbindung
│       │   ├── main.py
│       │   └── seed.py
│       ├── alembic/
│       ├── tests/
│       └── pyproject.toml
├── packages/
│   └── api-client/                 # aus OpenAPI generierte TS-Typen/Client
├── infra/
│   └── docker/                     # optional spätere Images/Entrypoints
├── docs/
│   └── architecture-milestone-1.md
├── compose.yaml                    # PostgreSQL; optional später App-Profile
├── .env.example                    # nur nicht geheime Beispielwerte
├── Makefile                        # kurze, dokumentierte Entwicklungsbefehle
├── package.json
├── pnpm-workspace.yaml
└── README.md
```

Die Struktur trennt Fachlogik von Transport und Persistenz, ohne generische „Clean Architecture“-Abstraktionen auf Vorrat zu bauen. Repository-Interfaces werden nur dort eingeführt, wo Tests oder ein echter alternativer Adapter sie benötigen.

## 5. Domänen- und Datenmodell

### Gemeinsame Konventionen

- Primärschlüssel: UUID, serverseitig erzeugt.
- Zeitpunkte: `timestamptz`, Speicherung in UTC, Ausgabe als ISO 8601; Zeitzone nur in der UI lokalisieren.
- Jede veränderbare Kerntabelle besitzt `created_at`, `updated_at` und `version` (Integer für Optimistic Concurrency).
- E-Mail-Adressen werden für Suche/Deduplizierung zusätzlich normalisiert (`trim` und lowercase), aber in Originalform angezeigt.
- Geld wird niemals als Float gespeichert, sondern als `fee_amount_minor bigint` plus ISO-4217-`fee_currency char(3)`; beide sind gemeinsam gesetzt oder beide `NULL`.
- Fachstatus wird in Anwendung und DB per erlaubter Stringmenge abgesichert. Ein `varchar` mit Check Constraint ist für migrationsfreundliche Status-Erweiterungen einem PostgreSQL-Enum vorzuziehen.
- Harte Deletes für Coops sind nicht Teil von M1. Absage/Abbruch sind fachliche Zustände und bleiben auditierbar.

### `organization`

| Feld | Typ | Regel/Zweck |
|---|---|---|
| `id` | UUID | PK |
| `name` | varchar(200) | Pflichtfeld |
| `normalized_name` | varchar(200) | Index für Suche/Deduplizierung; keine automatische Zusammenführung nur anhand des Namens |
| `kind` | varchar | `BRAND`, `AGENCY`, `OTHER` |
| `website_url` | varchar, nullable | normalisierte URL, keine Secrets |
| `notes` | text, nullable | interne Eckdaten |
| `created_at`, `updated_at`, `version` | siehe oben | |

Ein Unique Constraint auf `normalized_name` wäre zu aggressiv, da Namensgleichheit möglich ist. Stattdessen gibt es einen Index und später einen kontrollierten Merge-Prozess.

### `contact`

| Feld | Typ | Regel/Zweck |
|---|---|---|
| `id` | UUID | PK |
| `organization_id` | UUID, nullable FK | aktueller Hauptarbeitgeber/die primäre Agentur |
| `first_name`, `last_name` | varchar, nullable | mindestens Name oder E-Mail muss vorhanden sein |
| `display_name` | varchar(200) | verlässliche Anzeige auch bei unvollständigen Gmail-Daten |
| `email` | varchar(320), nullable | Originalschreibweise |
| `normalized_email` | varchar(320), nullable | case-insensitive eindeutiger Index, sofern gesetzt |
| `phone` | varchar(50), nullable | keine M1-Normalisierung über das Speichern hinaus |
| `notes` | text, nullable | interne Eckdaten |
| `created_at`, `updated_at`, `version` | siehe oben | |

M1 modelliert genau eine primäre Organisation pro Kontakt. Mehrfachzugehörigkeiten und historische Beschäftigungen sind ein offenes Erweiterungsthema, nicht Voraussetzung für die erste funktionsfähige Version.

### `coop`

| Feld | Typ | Regel/Zweck |
|---|---|---|
| `id` | UUID | PK |
| `title` | varchar(240) | kurze sichtbare Bezeichnung |
| `status` | varchar | siehe Statusmodell |
| `brand_organization_id` | UUID, nullable FK | Marke/Kunde |
| `agency_organization_id` | UUID, nullable FK | vermittelnde Agentur; kann gleichfalls fehlen |
| `summary` | text, nullable | vereinbarte Eckdaten/Briefing in M1 |
| `deliverables` | JSONB, nullable | kleine strukturierte Liste, z. B. Video/Short; Schema wird durch Pydantic validiert |
| `next_action` | text, nullable | operativer nächster Schritt |
| `release_at` | timestamptz, nullable | erst setzen, wenn vereinbart; darf bei `SCHEDULED` nicht fehlen |
| `fee_amount_minor` | bigint, nullable | Honorar in kleinster Währungseinheit |
| `fee_currency` | char(3), nullable | ISO 4217 |
| `completed_at` | timestamptz, nullable | bei Übergang nach `COMPLETED`; sonst `NULL` |
| `created_at`, `updated_at`, `version` | siehe oben | |

`deliverables` ist absichtlich der einzige flexible Fachbereich in M1. Häufig gefilterte Werte wie Status, Datum, Honorar und Beziehungen bleiben normale Spalten.

### `coop_contact`

M:N-Zuordnung, damit dieselbe Person über mehrere Kooperationen sichtbar ist.

| Feld | Typ | Regel/Zweck |
|---|---|---|
| `coop_id` | UUID FK | zusammengesetzter PK |
| `contact_id` | UUID FK | zusammengesetzter PK |
| `role` | varchar | `PRIMARY`, `AGENCY`, `BRAND`, `OTHER` |
| `created_at` | timestamptz | |

Pro Coop darf höchstens ein Kontakt die Rolle `PRIMARY` besitzen (partieller Unique Index). Ein Kontakt darf in beliebig vielen Coops vorkommen.

### `audit_event`

Append-only Ereignisprotokoll für jede fachliche Änderung an einer Coop.

| Feld | Typ | Regel/Zweck |
|---|---|---|
| `id` | UUID | PK |
| `coop_id` | UUID FK | indexiert; keine Löschkaskade |
| `action` | varchar | z. B. `COOP_CREATED`, `COOP_UPDATED`, `STATUS_TRANSITIONED`, `CONTACT_LINKED` |
| `actor_type` | varchar | `USER`, `AGENT`, `SYSTEM` |
| `actor_id` | varchar(200), nullable | stabile Subject-ID; bei System optional |
| `source` | varchar | `DASHBOARD`, `INFLUENCER_AGENT`, `GMAIL_INGESTION`, `SEED`, `API` |
| `changes` | JSONB | `{ "field": { "old": ..., "new": ... } }`; nur tatsächlich geänderte Felder |
| `reason` | text, nullable | fachliche Begründung, bei Reopen/Override verpflichtend |
| `request_id` | varchar(100) | Korrelation über Logs/API-Aufruf |
| `source_reference` | varchar(300), nullable | später z. B. Gmail-Message-ID, ohne Mailinhalt |
| `created_at` | timestamptz | unveränderbarer Ereigniszeitpunkt |

Mutation und Audit Event werden in **derselben Datenbanktransaktion** geschrieben. Ein Service-Aufruf gilt als fehlgeschlagen, wenn eines von beiden nicht gespeichert werden kann. Für `audit_event` existieren keine Update-/Delete-Endpunkte. In einer späteren Produktionsumgebung erhält die App-DB-Rolle nur `INSERT`/`SELECT` auf diese Tabelle; M1 erzwingt Append-only zusätzlich auf Anwendungsebene. Das Audit-Log ist kein Event Store: Der aktuelle Coop-Zustand bleibt in `coop`.

### Indizes für M1

- `coop(status, release_at)` für offene Liste und Terminansicht.
- `coop(brand_organization_id)` und `coop(agency_organization_id)`.
- `contact(normalized_email)` partiell unique, wenn nicht `NULL`.
- `organization(normalized_name)` nicht eindeutig.
- `coop_contact(contact_id)` für wiederkehrende Kontakte.
- `audit_event(coop_id, created_at desc)`.

## 6. Statusmodell und Transitionen

### Zustände

| Status | Bedeutung | Offen im Standard-Dashboard? |
|---|---|---|
| `NEW` | neu erfasst, noch nicht qualifiziert | ja |
| `QUALIFYING` | Relevanz/Passung wird geprüft | ja |
| `NEGOTIATING` | Konditionen oder Umfang in Abstimmung | ja |
| `CONFIRMED` | grundsätzlich vereinbart | ja |
| `IN_PRODUCTION` | Inhalt in Umsetzung | ja |
| `REVIEW` | Freigabe/Korrekturen laufen | ja |
| `SCHEDULED` | Veröffentlichung fest terminiert | ja |
| `PUBLISHED` | veröffentlicht, Abschluss noch offen | ja |
| `COMPLETED` | fachlich und administrativ abgeschlossen | nein |
| `DECLINED` | Angebot abgelehnt/nicht zustande gekommen | nein |
| `CANCELLED` | bereits laufende Coop abgebrochen | nein |

„Offen“ wird zentral als `status NOT IN (COMPLETED, DECLINED, CANCELLED)` definiert, nicht im Frontend dupliziert.

### Erlaubte reguläre Transitionen

```text
NEW           -> QUALIFYING, NEGOTIATING, DECLINED
QUALIFYING    -> NEGOTIATING, CONFIRMED, DECLINED
NEGOTIATING   -> QUALIFYING, CONFIRMED, DECLINED, CANCELLED
CONFIRMED     -> NEGOTIATING, IN_PRODUCTION, CANCELLED
IN_PRODUCTION -> CONFIRMED, REVIEW, SCHEDULED, CANCELLED
REVIEW        -> IN_PRODUCTION, SCHEDULED, CANCELLED
SCHEDULED     -> REVIEW, PUBLISHED, CANCELLED
PUBLISHED     -> REVIEW, COMPLETED
COMPLETED     -> (keine reguläre Transition)
DECLINED      -> (keine reguläre Transition)
CANCELLED     -> (keine reguläre Transition)
```

Regeln:

- `SCHEDULED` erfordert `release_at`.
- `COMPLETED` setzt `completed_at`; beim autorisierten Wiederöffnen wird es geleert.
- Rückschritte innerhalb des aktiven Workflows sind erlaubt, weil reale Kooperationen Nachverhandlungen und Korrekturen haben.
- Terminale Zustände können nur über eine separate Aktion `reopen` mit Berechtigung `coop:reopen` und Pflichtbegründung verlassen werden. Das Ziel ist dabei `QUALIFYING` für `DECLINED`, `CONFIRMED` für `CANCELLED` und `PUBLISHED` für `COMPLETED`.
- Der Influencer-Agent benutzt dieselben Transition-Services und darf keine Regeln umgehen.
- Ein Statuswechsel wird nicht als generisches Feld-Patch angeboten; dadurch bleiben Validierung und Audit-Semantik eindeutig.

## 7. REST-API (`/api/v1`)

Alle Antworten sind JSON. Fehler verwenden `application/problem+json` nach RFC 9457 mit stabilen Fehlercodes. Listen sind ab dem ersten Meilenstein paginiert (`limit`, `cursor`); keine unbeschränkten Tabellenantworten.

### System

| Methode | Pfad | Zweck |
|---|---|---|
| `GET` | `/health/live` | Prozess lebt; keine DB-Abhängigkeit |
| `GET` | `/health/ready` | DB erreichbar und Schema/Migration verwendbar |
| `GET` | `/openapi.json` | Vertrag, von FastAPI erzeugt |

### Coops

| Methode | Pfad | Zweck |
|---|---|---|
| `GET` | `/coops` | Liste; Filter `scope=open|all`, `status`, `contact_id`, `organization_id`, `release_from`, `release_to`, `query`; Standard `scope=open` |
| `POST` | `/coops` | Coop anlegen; M1 primär für Tests/Seed/Admin, mit Audit Event |
| `GET` | `/coops/{coop_id}` | Detail inklusive Kontakte und Organisationen |
| `PATCH` | `/coops/{coop_id}` | editierbare Eckdaten ändern, **nicht** `status`; erwartet `version` |
| `POST` | `/coops/{coop_id}/transitions` | `{target_status, expected_version, reason?}`; atomarer Statuswechsel |
| `POST` | `/coops/{coop_id}/reopen` | terminalen Zustand mit Pflichtgrund verlassen; eigener Scope |
| `GET` | `/coops/{coop_id}/audit-events` | paginierte Änderungshistorie |

Erfolgreiche Mutationen liefern den neuen Ressourcenstand und `version`. Bei veralteter `expected_version` antwortet die API mit `409 CONFLICT` und Fehlercode `stale_version`; bei fachlich unerlaubter Transition mit `409` und `invalid_status_transition`. Validierungsfehler sind `422`, fehlende Ressourcen `404`.

### Kontakte und Organisationen

| Methode | Pfad | Zweck |
|---|---|---|
| `GET` | `/contacts` | Suche/Listenansicht, optional `organization_id` |
| `POST` | `/contacts` | Kontakt anlegen |
| `GET` | `/contacts/{contact_id}` | Kontakt inklusive zugehöriger Coop-Zusammenfassung |
| `PATCH` | `/contacts/{contact_id}` | Stammdaten mit Version aktualisieren |
| `GET` | `/organizations` | Suche/Listenansicht |
| `POST` | `/organizations` | Organisation anlegen |
| `GET` | `/organizations/{organization_id}` | Organisation inklusive Kontakte und Coop-Zusammenfassung |
| `PATCH` | `/organizations/{organization_id}` | Stammdaten mit Version aktualisieren |
| `PUT` | `/coops/{coop_id}/contacts/{contact_id}` | idempotent verknüpfen/Rolle setzen; Audit Event |
| `DELETE` | `/coops/{coop_id}/contacts/{contact_id}` | Verknüpfung, nicht Kontakt, entfernen; Audit Event |

Seed-Daten werden per CLI geladen, nicht über einen öffentlich erreichbaren `/seed`-Endpunkt.

### Vertrag und Serialisierung

- API-Schemas und SQLAlchemy-Modelle bleiben getrennt; keine ORM-Objekte direkt serialisieren.
- `packages/api-client` wird aus einer kontrolliert erzeugten OpenAPI-Datei generiert. Generierte Dateien werden nicht manuell editiert.
- Cursors sind opak. Sortierung der offenen Liste ist stabil: `release_at ASC NULLS LAST`, danach `updated_at DESC`, `id`.
- `deliverables` erhält ein versioniertes Pydantic-Schema statt beliebiger JSON-Daten.

## 8. Konsistenz, Nebenläufigkeit und Auditierung

1. Jede Mutation beginnt eine DB-Transaktion.
2. Ressource wird anhand `id` und `expected_version` aktualisiert; `version` wird inkrementiert.
3. Der Service berechnet serverseitig die Differenz. Vom Client übermittelte Actor-, Audit- oder `old`-Werte werden ignoriert.
4. Der Actor-Kontext stammt ausschließlich aus verifizierter Authentifizierung; in lokalem M1-Dev-Modus aus einer fest konfigurierten Dev-Identität.
5. Das Audit Event wird in derselben Transaktion eingefügt.
6. Commit erfolgt nur, wenn Mutation und Audit Event erfolgreich sind.
7. API- und strukturierte Anwendungslogs tragen dieselbe `request_id`, enthalten aber keine Mailtexte, OAuth-Tokens oder unnötige personenbezogene Daten.

Diese Strategie verhindert stille „last write wins“-Verluste, wenn Dashboard und Agent später dieselbe Coop nahezu gleichzeitig ändern.

## 9. Authentifizierungs- und Sicherheitsgrenzen

### Meilenstein 1

- Es gibt einen expliziten `AUTH_MODE=disabled` ausschließlich für `APP_ENV=development` und lokale Bindung an `127.0.0.1`.
- Die Anwendung muss den Start verweigern, wenn Auth deaktiviert ist und `APP_ENV` nicht `development` ist.
- FastAPI-CORS erlaubt nur die konkret konfigurierte lokale Web-Origin, standardmäßig `http://localhost:3000`; kein `*` mit Credentials.
- PostgreSQL wird nur auf localhost veröffentlicht oder bleibt ausschließlich im Compose-Netz. Das Passwort stammt aus `.env`, nicht aus Git.
- `.env`, OAuth-Dateien, Token-Caches und lokale DB-Dumps stehen in `.gitignore`; `.env.example` enthält nur Platzhalter beziehungsweise ungefährliche lokale Defaults.
- Seed-Daten sind synthetisch und enthalten keine echten Kundendaten.
- API-Dokumentation darf lokal offen sein; in produktiven Umgebungen wird sie geschützt oder abgeschaltet.

### Zielgrenze für spätere Umgebungen

- Menschliche Nutzer: OIDC/OAuth2 mit serverseitiger Session im Web. FastAPI erhält einen verifizierbaren Access Token oder signierte interne Identität; untrusted `X-Actor-*`-Header aus dem Browser sind verboten.
- Autorisierung bleibt im Backend. Vorgesehene Scopes: `coop:read`, `coop:write`, `coop:transition`, `coop:reopen`, `audit:read`, `ingestion:write`.
- Influencer-Agent und Gmail-Worker erhalten getrennte Service-Identitäten, kurzlebige Tokens und minimale Scopes. Kein gemeinsamer „Admin API Key“.
- Browser hat keinen direkten DB-Zugriff und erhält niemals Agent-, Gmail- oder DB-Secrets.
- CSRF-Schutz ist erforderlich, falls cookiebasierte Browser-Sessions Mutationen anstoßen; zusätzlich sichere Cookies (`HttpOnly`, `Secure`, angemessenes `SameSite`) und eine restriktive Content Security Policy.
- Rate Limits, Secret Rotation, TLS, Backups, Lösch-/Aufbewahrungskonzept und verschlüsselte Tokenablage sind Deployment-Aufgaben vor produktiver Nutzung.

M1 ist damit **nur lokal vertrauenswürdig und nicht produktionsreif**. Diese Grenze muss in README und UI-Umgebungshinweis sichtbar sein.

## 10. Gmail-Ingestion-Schnittstelle für spätere Meilensteine

M1 implementiert keinen Gmail-Zugriff, keine OAuth-Einrichtung und keine automatische Erstellung. Es legt lediglich den Integrationsvertrag und die Modulgrenze fest.

### Adapter-Port

Ein späterer Gmail-Adapter normalisiert Providerdaten in einen transportneutralen `IngestedMessage`-Vertrag:

```json
{
  "provider": "gmail",
  "account_id": "stable-internal-account-id",
  "external_message_id": "gmail-message-id",
  "external_thread_id": "gmail-thread-id",
  "sent_at": "2026-10-14T08:00:00Z",
  "from": [{"name": "Example", "email": "person@example.invalid"}],
  "to": [{"name": "Creator", "email": "creator@example.invalid"}],
  "cc": [],
  "subject": "Kooperationsanfrage",
  "text_body": "normalized plain text",
  "labels": ["INBOX"],
  "attachments": [{"external_id": "id", "filename": "brief.pdf", "mime_type": "application/pdf", "size": 1234}],
  "content_sha256": "..."
}
```

Der Vertrag wird als Pydantic-Modell unter `apps/api/app/integrations/gmail/contracts.py` definiert. HTML wird vor Übergabe sicher in Text überführt; Remote-Inhalte und Anhänge werden nicht ungeprüft ausgeführt oder gerendert.

### Interner Endpunkt

Für einen später separat laufenden Adapter ist vorgesehen:

- `POST /api/v1/internal/ingestion/gmail/messages`
- Scope: ausschließlich `ingestion:write`
- Idempotenzschlüssel: `(provider, account_id, external_message_id)` mit Unique Constraint
- Antwort: `202 Accepted` bei erstmaliger Annahme, `200 OK` mit bestehender Ingestion-ID bei Wiederholung
- Größenlimits für Body und Anhänge; Anhänge werden nicht als Base64 in der Kerndatenbank gespeichert

Bei anfänglich gleichem Prozess darf derselbe Application Service direkt aufgerufen werden; die HTTP-Grenze ist dann nicht künstlich intern zu benutzen.

### Verarbeitung und fachliche Sicherheit

1. Normalisierte Nachrichten werden mit minimal nötigen Metadaten und sicherem Rohdaten-Verweis erfasst.
2. Absender/Empfänger werden über `normalized_email` vorhandenen Kontakten zugeordnet; unsichere Treffer werden nicht automatisch zusammengeführt.
3. Extraktion/Klassifikation erzeugt zunächst einen `coop_candidate` mit Konfidenz, Feldvorschlägen und Quellenreferenzen.
4. Niedrige Konfidenz, widersprüchliche Werte oder neue Organisationen gehen in eine Review Queue.
5. Erst ein bestätigter Candidate erstellt/aktualisiert eine Coop über denselben fachlichen Service wie das Dashboard und erzeugt Audit Events mit `source=GMAIL_INGESTION` beziehungsweise `INFLUENCER_AGENT`.
6. Re-Processing bleibt idempotent. Eine spätere Mail darf bestätigte menschliche Werte nicht still überschreiben; Konflikte werden als Vorschlag sichtbar.

Damit kann perspektivisch jede relevante Gmail-Coop einfließen, ohne E-Mail-Text direkt zum uneingeschränkten Schreibbefehl zu machen. Gmail-Inhalte gelten sicherheitstechnisch als untrusted input; insbesondere sind Prompt Injection, HTML/Attachment-Malware und Datenexfiltration beim späteren Agent-Einsatz zu berücksichtigen.

## 11. Seed-Daten

Der idempotente CLI-Seed erzeugt mindestens:

- zwei Brands und eine Agentur,
- drei Kontakte, davon mindestens ein Agenturkontakt in mehreren Coops,
- mindestens sechs Coops über aktive und terminale Zustände,
- mindestens eine Coop mit Release Date und Honorar,
- mindestens eine ohne vereinbartes Datum/Honorar,
- Audit Events mit `actor_type=SYSTEM`, `source=SEED`.

Idempotenz erfolgt über feste UUIDs oder stabile Seed-Schlüssel. Der Seed überschreibt keine bereits vom Nutzer geänderten Datensätze; für einen vollständigen Reset gibt es einen ausdrücklich destruktiven lokalen DB-Reset-Befehl.

## 12. Teststrategie

### Backend

- **Unit-Tests (`pytest`):** Transition-Matrix, Pflichtregeln für `release_at`, Geldvalidierung, Normalisierung, Diff-Erzeugung und Berechtigungsentscheidungen.
- **Service-/Integrationstests gegen PostgreSQL:** Migration auf leerer DB, CRUD, Filter/Sortierung, atomare Auditierung, Rollback bei Audit-Fehler, Unique/Check Constraints und Optimistic-Concurrency-Konflikt. Tests verwenden eine isolierte PostgreSQL-Datenbank/Container, nicht SQLite.
- **API-Tests:** FastAPI-Testclient beziehungsweise `httpx`; Statuscodes, RFC-9457-Fehler, Pagination, OpenAPI-Schema und Actor-Ermittlung.
- **Migrationstest:** `alembic upgrade head`, optional Upgrade von letzter veröffentlichter Revision und `alembic check` auf Model-/Schema-Drift.

### Frontend

- **Unit-/Komponententests:** Vitest + React Testing Library; Statusdarstellung, wiederkehrende Kontakt-/Organisationshinweise, leere/Fehler-/Ladezustände und erlaubte Statusoptionen aus API-Daten.
- **Keine duplizierte Transition-Logik als Sicherheitsentscheidung:** Die UI darf Optionen nutzerfreundlich einschränken, die API bleibt autoritativ.
- **Accessibility:** automatisierte `axe`-Prüfung für zentrale Ansichten plus Tastaturtest des Statusdialogs.
- **Mocking:** MSW auf HTTP-Grenze mit Responses, die zum generierten Vertrag passen.

### Ende-zu-Ende und Vertrag

- **Playwright:** Dashboard lädt Seed-Daten; wiederkehrender Kontakt ist sichtbar; Statuswechsel wird bestätigt; Reload zeigt den neuen Status; Audit-Historie enthält Quelle und Änderung; stale-version-Konflikt wird verständlich angezeigt.
- **Contract Gate:** OpenAPI exportieren, TypeScript-Client neu generieren und CI fehlschlagen lassen, wenn ein nicht eingecheckter Diff entsteht.
- **CI-Reihenfolge:** Lint/Format/Typecheck → Unit-Tests → PostgreSQL-Integration/Migration → Web Build → Playwright.

Ziel für M1 ist nicht eine abstrakte Prozentzahl, sondern vollständige Abdeckung aller Statuskanten, der fachlichen Constraints und des Audit-/Concurrency-Pfads.

## 13. Vorgesehene lokale Startbefehle

Die folgenden Befehle sind der verbindliche Ziel-Workflow für die Umsetzung von M1; dieses Architektur-Dokument selbst implementiert sie noch nicht.

### Erstinstallation

```bash
cd /root/coop-video-planning-dashboard
cp .env.example .env
corepack enable
pnpm install --frozen-lockfile
uv sync --project apps/api --all-groups --frozen

docker compose up -d db
uv run --project apps/api alembic upgrade head
uv run --project apps/api python -m app.seed
```

Bei der allerersten Installation existieren Lockfiles erst nach bewusstem Dependency-Pinning; danach sind die `--frozen`-Varianten Standard.

### Entwicklung in zwei Terminals

```bash
# Terminal 1 – API
cd /root/coop-video-planning-dashboard
uv run --project apps/api uvicorn app.main:app \
  --reload --host 127.0.0.1 --port 8000
```

```bash
# Terminal 2 – Web
cd /root/coop-video-planning-dashboard
pnpm --filter web dev
```

Erwartete lokale URLs:

- Dashboard: `http://localhost:3000`
- API: `http://127.0.0.1:8000/api/v1`
- API-Dokumentation im Dev-Modus: `http://127.0.0.1:8000/docs`

### Tests und Qualitätsprüfungen

```bash
uv run --project apps/api pytest
uv run --project apps/api alembic check
pnpm --filter web lint
pnpm --filter web typecheck
pnpm --filter web test
pnpm --filter web build
pnpm exec playwright test
```

Ein Root-`Makefile` soll diese Abläufe als `make setup`, `make db-up`, `make migrate`, `make seed`, `make dev`, `make test` und `make down` bündeln, aber die darunterliegenden Befehle bleiben dokumentiert und direkt ausführbar.

## 14. Umsetzungsreihenfolge für Meilenstein 1

1. Monorepo, Lockfiles, Env-Beispiel und Compose-PostgreSQL anlegen.
2. Backend-Konfiguration, DB-Session und erste Alembic-Migration erstellen.
3. Modelle/Constraints und Seed-CLI mit wiederkehrenden Beziehungen implementieren.
4. Transition-Service, Optimistic Concurrency und atomisches Audit-Log testgetrieben umsetzen.
5. REST-Endpunkte und OpenAPI-Client bereitstellen.
6. UX-Ansichten gegen den generierten Client implementieren: offene Liste, Detail, Statusdialog, Fehlerzustände.
7. Integration-/E2E-Tests und lokale Betriebsdokumentation abschließen.
8. Meilenstein-Demo und explizite Abnahme; erst danach Gmail-/Agent-Arbeit beginnen.

UX- und DEV-Arbeit können nach Festlegung des API-Vertrags parallel laufen. Der UX-Agent arbeitet mit Mockdaten aus dem Vertrag; der DEV-Agent besitzt Domänenregeln, Migrationen und Integration. Die Abnahme bleibt ein gemeinsamer vertikaler Test, nicht zwei getrennte Teilabnahmen.

## 15. Offene Risiken und spätere Entscheidungen

| Risiko/offene Frage | Auswirkung | Umgang |
|---|---|---|
| Auth-/Hosting-Anbieter noch unbekannt | Session-, Token- und Netzwerkdetails sind nicht final | Vor erstem nichtlokalem Deployment eigene ADR erstellen; M1 bleibt strikt lokal. |
| Gmail-Volumen, Kontenumfang und historische Importtiefe unbekannt | Polling vs. Push, Queue und Quotas offen | In Folgemeilenstein messen; Adaptervertrag/idempotente Grenze bleibt unabhängig davon gültig. |
| Automatische Erkennung „jede Coop aus Gmail“ ist semantisch unscharf | False Positives/Negatives | Candidate/Review-Workflow und messbare Recall-/Precision-Stichprobe statt blindem Auto-Write. |
| Kontakt kann mehrere Organisationen oder Arbeitgeberhistorie haben | M1-Zuordnung kann zu grob sein | Zunächst Hauptorganisation; bei realem Bedarf `contact_organization` mit Gültigkeit ergänzen. |
| Honorar kann Raten, Sachleistungen, Netto/Brutto oder mehrere Währungen umfassen | Zwei Geldfelder reichen langfristig nicht | M1 bildet vereinbartes Gesamthonorar ab; Zahlungsmodell separat nach realen Fällen entwerfen. |
| „Release Date“ kann mehrere Deliverables betreffen | Ein Datum pro Coop kann unzureichend werden | M1 nutzt das primäre vereinbarte Datum; später Deliverable-Entität nur bei echtem Bedarf. |
| JSONB-Deliverables kann unkontrolliert wachsen | Schlechte Filter-/Migrationsfähigkeit | Striktes versioniertes Schema; häufig abgefragte Felder später normalisieren. |
| Gleichzeitige Änderungen durch UI und Agent | Überschreiben/Inkonsistenz | `version`, 409-Konflikte und atomisches Audit sind bereits in M1 Pflicht. |
| Audit-Log enthält personenbezogene oder sensible Altwerte | Datenschutz/Retention | Nur notwendige Felder loggen; keine Mailtexte/Tokens; vor Produktion Retention und Auskunft/Löschung klären. |
| Append-only nur auf Anwendungsebene in M1 | DB-Admin könnte Historie verändern | Später separate DB-Rechte, Backups und gegebenenfalls manipulationssichtbare Verkettung evaluieren. |
| PostgreSQL/Compose ist eine lokale Voraussetzung | Onboarding kann aufwendiger sein | Healthcheck, Make-Targets und klare Fehlerhinweise; kein semantisch abweichender SQLite-Fallback. |
| API-Client-Generierung kann Drift erzeugen | Frontend und Backend passen nicht zusammen | OpenAPI-Diff und Regeneration als CI-Gate. |

## 16. Explizite Nicht-Entscheidungen für M1

- konkreter Cloud-/Hosting-Anbieter,
- konkreter OIDC-Anbieter,
- Gmail Polling vs. Gmail Watch/Pub/Sub,
- LLM-Anbieter und Extraktionsprompt,
- Attachment-Speicher,
- Rechnungs-/Zahlungsmodell,
- manipulationssicheres Compliance-Audit über DB-Rechte hinaus,
- Mehrmandantenfähigkeit.

Diese Punkte werden erst entschieden, wenn Anforderungen oder Betriebsumgebung feststehen. Die gewählten Grenzen – versionierte REST-API, zentrale Services, Actor-Kontext, Idempotenz und Audit Events – halten diese Optionen offen, ohne M1 mit Vorabkomplexität zu belasten.

## 17. Entscheidungssumme

Meilenstein 1 wird als modularer Monolith in einem Monorepo gebaut: Next.js/TypeScript für das Dashboard, FastAPI/SQLAlchemy/Alembic als alleinige fachliche Schreibgrenze und PostgreSQL via Docker Compose. Statuswechsel sind explizite Aktionen mit zentraler Transition-Matrix, Optimistic Concurrency und transaktionalem Audit Event. Wiederkehrende Kontakte und Agenturen werden relational modelliert. Gmail und Influencer-Agent greifen später ausschließlich über normalisierte, authentifizierte und idempotente Schnittstellen auf dieselben Fachservices zu. Bis produktive Authentifizierung und Betriebsmaßnahmen abgenommen sind, ist das System ausschließlich für lokale Entwicklung freigegeben.
