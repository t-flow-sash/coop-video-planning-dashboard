# Gmail evidence manifests

- `gmail-import-cronjobs.yaml`: permanently suspended read-only FULL pilot template.
- `gmail-recurring-cronjobs.yaml`: dormant bootstrap and hourly delta collectors; both start suspended and unarmed.
- `gmail-egress-proxy.yaml`: rootless digest-pinned Envoy CONNECT proxy with exact Google OAuth/Gmail authorities.
- `network-policy.yaml`: importer may reach only DNS, PostgreSQL and the proxy; only the proxy has public TCP/443 egress.
- `gmail-oauth.secret.example.yaml`: value-free secret shape only; never add plaintext values.
- `gmail-oauth.secret.sops.yaml` and `gmail-collector-db.secret.sops.yaml`: dedicated immutable, SOPS-encrypted runtime secrets.
- `gmail-full-pilot-admission.yaml`: fail-closed Job/slot admission contracts for the one-shot operator identity.

The workload receives only `gmail-collector-db/COLLECTOR_DATABASE_URL`, whose login must be
`coop_evidence_collector`; it never receives the API/migration `DATABASE_URL`. PostgreSQL ACLs
make `ingestion_source` read-only, limit inserts to the other evidence tables and allow only
narrowly enumerated run/checkpoint updates.

The FULL pilot job stays suspended and unarmed. `scripts/gmail_full_pilot.py` remains the fail-closed controller for the already consumed one-shot pilot path and never unsuspends its CronJob. Bootstrap and delta are separate recurring-runtime contracts: the dormant release keeps both suspended and unarmed; later reviewed activation stages enable the source, run one controlled bootstrap, attest its checkpoint, and only then arm/unsuspend delta. Domain apply is not available. See `docs/gmail-import-operations.md`.
