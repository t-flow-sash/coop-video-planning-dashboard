# Gmail evidence manifests

- `gmail-import-cronjobs.yaml`: permanently suspended read-only FULL pilot template.
- `gmail-recurring-cronjobs.yaml`: Bootstrap und Delta bleiben im Source-Kandidaten suspendiert/unarmed; Aktivierung erfordert einen gesondert reviewten Image-Promotion- und Aktivierungs-PR.
- `gmail-egress-proxy.yaml`: rootless digest-pinned Envoy CONNECT proxy with exact Google OAuth/Gmail authorities.
- `network-policy.yaml`: importer may reach only DNS, PostgreSQL and the proxy; only the proxy has public TCP/443 egress.
- `gmail-oauth.secret.example.yaml`: value-free secret shape only; never add plaintext values.
- `gmail-oauth.secret.sops.yaml` and `gmail-collector-db.secret.sops.yaml`: dedicated immutable, SOPS-encrypted runtime secrets.
- `gmail-full-pilot-admission.yaml`: fail-closed Job/slot admission contracts for the one-shot operator identity.

The workload receives only `gmail-collector-db/COLLECTOR_DATABASE_URL`, whose login must be
`coop_evidence_collector`; it never receives the API/migration `DATABASE_URL`. PostgreSQL ACLs
make `ingestion_source` read-only, limit inserts to the other evidence tables and allow only
narrowly enumerated run/checkpoint updates.

The FULL pilot stays suspended and unarmed. `scripts/gmail_full_pilot.py` remains the fail-closed controller for the consumed one-shot pilot and never unsuspends its CronJob. Bootstrap has already completed and remains suspended/unarmed. The source and checkpoint are active; only delta is scheduled and armed. The owner explicitly approves the existing broad OAuth credential; that approval does not authorize Gmail writes by this collector (GET-only except OAuth refresh). Restore quarantines and suspends delta; removing quarantine after a restore requires separate checkpoint/evidence attestation and a reviewed restart. Domain apply is not available. See `docs/gmail-import-operations.md`.
