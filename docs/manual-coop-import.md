# Manueller Koop-Import (Stand 19.10.2026)

Der fachlich geprüfte Datenstand ist auf den **19.10.2026** festgelegt. Dieses manuelle Verfahren aktiviert weder Gmail/OAuth noch einen Scheduler und stellt keinen Import-API-Endpunkt bereit. Der Gmail-Delta-Source-Kandidat ist im GitOps-Soll stündlich entsuspendiert und armed; erst Merge, Flux-Abgleich und ein controller-created erfolgreicher Delta-Run belegen die Live-Aktivierung.

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

## Abgrenzung zum separaten Delta-Scheduler

Der CLI-Pfad ist ein kontrollierter manueller Snapshot-Import und schaltet den Gmail-Delta-Lauf selbst nicht frei. Im GitOps-Soll bleiben FULL und Bootstrap suspendiert/unarmed; ausschließlich Delta ist entsuspendiert/armed. Merge, grünes CI, Flux-Abgleich und ein verifizierter controller-created Delta-Run stehen als separate Live-Gates aus. Siehe `docs/gmail-import-operations.md`.
