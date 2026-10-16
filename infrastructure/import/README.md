# Gmail FULL evidence manifests

- `gmail-import-cronjobs.yaml`: suspended, read-only FULL evidence collector.
- `gmail-egress-proxy.yaml`: rootless digest-pinned Envoy CONNECT proxy with exact Google OAuth/Gmail authorities.
- `network-policy.yaml`: importer may reach only DNS, PostgreSQL and the proxy; only the proxy has public TCP/443 egress.
- `gmail-oauth.secret.example.yaml`: value-free secret shape only; never add plaintext values.
- `gmail-oauth.secret.sops.yaml` and `gmail-collector-db.secret.sops.yaml`: dedicated immutable, SOPS-encrypted runtime secrets.
- `gmail-full-pilot-admission.yaml`: fail-closed Job/slot admission contracts for the one-shot operator identity.

The workload receives only `gmail-collector-db/COLLECTOR_DATABASE_URL`, whose login must be
`coop_evidence_collector`; it never receives the API/migration `DATABASE_URL`. PostgreSQL ACLs
make `ingestion_source` read-only, limit inserts to the other evidence tables and allow only
narrowly enumerated run/checkpoint updates.

The FULL job stays suspended and unarmed until the dedicated SOPS OAuth Secret, promoted API image, independently approved non-sentinel account fingerprint, explicitly enabled source kill switch and live NetworkPolicy/proxy/account/scope verification are complete. `scripts/gmail_full_pilot.py` provides the fail-closed read-only preflight and requires a short-lived, digest-bound live-attestation file plus explicit confirmation for the one-shot pilot path; it never unsuspends the CronJob. History delta and domain apply are not shipped in M3. See `docs/gmail-import-operations.md`.
