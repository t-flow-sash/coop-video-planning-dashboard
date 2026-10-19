# Manueller Koop-Import (Stand 19.10.2026)

Der fachlich geprüfte Datenstand ist auf den **19.10.2026** festgelegt. Der automatische Gmail-Delta-Lauf bleibt **pausiert, suspendiert und unarmed**. Dieses Verfahren aktiviert weder Gmail/OAuth noch einen Scheduler und stellt keinen Import-API-Endpunkt bereit.

## Freigegebener CLI-Pfad

Der lokale Operator startet ausschließlich den versionierten, manuell geprüften JSON-Import. Das Manifest darf keine Provider-, Gmail-, Thread-, Message-, History- oder sonstigen externen IDs enthalten. Es wird nicht in das Repository eingecheckt.

```bash
cd apps/api
uv run python -m app.manual_coop_import \
  /sicherer/lokaler/pfad/koop-manifest.json \
  --expected-manifest-hash '<sha256-aus-vier-augen-pruefung>'
```

Erforderlich sind `schema_version: 1`, ein kanonischer SHA-256 für das gesamte Manifest sowie pro Datensatz `record_key`, monotone `record_version` und ein kanonischer `content_hash`. Der CLI-Pfad prüft vor der Übernahme:

- Hash und Schema-Version,
- doppelte fachliche Schlüssel,
- verbotene Provider-/Importfelder auch in verschachtelten Objekten,
- Versionskonflikte und lokale manuelle Abweichungen,
- Agentur-/Kontaktidentitäten.

Die Übernahme läuft unter PostgreSQL-Advisory-Lock in **einer Transaktion**. Fehler verwerfen den vollständigen Batch. Erfolgreiche Änderungen erzeugen `MANUAL_IMPORT_CREATED` beziehungsweise `MANUAL_IMPORT_UPDATED` im Audit; eine unveränderte Wiederholung erzeugt weder Duplikate noch ein zweites Audit-Ereignis.

## Delta bleibt aus

Der CLI-Pfad ist ein kontrollierter manueller Snapshot-Import. Er schaltet den Gmail-Delta-Lauf nicht frei. Die vorhandenen Delta-Workloads bleiben `suspend: true` und `GMAIL_IMPORT_ARMED=false`, bis dafür ein eigener geprüfter Freigabeschritt erfolgt.
