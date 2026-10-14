# Coop- & Video-Planungsdashboard — Meilenstein 1

Lokal ausführbare M1-Basis für die operative Planung von Kooperationen: responsive Arbeitsliste, Suche/Filter, wiederkehrende Kontakte und Agenturen, Detail- und Auditansicht sowie validierte Statuswechsel mit Optimistic Concurrency.

> **Lokale Vorschau:** M1 enthält keine echte Gmail-Verbindung, keine automatische Agentenbearbeitung, keine produktive Authentifizierung und keine echten Kundendaten. `AUTH_MODE=disabled` ist ausschließlich im Entwicklungsmodus zulässig.

## Stack

- Next.js 16 / React 19 / TypeScript
- FastAPI / Pydantic 2 / SQLAlchemy 2 / Alembic
- PostgreSQL 17 über Docker Compose
- pnpm und uv; Playwright, Vitest/RTL/axe, pytest

## Voraussetzungen

Docker mit Compose, Node.js 22 + Corepack und `uv`.

## Erststart

```bash
cp .env.example .env
corepack enable
corepack pnpm install --frozen-lockfile
uv sync --project apps/api --all-groups --frozen
docker compose up -d db
make db-wait
make migrate
make seed
```

Danach in zwei Terminals:

```bash
make api   # http://127.0.0.1:8000, API unter /api/v1
make web   # http://localhost:3000/coops
```

## Qualitätsgates

```bash
make lint
make format-check
make typecheck
make test
make build
corepack pnpm exec playwright install chrome
make e2e                 # API und Web müssen laufen, DB muss migriert/gesät sein
uv run --project apps/api alembic check
```

`make openapi` exportiert den versionierten Vertrag nach `packages/api-client/openapi.json`. Der Frontend-Adapter ist explizit gegen diese API-Modelle typisiert; FastAPI bleibt die alleinige fachliche Schreibgrenze.

## Statusmodell

`NEW → QUALIFYING → NEGOTIATING → CONFIRMED → IN_PRODUCTION → REVIEW → SCHEDULED → PUBLISHED → COMPLETED` mit den in der Architektur dokumentierten Rückschritten und Abbruchpfaden. `COMPLETED`, `DECLINED` und `CANCELLED` sind terminal; alle anderen Status gelten als offen. Terminale Datensätze werden nur mit „Abgeschlossene einblenden“ sichtbar.

## Verzeichnisstruktur

- `apps/api`: API, Migration, Seed und PostgreSQL-Tests
- `apps/web`: Dashboard und Komponenten-/Accessibility-Tests
- `e2e`: echter Browserpfad für Statusänderung und Reload-Persistenz
- `packages/api-client`: eingecheckter OpenAPI-Vertrag
- `docs`: Architektur- und UX-Spezifikation für M1

## Sicherheit und Grenzen

Seed-Adressen verwenden ausschließlich `.invalid`; `.env`, Schlüssel, Tokens, Dumps und lokale Buildartefakte sind ignoriert. Die lokale Basis ist nicht produktionsreif. Gmail/OAuth, Influencer-Agent, Rollen/Rechte und Deployment beginnen ausdrücklich erst nach M1-Abnahme.
