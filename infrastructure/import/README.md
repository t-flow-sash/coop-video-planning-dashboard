# Gmail FULL evidence manifests

- `gmail-import-cronjobs.yaml`: suspended, read-only FULL evidence collector.
- `gmail-egress-proxy.yaml`: rootless digest-pinned Envoy CONNECT proxy with exact Google OAuth/Gmail authorities.
- `network-policy.yaml`: importer may reach only DNS, PostgreSQL and the proxy; only the proxy has public TCP/443 egress.
- `gmail-oauth.secret.example.yaml`: value-free secret shape only; never add plaintext values.

The workload receives only `coop-runtime/COLLECTOR_DATABASE_URL`, whose login must be
`coop_evidence_collector`; it never receives the API/migration `DATABASE_URL`. PostgreSQL ACLs
limit this role to evidence-table inserts/selects and narrowly enumerated run/checkpoint updates.

The FULL job stays suspended until the dedicated SOPS OAuth Secret, promoted API image and live NetworkPolicy/proxy/account/scope verification are complete. History delta and domain apply are not shipped in M3. See `docs/gmail-import-operations.md`.
