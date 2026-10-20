# Manueller Koop-Import (Stand 19.10.2026)

Der fachlich geprüfte Datenstand ist auf den **19.10.2026** festgelegt. Dieses manuelle Verfahren aktiviert weder Gmail/OAuth noch einen Scheduler und stellt keinen Import-API-Endpunkt bereit. Der separate Gmail-Delta-Source-Kandidat bleibt im GitOps-Soll suspendiert und unbewaffnet; erst ein geprüfter Promotion-/Aktivierungs-PR darf den produktiven Scheduler einschalten.

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

Der CLI-Pfad ist ein kontrollierter manueller Snapshot-Import und schaltet den Gmail-Delta-Lauf selbst nicht frei. Im Source-Kandidaten bleiben FULL, Bootstrap und Delta suspendiert/unarmed. Aktivierung erfordert einen gesondert reviewten Image-Promotion-/Aktivierungs-PR, grünes CI, Merge, Flux-Abgleich und einen verifizierten controller-created Delta-Run. Siehe `docs/gmail-import-operations.md`.
