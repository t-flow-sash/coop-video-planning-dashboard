from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]


def documents(relative_path: str) -> list[dict[str, Any]]:
    with (ROOT / relative_path).open(encoding="utf-8") as stream:
        return [document for document in yaml.safe_load_all(stream) if document]


def document(relative_path: str) -> dict[str, Any]:
    parsed = documents(relative_path)
    if len(parsed) != 1:
        raise AssertionError(
            f"expected one YAML document in {relative_path}, got {len(parsed)}"
        )
    return parsed[0]


ALEMBIC_VERSIONS = ROOT / "apps/api/alembic/versions"


def alembic_heads() -> list[str]:
    """Resolve the migration graph head(s) without importing Alembic."""
    revisions: set[str] = set()
    parents: set[str] = set()
    for path in sorted(ALEMBIC_VERSIONS.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        revision = re.search(r'^revision: str = "(\w+)"$', source, re.MULTILINE)
        if revision is None:
            raise AssertionError(f"missing revision in {path.name}")
        revisions.add(revision.group(1))
        parent = re.search(
            r'^down_revision: str \| None = "(\w+)"$', source, re.MULTILINE
        )
        if parent is not None:
            parents.add(parent.group(1))
    return sorted(revisions - parents)


def rendered_documents(relative_path: str) -> list[dict[str, Any]]:
    rendered = subprocess.run(
        ["kubectl", "kustomize", str(ROOT / relative_path)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return [item for item in yaml.safe_load_all(rendered.stdout) if item]


class FreshBootstrapTests(unittest.TestCase):
    def test_github_ed25519_host_key_is_versioned_and_no_keyscan_trust_remains(
        self,
    ) -> None:
        script = (ROOT / "scripts/bootstrap-production-host.sh").read_text(
            encoding="utf-8"
        )
        known_hosts = (ROOT / "config/github-known-hosts").read_text(encoding="utf-8")
        self.assertEqual(
            known_hosts,
            "github.com ssh-ed25519 "
            "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl\n",
        )
        self.assertNotIn("ssh-keyscan", script)
        self.assertIn("SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU", script)
        self.assertIn("create secret generic coop-git-readonly", script)
        self.assertIn('--from-file=identity="$CONFIG_DIR/flux-github-readonly"', script)
        self.assertIn('--from-file=known_hosts="$GITHUB_KNOWN_HOSTS_FILE"', script)
        self.assertNotIn("--known-hosts-file", script)

    def test_bootstrap_is_two_stage_fail_closed_and_root_is_self_managed(self) -> None:
        script = (ROOT / "scripts/bootstrap-production-host.sh").read_text(
            encoding="utf-8"
        )
        root = document("clusters/production/root-kustomization.yaml")
        cluster = document("clusters/production/kustomization.yaml")

        self.assertIn("--install-flux-controllers", script)
        self.assertIn("--bootstrap-coop-root", script)
        self.assertRegex(
            script,
            r"kubectl wait --for=condition=Established[^\n]+customresourcedefinition",
        )
        flux_crds = {
            item["metadata"]["name"]
            for item in documents(
                "clusters/production/flux-system/gotk-components.yaml"
            )
            if item.get("kind") == "CustomResourceDefinition"
        }
        self.assertGreater(len(flux_crds), 0)
        for crd in flux_crds:
            self.assertIn(f"customresourcedefinition/{crd}", script)
        for controller in (
            "source-controller",
            "kustomize-controller",
            "helm-controller",
            "notification-controller",
        ):
            self.assertRegex(
                script,
                rf"kubectl wait [^\n]*--for=condition=Available[^\n]+deployment/{controller}",
            )

        stage_two = script.index("--bootstrap-coop-root")
        readiness = script.index("wait_flux_ready", stage_two)
        secret_creation = script.index("create secret generic sops-age", stage_two)
        source_apply = script.index(
            "apply -f clusters/production/source.yaml", stage_two
        )
        source_ready = script.index(
            "gitrepository/coop-video-planning --timeout=5m", stage_two
        )
        root_apply = script.index("root-kustomization.yaml", stage_two)
        self.assertLess(readiness, secret_creation)
        self.assertLess(secret_creation, source_apply)
        self.assertLess(source_apply, source_ready)
        self.assertLess(source_ready, root_apply)

        self.assertEqual(root["kind"], "Kustomization")
        self.assertEqual(
            root["metadata"], {"name": "coop-root", "namespace": "flux-system"}
        )
        self.assertEqual(root["spec"]["path"], "./clusters/production")
        self.assertEqual(
            root["spec"]["sourceRef"],
            {"kind": "GitRepository", "name": "coop-video-planning"},
        )
        self.assertTrue(root["spec"]["prune"])
        self.assertFalse(root["spec"]["wait"])
        self.assertEqual(
            root["spec"]["decryption"],
            {"provider": "sops", "secretRef": {"name": "sops-age"}},
        )
        self.assertNotIn("dependsOn", root["spec"])
        self.assertIn("root-kustomization.yaml", cluster["resources"])

        workload_syncs = documents("clusters/production/flux-kustomizations.yaml")
        for sync in workload_syncs:
            dependencies = sync["spec"].get("dependsOn", [])
            self.assertNotIn({"name": "coop-root"}, dependencies)


class ImmutableImageTests(unittest.TestCase):
    FLUX_IMAGES = {
        "ghcr.io/fluxcd/source-controller": (
            "v1.9.6",
            "sha256:6a6693172589f8ff26123a231d5fa6ceb194a6efb4dc647cdf057c959f76a2e3",
        ),
        "ghcr.io/fluxcd/kustomize-controller": (
            "v1.9.6",
            "sha256:2ebeaa341da77d52b6abbbba5efcee0450d47f8b42f0e6f33b08f9020262d606",
        ),
        "ghcr.io/fluxcd/helm-controller": (
            "v1.6.5",
            "sha256:0d52fff5c4d476277b8fcb6beb9041e269adb5db943fe69f5a806ea0c92b1511",
        ),
        "ghcr.io/fluxcd/notification-controller": (
            "v1.9.4",
            "sha256:840f318265ee26f0d2c48a158bf7896b22aa4e998e320a18646309f0e40b15da",
        ),
    }
    POSTGRES = (
        "docker.io/library/postgres:17-alpine@"
        "sha256:b0f9560a2de083e2cc7382e75f808c7381a32852a7ec49117deedb300e552b24"
    )

    def test_flux_export_stays_official_and_overlay_pins_controller_indexes(
        self,
    ) -> None:
        export = (
            ROOT / "clusters/production/flux-system/gotk-components.yaml"
        ).read_bytes()
        self.assertEqual(
            hashlib.sha256(export).hexdigest(),
            "e0b32861e2701c26336b1a25e5d2896d9f1bc0eed68c2115e2a80277a93cdf03",
        )
        overlay = document("clusters/production/flux-system/kustomization.yaml")
        images = {
            item["name"]: (item["newTag"], item["digest"])
            for item in overlay.get("images", [])
        }
        self.assertEqual(images, self.FLUX_IMAGES)

    def test_all_current_third_party_runtime_images_are_digest_pinned(self) -> None:
        for relative_path in (
            "compose.yaml",
            ".github/workflows/ci.yml",
            "infrastructure/database/postgres-statefulset.yaml",
            "infrastructure/backup/backup-cronjob.yaml",
            "scripts/restore-postgres.sh",
        ):
            content = (ROOT / relative_path).read_text(encoding="utf-8")
            self.assertIn(self.POSTGRES, content, relative_path)
            self.assertNotRegex(
                content, r"(?<![\w/])postgres:17-alpine(?!@)", relative_path
            )

        bootstrap = (ROOT / "scripts/bootstrap-production-host.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "docker.io/library/registry:3.1.2@"
            "sha256:ddf754342cfc8acc51a56d5d0ab6af06826461864460636d8bd5c546dab2a7b8",
            bootstrap,
        )

    def test_dockerfile_frontends_and_bases_are_digest_pinned(self) -> None:
        expected = {
            "apps/api/Dockerfile": {
                "docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e",
                "ghcr.io/astral-sh/uv:python3.11-bookworm-slim@sha256:4f5d923c9dcea037f57bda425dd209f3ec643da2f0b74227f68d09dab0b3bb36",
                "python:3.11.15-slim-bookworm@sha256:d29f48a31a8b408ed19272ca1e7b10ebae13b240a27e862d3d4217c528e2e0c3",
            },
            "apps/web/Dockerfile": {
                "docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e",
                "node:24-alpine@sha256:44fce696f38ba8c987c4db89954051000bbd05ffd8d8659ba15ed4a8602e937f",
            },
        }
        for relative_path, references in expected.items():
            content = (ROOT / relative_path).read_text(encoding="utf-8")
            for reference in references:
                self.assertIn(reference, content, relative_path)
            for line in content.splitlines():
                if line.startswith("FROM "):
                    self.assertRegex(
                        line.split()[1], r"@sha256:[0-9a-f]{64}$", relative_path
                    )

    def test_every_rendered_container_image_is_digest_pinned(self) -> None:
        roots = (
            "infrastructure/storage",
            "infrastructure/database",
            "infrastructure/migration",
            "infrastructure/import",
            "infrastructure/semantic",
            "infrastructure/apps",
            "infrastructure/ingress",
            "infrastructure/backup",
            "clusters/production/flux-system",
        )
        images: list[tuple[str, str]] = []
        for root in roots:
            for item in rendered_documents(root):
                spec = item.get("spec", {})
                pod_specs = []
                if item.get("kind") in {
                    "Deployment",
                    "StatefulSet",
                    "DaemonSet",
                    "Job",
                }:
                    pod_specs.append(spec.get("template", {}).get("spec", {}))
                elif item.get("kind") == "CronJob":
                    pod_specs.append(
                        spec.get("jobTemplate", {})
                        .get("spec", {})
                        .get("template", {})
                        .get("spec", {})
                    )
                for pod_spec in pod_specs:
                    for container in [
                        *pod_spec.get("initContainers", []),
                        *pod_spec.get("containers", []),
                    ]:
                        images.append((root, container["image"]))
        self.assertGreater(len(images), 0)
        for root, image in images:
            self.assertRegex(image, r"@sha256:[0-9a-f]{64}$", f"{root}: {image}")


class RenderedSecurityContractTests(unittest.TestCase):
    @staticmethod
    def by_kind_name(root: str) -> dict[tuple[str, str], dict[str, Any]]:
        return {
            (item["kind"], item["metadata"]["name"]): item
            for item in rendered_documents(root)
        }

    def test_rendered_postgres_client_jobs_are_selectable_by_restore_guard(
        self,
    ) -> None:
        selector = "coop.example.com/postgres-client"
        migration = self.by_kind_name("infrastructure/migration")
        backup = self.by_kind_name("infrastructure/backup")
        migration_jobs = [
            resource
            for (kind, name), resource in migration.items()
            if kind == "Job" and re.fullmatch(r"coop-migration-[0-9a-f]{12}", name)
        ]
        self.assertEqual(len(migration_jobs), 1)
        migration_job = migration_jobs[0]
        seed = migration[("CronJob", "coop-seed")]
        backup_cron = backup[("CronJob", "postgres-backup")]

        self.assertEqual(migration_job["metadata"]["labels"][selector], "true")
        for cronjob in (seed, backup_cron):
            self.assertEqual(cronjob["metadata"]["labels"][selector], "true")
            self.assertEqual(
                cronjob["spec"]["jobTemplate"]["metadata"]["labels"][selector], "true"
            )

    def test_backup_waits_boundedly_for_network_policy_egress(self) -> None:
        backup = self.by_kind_name("infrastructure/backup")
        cronjob = backup[("CronJob", "postgres-backup")]
        command = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"][
            "containers"
        ][0]["args"][0]
        self.assertIn("until pg_isready --quiet", command)
        self.assertIn('if [ "$attempts" -ge 30 ]', command)
        self.assertIn("sleep 2", command)

    def test_ingress_is_lan_only_and_has_no_auth_middleware(self) -> None:
        objects = self.by_kind_name("infrastructure/ingress")
        ingress = objects[("IngressRoute", "coop-https")]
        routes = {route["match"]: route for route in ingress["spec"]["routes"]}
        # Authentication was removed deliberately: no route carries a middleware.
        self.assertEqual(
            sorted(routes),
            sorted(
                [
                    "Path(`/api/v1/health/live`)",
                    "Path(`/api/v1/health/ready`)",
                    "PathPrefix(`/api/v1`)",
                    "PathPrefix(`/`)",
                ]
            ),
        )
        for match, route in routes.items():
            self.assertFalse(route.get("middlewares"), match)

        middleware_names = {
            item["metadata"]["name"]
            for (kind, _name), item in objects.items()
            if kind == "Middleware"
        }
        self.assertEqual(middleware_names, {"redirect-https"})
        basic_auth_middlewares = [
            item
            for (kind, _name), item in objects.items()
            if kind == "Middleware" and "basicAuth" in item.get("spec", {})
        ]
        self.assertEqual(basic_auth_middlewares, [])

        # The secret is retained intentionally so the change stays revertible in one commit.
        self.assertEqual(objects[("Secret", "coop-basic-auth")]["type"], "Opaque")
        self.assertEqual(objects[("Secret", "coop-tls")]["type"], "kubernetes.io/tls")
        self.assertEqual(ingress["spec"]["tls"], {"secretName": "coop-tls"})

    def test_ingress_configures_coop_tls_as_the_single_default_certificate(
        self,
    ) -> None:
        objects = self.by_kind_name("infrastructure/ingress")
        stores = [item for (kind, _name), item in objects.items() if kind == "TLSStore"]
        self.assertEqual(len(stores), 1)
        self.assertEqual(stores[0]["apiVersion"], "traefik.io/v1alpha1")
        self.assertEqual(
            stores[0]["metadata"], {"name": "default", "namespace": "coop-prod"}
        )
        self.assertEqual(
            stores[0]["spec"],
            {"defaultCertificate": {"secretName": "coop-tls"}},
        )
        self.assertEqual(
            objects[("IngressRoute", "coop-https")]["spec"]["tls"],
            {"secretName": "coop-tls"},
        )

    def test_public_local_ca_is_a_strict_browser_compatible_p256_anchor(self) -> None:
        ca_path = ROOT / "docs/certificates/coop-local-ca.crt"
        details = subprocess.run(
            ["openssl", "x509", "-in", str(ca_path), "-noout", "-text"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        strict = subprocess.run(
            [
                "openssl",
                "verify",
                "-x509_strict",
                "-CAfile",
                str(ca_path),
                str(ca_path),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

        self.assertEqual(strict.returncode, 0, strict.stderr)
        self.assertIn("Public Key Algorithm: id-ecPublicKey", details)
        self.assertIn("ASN1 OID: prime256v1", details)
        self.assertRegex(
            details, r"X509v3 Basic Constraints: critical\s+CA:TRUE, pathlen:0"
        )
        self.assertRegex(
            details,
            r"X509v3 Key Usage: critical\s+Certificate Sign, CRL Sign",
        )
        self.assertIn("X509v3 Subject Key Identifier:", details)
        self.assertIn("X509v3 Authority Key Identifier:", details)

    def test_ingress_secret_remains_sops_encrypted(self) -> None:
        secrets = documents("infrastructure/ingress/ingress-secrets.secret.sops.yaml")
        self.assertEqual(
            {item["metadata"]["name"] for item in secrets},
            {"coop-basic-auth", "coop-tls"},
        )
        for secret in secrets:
            self.assertEqual(secret["kind"], "Secret")
            self.assertIn("sops", secret)
            encrypted_values = (
                secret.get("stringData") or secret.get("data") or {}
            ).values()
            self.assertTrue(encrypted_values)
            for value in encrypted_values:
                self.assertRegex(value, r"^ENC\[AES256_GCM,")

    @unittest.skipUnless(
        os.environ.get("SOPS_AGE_KEY_FILE"), "requires root-only SOPS key"
    )
    def test_decryptable_tls_secret_matches_the_public_ca_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            decrypted = subprocess.run(
                [
                    "sops",
                    "--decrypt",
                    str(
                        ROOT / "infrastructure/ingress/ingress-secrets.secret.sops.yaml"
                    ),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=True,
            ).stdout
            secrets = {
                item["metadata"]["name"]: item
                for item in yaml.safe_load_all(decrypted)
                if item
            }
            secret = secrets["coop-tls"]
            if "stringData" in secret:
                tls = secret["stringData"]
            else:
                tls = {
                    key: base64.b64decode(value).decode("utf-8")
                    for key, value in secret["data"].items()
                }
            leaf_path = Path(temp_dir) / "tls.crt"
            key_path = Path(temp_dir) / "tls.key"
            leaf_path.write_text(tls["tls.crt"], encoding="utf-8")
            key_path.write_text(tls["tls.key"], encoding="utf-8")
            leaf_path.chmod(0o600)
            key_path.chmod(0o600)

            leaf_details = subprocess.run(
                ["openssl", "x509", "-in", str(leaf_path), "-noout", "-text"],
                text=True,
                capture_output=True,
                check=True,
            ).stdout
            leaf_sans = subprocess.run(
                [
                    "openssl",
                    "x509",
                    "-in",
                    str(leaf_path),
                    "-noout",
                    "-ext",
                    "subjectAltName",
                ],
                text=True,
                capture_output=True,
                check=True,
            ).stdout
            certificate_public_key = subprocess.run(
                ["openssl", "x509", "-in", str(leaf_path), "-pubkey", "-noout"],
                capture_output=True,
                check=True,
            ).stdout
            private_public_key = subprocess.run(
                ["openssl", "pkey", "-in", str(key_path), "-pubout"],
                capture_output=True,
                check=True,
            ).stdout
            verify = subprocess.run(
                [
                    "openssl",
                    "verify",
                    "-x509_strict",
                    "-purpose",
                    "sslserver",
                    "-verify_ip",
                    "192.168.2.94",
                    "-CAfile",
                    str(ROOT / "docs/certificates/coop-local-ca.crt"),
                    str(leaf_path),
                ],
                text=True,
                capture_output=True,
            )

            self.assertEqual(verify.returncode, 0, verify.stderr)
            self.assertEqual(certificate_public_key, private_public_key)
            self.assertIn("Public Key Algorithm: id-ecPublicKey", leaf_details)
            self.assertIn("ASN1 OID: prime256v1", leaf_details)
            self.assertRegex(
                leaf_details, r"X509v3 Basic Constraints: critical\s+CA:FALSE"
            )
            self.assertRegex(
                leaf_details, r"X509v3 Key Usage: critical\s+Digital Signature"
            )
            self.assertRegex(
                leaf_details,
                r"X509v3 Extended Key Usage:\s+TLS Web Server Authentication",
            )
            self.assertEqual(
                set(re.findall(r"(?:IP Address|DNS):[^,\n]+", leaf_sans)),
                {
                    "IP Address:192.168.2.94",
                    "DNS:zionhermes",
                    "DNS:coop.zionhermes.local",
                },
            )

    def test_apps_render_all_three_http_probes(self) -> None:
        objects = self.by_kind_name("infrastructure/apps")
        expected = {
            "coop-api": {
                "startupProbe": "/api/v1/health/live",
                "readinessProbe": "/api/v1/health/ready",
                "livenessProbe": "/api/v1/health/live",
            },
            "coop-web": {
                "startupProbe": "/",
                "readinessProbe": "/",
                "livenessProbe": "/",
            },
        }
        for name, probes in expected.items():
            container = objects[("Deployment", name)]["spec"]["template"]["spec"][
                "containers"
            ][0]
            for probe, path in probes.items():
                self.assertEqual(
                    container[probe]["httpGet"]["path"], path, f"{name}/{probe}"
                )

    def test_application_network_policies_are_default_deny_and_least_privilege(
        self,
    ) -> None:
        objects = self.by_kind_name("infrastructure/database")
        policies = {
            name: item
            for (kind, name), item in objects.items()
            if kind == "NetworkPolicy"
        }
        self.assertEqual(
            set(policies),
            {
                "default-deny",
                "allow-dns-egress",
                "allow-traefik-to-apps",
                "allow-postgres-clients",
                "allow-postgres-egress",
            },
        )
        self.assertEqual(
            policies["default-deny"]["spec"]["policyTypes"], ["Ingress", "Egress"]
        )
        self.assertNotIn("ingress", policies["default-deny"]["spec"])
        self.assertNotIn("egress", policies["default-deny"]["spec"])
        dns = policies["allow-dns-egress"]["spec"]["egress"]
        self.assertEqual(
            {(port["protocol"], port["port"]) for port in dns[0]["ports"]},
            {("UDP", 53), ("TCP", 53)},
        )
        traefik = policies["allow-traefik-to-apps"]["spec"]
        self.assertEqual(
            set(traefik["podSelector"]["matchExpressions"][0]["values"]),
            {"coop-api", "coop-web"},
        )
        self.assertEqual(
            {port["port"] for port in traefik["ingress"][0]["ports"]},
            {3000, 8000},
        )
        for name, direction in (
            ("allow-postgres-clients", "ingress"),
            ("allow-postgres-egress", "egress"),
        ):
            rule = policies[name]["spec"][direction][0]
            self.assertEqual(rule["ports"], [{"protocol": "TCP", "port": 5432}])

    def test_import_schedulers_are_hardened_and_exactly_staged(
        self,
    ) -> None:
        objects = self.by_kind_name("infrastructure/import")
        cronjobs = {
            name: item for (kind, name), item in objects.items() if kind == "CronJob"
        }
        self.assertEqual(
            set(cronjobs),
            {"gmail-import-full", "gmail-import-bootstrap", "gmail-import-delta"},
        )
        expected_commands = {
            "gmail-import-full": ["full"],
            "gmail-import-bootstrap": ["bootstrap"],
            "gmail-import-delta": ["delta"],
        }
        expected_deadlines = {
            "gmail-import-full": 3600,
            "gmail-import-bootstrap": 3600,
            "gmail-import-delta": 900,
        }
        expected_activation = {
            "gmail-import-full": (True, "false"),
            "gmail-import-bootstrap": (True, "false"),
            "gmail-import-delta": (False, "true"),
        }
        images = set()
        for name, cronjob in cronjobs.items():
            spec = cronjob["spec"]
            self.assertEqual(spec["suspend"], expected_activation[name][0], name)
            self.assertEqual(spec["concurrencyPolicy"], "Forbid", name)
            self.assertEqual(spec["jobTemplate"]["spec"]["backoffLimit"], 0, name)
            self.assertEqual(
                spec["jobTemplate"]["spec"]["activeDeadlineSeconds"],
                expected_deadlines[name],
                name,
            )
            self.assertIn("startingDeadlineSeconds", spec, name)
            pod = spec["jobTemplate"]["spec"]["template"]["spec"]
            self.assertFalse(pod["automountServiceAccountToken"], name)
            self.assertTrue(pod["securityContext"]["runAsNonRoot"], name)
            self.assertEqual(
                pod["securityContext"]["seccompProfile"], {"type": "RuntimeDefault"}
            )
            container = pod["containers"][0]
            self.assertEqual(container["args"], expected_commands[name], name)
            images.add(container["image"])
            env = {item["name"]: item for item in container["env"]}
            self.assertEqual(
                env["GMAIL_IMPORT_ARMED"]["value"], expected_activation[name][1], name
            )
            query = env["GMAIL_IMPORT_QUERY"]["value"]
            expected_query_hash = hashlib.sha256(
                json.dumps(
                    query.strip(), sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            self.assertEqual(
                env["GMAIL_IMPORT_QUERY_SHA256"]["value"], expected_query_hash, name
            )
            self.assertTrue(
                container["securityContext"]["readOnlyRootFilesystem"], name
            )
            self.assertFalse(
                container["securityContext"]["allowPrivilegeEscalation"], name
            )
            self.assertEqual(
                container["securityContext"]["capabilities"], {"drop": ["ALL"]}
            )
            self.assertTrue(container["resources"]["requests"], name)
            self.assertTrue(container["resources"]["limits"], name)
        self.assertEqual(len(images), 1)
        image = images.pop()
        sentinel = (
            "127.0.0.1:5000/coop-api:git-000000000000@"
            "sha256:0000000000000000000000000000000000000000000000000000000000000000"
        )
        self.assertTrue(
            image == sentinel
            or re.fullmatch(
                r"127\.0\.0\.1:5000/coop-api:git-[0-9a-f]{12}@sha256:[0-9a-f]{64}",
                image,
            ),
            image,
        )

    def test_semantic_worker_is_armed_hardened_and_exactly_staged(self) -> None:
        objects = self.by_kind_name("infrastructure/semantic")
        cronjobs = {
            name: item for (kind, name), item in objects.items() if kind == "CronJob"
        }
        self.assertEqual(set(cronjobs), {"gmail-semantic-worker"})
        cronjob = cronjobs["gmail-semantic-worker"]
        spec = cronjob["spec"]
        # Activation: the scheduler runs with a promoted digest and an explicit
        # arm flag; the dormant pair (suspend true + armed false) was the gate.
        self.assertIs(spec["suspend"], False)
        self.assertEqual(spec["concurrencyPolicy"], "Forbid")
        self.assertEqual(spec["jobTemplate"]["spec"]["backoffLimit"], 0)
        self.assertEqual(spec["jobTemplate"]["spec"]["activeDeadlineSeconds"], 900)
        self.assertIn("startingDeadlineSeconds", spec)
        pod = spec["jobTemplate"]["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertEqual(
            pod["securityContext"]["seccompProfile"], {"type": "RuntimeDefault"}
        )
        container = pod["containers"][0]
        self.assertEqual(
            container["command"],
            ["python", "-m", "app.ingestion.gmail_semantic_queue"],
        )
        self.assertEqual(container["args"], ["--max-batch", "25"])
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
        self.assertEqual(
            container["securityContext"]["capabilities"], {"drop": ["ALL"]}
        )
        env = {item["name"]: item for item in container["env"]}
        self.assertEqual(env["SEMANTIC_WORKER_ARMED"]["value"], "true")
        self.assertEqual(env["HTTPS_PROXY"]["value"], "http://gmail-egress-proxy:10000")
        self.assertEqual(env["HTTP_PROXY"]["value"], "http://gmail-egress-proxy:10000")
        # The semantic writer must never borrow the collector or runtime credential.
        self.assertEqual(
            env["DATABASE_URL"]["valueFrom"]["secretKeyRef"],
            {"name": "gmail-semantic-db", "key": "SEMANTIC_DATABASE_URL"},
        )
        rendered = json.dumps(cronjob)
        self.assertNotIn("gmail-collector-db", rendered)
        self.assertNotIn("coop-runtime", rendered)
        sentinel = (
            "127.0.0.1:5000/coop-api:git-000000000000@"
            "sha256:0000000000000000000000000000000000000000000000000000000000000000"
        )
        self.assertTrue(
            container["image"] == sentinel
            or re.fullmatch(
                r"127\.0\.0\.1:5000/coop-api:git-[0-9a-f]{12}@sha256:[0-9a-f]{64}",
                container["image"],
            ),
            container["image"],
        )

    def test_semantic_egress_is_only_dns_postgres_and_controlled_proxy(self) -> None:
        objects = self.by_kind_name("infrastructure/semantic")
        policies = {
            name: item
            for (kind, name), item in objects.items()
            if kind == "NetworkPolicy"
        }
        self.assertEqual(set(policies), {"gmail-semantic-worker-egress"})
        policy = policies["gmail-semantic-worker-egress"]
        self.assertEqual(
            policy["spec"]["podSelector"]["matchLabels"],
            {"coop.example.com/semantic-worker": "true"},
        )
        self.assertEqual(policy["spec"]["policyTypes"], ["Egress"])
        egress = policy["spec"]["egress"]
        self.assertNotIn({}, egress)
        ports = {
            (port.get("protocol", "TCP"), port["port"])
            for rule in egress
            for port in rule.get("ports", [])
        }
        self.assertEqual(
            ports, {("UDP", 53), ("TCP", 53), ("TCP", 5432), ("TCP", 10000)}
        )
        targets = {
            (
                target.get("namespaceSelector", {})
                .get("matchLabels", {})
                .get("kubernetes.io/metadata.name"),
                target.get("podSelector", {})
                .get("matchLabels", {})
                .get("app.kubernetes.io/name"),
                target.get("podSelector", {}).get("matchLabels", {}).get("k8s-app"),
            )
            for rule in egress
            for target in rule.get("to", [])
        }
        self.assertEqual(
            targets,
            {
                (None, "gmail-egress-proxy", None),
                (None, "postgres", None),
                ("kube-system", None, "kube-dns"),
            },
        )

    def test_semantic_flux_wiring_and_ci_render_are_integrated(self) -> None:
        syncs = {
            item["metadata"]["name"]: item
            for item in documents("clusters/production/flux-kustomizations.yaml")
        }
        semantic_sync = syncs["coop-semantic"]
        self.assertEqual(semantic_sync["spec"]["path"], "./infrastructure/semantic")
        # Activation: the semantic sync is unsuspended and waits for database,
        # migration and import so the release migration Job provisions the writer
        # credential before the scheduler may run.
        self.assertIs(semantic_sync["spec"]["suspend"], False)
        self.assertEqual(
            semantic_sync["spec"]["dependsOn"],
            [
                {"name": "coop-database"},
                {"name": "coop-migration"},
                {"name": "coop-import"},
            ],
        )
        self.assertEqual(
            semantic_sync["spec"]["decryption"],
            {"provider": "sops", "secretRef": {"name": "sops-age"}},
        )
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        render_loop = re.search(r"for dir in ([^;]+); do", workflow)
        self.assertIsNotNone(render_loop)
        assert render_loop is not None
        # Without this render step the dormant semantic manifests would ship
        # without schema, digest-pin and dormancy validation.
        self.assertIn("infrastructure/semantic", render_loop.group(1))
        self.assertIn('"$dir" == infrastructure/semantic', workflow)
        # The arm-flag proof now lives in the validator the CI invokes; assert
        # both the invocation and the flag so a silent removal is still caught.
        self.assertIn("scripts/validate_semantic_activation.py", workflow)
        validator = (ROOT / "scripts/validate_semantic_activation.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("SEMANTIC_WORKER_ARMED", validator)
        # Activation promotes the semantic scheduler with a real digest; the
        # sentinel exception in CI covers only the dormant render.
        promotion = (ROOT / "scripts/build-and-push-images.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("infrastructure/semantic/semantic-cronjob.yaml", promotion)
        semantic_manifest = (
            ROOT / "infrastructure/semantic/semantic-cronjob.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn("SEMANTIC_WORKER_ARMED", semantic_manifest)

    def test_completed_operational_bootstrap_job_is_removed_from_desired_state(
        self,
    ) -> None:
        objects = self.by_kind_name("infrastructure/import")
        jobs = {name: item for (kind, name), item in objects.items() if kind == "Job"}
        self.assertEqual(jobs, {})

    def test_import_egress_is_only_dns_postgres_and_controlled_proxy(self) -> None:
        objects = self.by_kind_name("infrastructure/import")
        policies = {
            name: item
            for (kind, name), item in objects.items()
            if kind == "NetworkPolicy"
        }
        self.assertEqual(set(policies), {"gmail-import-egress", "gmail-egress-proxy"})
        importer_rules = policies["gmail-import-egress"]["spec"]["egress"]
        importer_ports = [port for rule in importer_rules for port in rule["ports"]]
        self.assertEqual(
            {(port.get("protocol", "TCP"), port["port"]) for port in importer_ports},
            {("UDP", 53), ("TCP", 53), ("TCP", 5432), ("TCP", 10000)},
        )
        self.assertNotIn("ipBlock", yaml.safe_dump(policies["gmail-import-egress"]))
        proxy_rules = policies["gmail-egress-proxy"]["spec"]["egress"]
        https_rule = next(
            rule
            for rule in proxy_rules
            if {port["port"] for port in rule["ports"]} == {443}
        )
        self.assertEqual(https_rule["to"][0]["ipBlock"]["cidr"], "0.0.0.0/0")
        self.assertIn("10.0.0.0/8", https_rule["to"][0]["ipBlock"]["except"])
        self.assertNotIn("192.0.2.10/32", yaml.safe_dump_all(policies.values()))

    def test_import_secret_example_is_value_free_and_production_secret_is_encrypted(
        self,
    ) -> None:
        example = document("infrastructure/import/gmail-oauth.secret.example.yaml")
        self.assertEqual(example["metadata"]["name"], "gmail-oauth")
        self.assertEqual(example["data"], {})
        text = (
            ROOT / "infrastructure/import/gmail-oauth.secret.example.yaml"
        ).read_text()
        for key in (
            "GMAIL_CLIENT_ID",
            "GMAIL_CLIENT_SECRET",
            "GMAIL_REFRESH_TOKEN",
            "GMAIL_ACCOUNT",
        ):
            self.assertIn(key, text)
        rendered = rendered_documents("infrastructure/import")
        oauth_secrets = [
            item
            for item in rendered
            if item.get("kind") == "Secret"
            and item.get("metadata", {}).get("name") == "gmail-oauth"
        ]
        self.assertEqual(len(oauth_secrets), 1)
        encrypted = oauth_secrets[0]
        self.assertIs(encrypted.get("immutable"), True)
        self.assertEqual(
            set(encrypted.get("stringData", {})),
            {
                "GMAIL_CLIENT_ID",
                "GMAIL_CLIENT_SECRET",
                "GMAIL_REFRESH_TOKEN",
                "GMAIL_ACCOUNT",
            },
        )
        self.assertTrue(
            all(
                isinstance(value, str) and value.startswith("ENC[")
                for value in encrypted["stringData"].values()
            )
        )
        self.assertIsInstance(encrypted.get("sops"), dict)

        collector_secrets = [
            item
            for item in rendered
            if item.get("kind") == "Secret"
            and item.get("metadata", {}).get("name") == "gmail-collector-db"
        ]
        self.assertEqual(len(collector_secrets), 1)
        collector = collector_secrets[0]
        self.assertIs(collector.get("immutable"), True)
        self.assertEqual(
            set(collector.get("stringData", {})), {"COLLECTOR_DATABASE_URL"}
        )
        self.assertTrue(
            collector["stringData"]["COLLECTOR_DATABASE_URL"].startswith("ENC[")
        )
        self.assertIsInstance(collector.get("sops"), dict)

    def test_pilot_operator_can_contain_but_cannot_delete_consumed_slot(self) -> None:
        objects = self.by_kind_name("infrastructure/import")
        service_account = objects[("ServiceAccount", "gmail-full-pilot-operator")]
        self.assertEqual(service_account["automountServiceAccountToken"], False)
        role = objects[("Role", "gmail-full-pilot-operator")]
        configmap_rules = [
            rule for rule in role["rules"] if "configmaps" in rule.get("resources", [])
        ]
        self.assertTrue(any("create" in rule["verbs"] for rule in configmap_rules))
        self.assertFalse(any("delete" in rule["verbs"] for rule in configmap_rules))
        self.assertTrue(
            any(
                "gmail-import-full-pilot-consumed" in rule.get("resourceNames", [])
                and rule["verbs"] == ["get"]
                for rule in configmap_rules
            )
        )
        secret_rules = [
            rule for rule in role["rules"] if "secrets" in rule.get("resources", [])
        ]
        self.assertTrue(secret_rules)
        self.assertFalse(
            any(
                verb in {"create", "delete", "patch", "update"}
                for rule in secret_rules
                for verb in rule["verbs"]
            )
        )
        self.assertEqual(
            {name for rule in secret_rules for name in rule.get("resourceNames", [])},
            {"gmail-oauth", "gmail-collector-db"},
        )
        flux_reader = objects[("ClusterRole", "gmail-full-pilot-flux-reader")]
        self.assertEqual(
            {tuple(rule["resourceNames"]) for rule in flux_reader["rules"]},
            {
                ("coop-root", "coop-database", "coop-migration", "coop-import"),
                ("coop-video-planning",),
                ("gmail-full-pilot-operator", "gmail-full-pilot-slot"),
            },
        )

    def test_pilot_admission_contract_closes_generic_create_and_update(self) -> None:
        objects = self.by_kind_name("infrastructure/import")
        params = objects[("ConfigMap", "gmail-full-pilot-admission-params")]
        cronjob = objects[("CronJob", "gmail-import-full")]
        container = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"][
            "containers"
        ][0]
        env = {item["name"]: item for item in container["env"]}
        self.assertEqual(params["data"]["image"], container["image"])
        self.assertEqual(
            params["data"]["imageSourceRevision"],
            cronjob["metadata"]["annotations"][
                "coop.example.com/image-source-revision"
            ],
        )
        self.assertEqual(params["data"]["query"], env["GMAIL_IMPORT_QUERY"]["value"])
        self.assertEqual(
            params["data"]["querySha256"],
            env["GMAIL_IMPORT_QUERY_SHA256"]["value"],
        )
        self.assertEqual(
            params["data"]["accountSha256"], env["GMAIL_ACCOUNT_SHA256"]["value"]
        )
        for name in ("gmail-full-pilot-operator", "gmail-full-pilot-slot"):
            policy = objects[("ValidatingAdmissionPolicy", name)]
            binding = objects[("ValidatingAdmissionPolicyBinding", name)]
            self.assertEqual(policy["spec"]["failurePolicy"], "Fail")
            self.assertEqual(
                policy["spec"]["matchConditions"],
                [
                    {
                        "name": "pilot-operator-only",
                        "expression": "request.userInfo.username == 'system:serviceaccount:coop-prod:gmail-full-pilot-operator'",
                    }
                ],
            )
            self.assertEqual(binding["spec"]["validationActions"], ["Deny"])
            self.assertEqual(binding["spec"]["policyName"], name)
            self.assertEqual(
                binding["spec"]["paramRef"],
                {
                    "name": "gmail-full-pilot-admission-params",
                    "namespace": "coop-prod",
                    "parameterNotFoundAction": "Deny",
                },
            )
        job_expressions = "\n".join(
            item["expression"]
            for item in objects[
                ("ValidatingAdmissionPolicy", "gmail-full-pilot-operator")
            ]["spec"]["validations"]
        )
        for required in (
            "request.name == 'gmail-import-full-pilot'",
            "object.spec.suspend == true",
            "oldObject.spec.suspend == true && object.spec.suspend == false",
            "oldObject.spec.template == object.spec.template",
            "params.data.image",
            "gmail-collector-db",
            "gmail-oauth",
            "!has(object.spec.template.spec.containers[0].lifecycle)",
            "!has(object.spec.template.spec.containers[0].ports)",
            "!has(object.spec.template.spec.containers[0].livenessProbe)",
            "!has(object.spec.template.spec.containers[0].readinessProbe)",
            "!has(object.spec.template.spec.containers[0].startupProbe)",
            "!has(object.spec.template.spec.containers[0].workingDir)",
            "!has(object.spec.template.spec.containers[0].restartPolicy)",
            "!has(object.spec.template.spec.containers[0].resizePolicy)",
            "!has(object.spec.template.spec.containers[0].securityContext.seccompProfile)",
            "!has(object.spec.template.spec.containers[0].securityContext.appArmorProfile)",
            "!has(object.spec.template.spec.containers[0].securityContext.capabilities.add)",
            "!has(object.spec.template.spec.containers[0].securityContext.runAsNonRoot)",
            "!has(object.spec.template.spec.containers[0].securityContext.runAsUser)",
            "!has(object.spec.template.spec.containers[0].securityContext.runAsGroup)",
            "object.spec.template.spec.schedulerName == 'default-scheduler'",
            "object.spec.template.spec.dnsPolicy == 'ClusterFirst'",
            "object.spec.template.spec.terminationGracePeriodSeconds == 30",
            "!has(object.spec.template.spec.topologySpreadConstraints)",
            "!has(object.spec.template.spec.priorityClassName)",
            "!has(object.spec.template.spec.priority)",
            "!has(object.spec.template.spec.preemptionPolicy)",
            "!has(object.spec.template.spec.activeDeadlineSeconds)",
            "!has(object.spec.template.spec.hostUsers)",
            "!has(object.spec.template.spec.os)",
            "!has(object.spec.template.spec.hostnameOverride)",
            "!has(object.spec.template.spec.dnsConfig)",
            "!has(object.spec.template.spec.overhead)",
            "!has(object.spec.template.spec.resources)",
            "!has(object.spec.template.spec.schedulingGroup)",
            "object.spec.template.spec.serviceAccount == 'gmail-full-pilot-runtime'",
            "!has(object.spec.template.spec.containers[0].restartPolicyRules)",
            "object.spec.template.spec.shareProcessNamespace == false",
            "object.spec.template.spec.containers[0].terminationMessagePath == '/dev/termination-log'",
            "object.spec.template.spec.containers[0].terminationMessagePolicy == 'File'",
            "!has(object.spec.template.spec.readinessGates)",
            "!has(object.spec.template.spec.schedulingGates)",
            "!has(object.spec.template.spec.resourceClaims)",
            "!has(object.spec.template.spec.hostname)",
            "!has(object.spec.template.spec.subdomain)",
            "object.spec.template.spec.containers[0].resources.requests['cpu'] == '50m'",
            "object.spec.template.spec.containers[0].resources.limits['memory'] == '512Mi'",
            "object.spec.template.spec.containers[0].volumeMounts[0].subPath == ''",
            "!has(object.spec.template.spec.containers[0].volumeMounts[0].subPathExpr)",
            "object.spec.template.spec.containers[0].stdin == false",
            "object.spec.template.spec.containers[0].stdinOnce == false",
            "object.spec.template.spec.containers[0].tty == false",
        ):
            self.assertIn(required, job_expressions)
        marker_expressions = "\n".join(
            item["expression"]
            for item in objects[("ValidatingAdmissionPolicy", "gmail-full-pilot-slot")][
                "spec"
            ]["validations"]
        )
        self.assertIn("gmail-import-full-pilot-consumed", marker_expressions)
        self.assertIn("object.immutable == true", marker_expressions)
        controller = (ROOT / "scripts/gmail_full_pilot.py").read_text(encoding="utf-8")
        finalizer_match = re.search(
            r'^PILOT_MARKER_FINALIZER = "([^"]+)"$', controller, re.MULTILINE
        )
        self.assertIsNotNone(finalizer_match)
        self.assertIn(finalizer_match.group(1), marker_expressions)  # type: ignore[union-attr]
        for kind, name in (
            ("ValidatingAdmissionPolicy", "gmail-full-pilot-operator"),
            ("ValidatingAdmissionPolicyBinding", "gmail-full-pilot-operator"),
            ("ValidatingAdmissionPolicy", "gmail-full-pilot-slot"),
            ("ValidatingAdmissionPolicyBinding", "gmail-full-pilot-slot"),
        ):
            rendered_spec = objects[(kind, name)]["spec"]
            digest = hashlib.sha256(
                json.dumps(
                    rendered_spec, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            self.assertIn(f'"{digest}"', controller)

    def test_import_flux_dependency_promotion_and_ci_render_are_integrated(
        self,
    ) -> None:
        syncs = {
            item["metadata"]["name"]: item
            for item in documents("clusters/production/flux-kustomizations.yaml")
        }
        import_sync = syncs["coop-import"]
        self.assertEqual(import_sync["spec"]["path"], "./infrastructure/import")
        self.assertEqual(
            import_sync["spec"]["dependsOn"],
            [{"name": "coop-database"}, {"name": "coop-migration"}],
        )
        promotion = (ROOT / "scripts/build-and-push-images.sh").read_text()
        self.assertIn(
            'update_image("infrastructure/import/kustomization.yaml"', promotion
        )
        self.assertIn(
            "infrastructure/import/gmail-full-pilot-admission.yaml", promotion
        )
        self.assertNotIn("infrastructure/import/gmail-bootstrap-job.yaml", promotion)
        self.assertNotIn("gmail-import-bootstrap-{short_sha}", promotion)
        self.assertIn("pilot_image =", promotion)
        self.assertIn("coop-image-promotion-transaction", promotion)
        self.assertIn("Recovering interrupted image-promotion transaction", promotion)
        self.assertIn("coop-image-promotion-transaction.initializing", promotion)
        self.assertIn("flock -n 9", promotion)
        self.assertIn(
            'cp -a -- infrastructure "${initializing_dir}/staged/"', promotion
        )
        self.assertIn('mv -- "$initializing_dir" "$transaction_dir"', promotion)
        self.assertIn('staged_dir="${transaction_dir}/staged/${dir}"', promotion)
        self.assertIn(
            "infrastructure/import", promotion[promotion.index("for dir in") :]
        )
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        render_loop = re.search(r"for dir in ([^;]+); do", workflow)
        self.assertIsNotNone(render_loop)
        self.assertIn("infrastructure/import", render_loop.group(1))

    def test_flux_egress_overlay_removes_allow_all_and_keeps_required_paths(
        self,
    ) -> None:
        objects = self.by_kind_name("clusters/production/flux-system")
        policy = objects[("NetworkPolicy", "allow-egress")]
        egress = policy["spec"]["egress"]
        self.assertNotIn({}, egress)
        ports = {
            (port.get("protocol", "TCP"), port["port"])
            for rule in egress
            for port in rule.get("ports", [])
        }
        self.assertTrue(
            {("UDP", 53), ("TCP", 53), ("TCP", 22), ("TCP", 443), ("TCP", 6443)}
            <= ports
        )
        self.assertTrue(
            any(
                target.get("namespaceSelector", {})
                .get("matchLabels", {})
                .get("kubernetes.io/metadata.name")
                == "flux-system"
                and target.get("podSelector") == {}
                for rule in egress
                for target in rule.get("to", [])
            )
        )


class ContinuousIntegrationContractTests(unittest.TestCase):
    def test_api_pytest_uses_the_api_project_configuration(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn("- run: cd apps/api && uv run pytest", workflow)
        self.assertIn(
            "\tcd apps/api && DATABASE_URL=$(DATABASE_URL) uv run pytest", makefile
        )
        self.assertIn("scripts.tests.test_gmail_full_pilot", workflow)

    def test_actions_are_full_sha_pinned_and_dependency_updates_are_configured(
        self,
    ) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        uses = re.findall(
            r"^\s*(?:-\s*)?uses:\s*([^\s#]+)(?:\s+#\s*(.+))?$", workflow, re.MULTILINE
        )
        self.assertGreater(len(uses), 0)
        for action, comment in uses:
            self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$", action)
            self.assertRegex(comment or "", r"^v?\d", action)
        dependabot = document(".github/dependabot.yml")
        ecosystems = {item["package-ecosystem"] for item in dependabot["updates"]}
        self.assertIn("github-actions", ecosystems)

    def test_ci_builds_each_real_image_once_and_smokes_read_only_non_root(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertEqual(
            len(re.findall(r"docker build[^\n]+apps/api/Dockerfile", workflow)), 1
        )
        self.assertEqual(
            len(re.findall(r"docker build[^\n]+apps/web/Dockerfile", workflow)), 1
        )
        self.assertIn("--file apps/api/Dockerfile apps/api", workflow)
        self.assertIn("--file apps/web/Dockerfile .", workflow)
        self.assertGreaterEqual(workflow.count("--read-only"), 2)
        self.assertIn("Config.User", workflow)
        self.assertIn("id -u", workflow)

    def test_ci_runs_migrations_from_built_production_image(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("name: Prove migrations from production API image", workflow)
        self.assertIn("coop_image_migration_test", workflow)
        self.assertIn('-e COLLECTOR_DATABASE_URL="$image_collector_url"', workflow)
        self.assertIn(
            '-e SEMANTIC_DATABASE_URL="$image_semantic_url" coop-api:ci)', workflow
        )
        self.assertIn(
            '"${image_run[@]}" python -m app.database_access provision', workflow
        )
        self.assertIn('"${image_run[@]}" alembic upgrade head', workflow)
        self.assertIn(
            '"${image_run[@]}" python -m app.database_access reconcile', workflow
        )
        # The semantic writer attestation depends on the database-wide invariants
        # the collector reconcile establishes, so pin the ordered chain: a later
        # reorder would only surface as a red run ~30 minutes in.
        self.assertLess(
            workflow.index('"${image_run[@]}" python -m app.database_access reconcile'),
            workflow.index(
                '"${image_run[@]}" python -m app.semantic_database_access reconcile'
            ),
        )
        self.assertIn(
            '"${image_run[@]}" python -m app.semantic_database_access provision',
            workflow,
        )
        # The promoted image must actually ship the semantic runner and writer ACL.
        self.assertIn("import app.ingestion.gmail_semantic_queue", workflow)
        self.assertRegex(workflow, r"import app\.database_access(;|\b)")
        self.assertIn("SELECT version_num FROM alembic_version", workflow)
        self.assertIn('source == ("gmail-open-coops-v1", "GMAIL", True)', workflow)
        heads = alembic_heads()
        # A single linear head keeps the production-image proof unambiguous.
        self.assertEqual(len(heads), 1, heads)
        # The image must ship the reviewed head; a stale literal here silently
        # proves the wrong revision, so it is derived from the migration graph.
        self.assertIn(f'assert version == ("{heads[0]}",)', workflow)
        self.assertIn("assert len(jobs) == 3", workflow)

    def test_global_render_image_gate_rejects_any_unpinned_image(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("scripts/validate-rendered-images.py", workflow)
        invalid = textwrap.dedent(
            """\
            apiVersion: apps/v1
            kind: Deployment
            metadata: {name: invalid}
            spec:
              template:
                spec:
                  containers:
                    - {name: app, image: example.invalid/app:v1}
            """
        )
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/validate-rendered-images.py")],
            input=invalid,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("example.invalid/app:v1", result.stderr)


class DocumentationContractTests(unittest.TestCase):
    def test_gmail_import_trust_boundary_is_explicit_and_fail_closed(self) -> None:
        runbook = (ROOT / "docs/gmail-import-operations.md").read_text(encoding="utf-8")
        for contract in (
            "PENDING_REVIEW",
            "technische Gmail-IDs",
            "Gmail `history.list`",
            "dormant",
            "keine API-, Service- oder Domain-Mutationsfläche",
            "full_required",
            "https://www.googleapis.com/auth/gmail.readonly",
            "oauth2.googleapis.com:443",
            "gmail.googleapis.com:443",
            "BLOCKED_REQUIRES_EVIDENCE_ENRICHMENT",
            "GMAIL_IMPORT_ARMED=false",
            "alle drei",
            "Gmail-CronJobs",
            "scripts/gmail_full_pilot.py",
            "--yes",
            "entsuspendiert den CronJob nie",
            "0700",
            "0600",
        ):
            self.assertIn(contract, runbook)

    def test_runbook_matches_firewall_auth_and_live_gate_reality(self) -> None:
        runbook = (ROOT / "docs/gitops-m2.md").read_text(encoding="utf-8")
        self.assertIn("scripts/bootstrap-production-host.sh --verify-firewall", runbook)
        self.assertIn(
            "Default: deny (incoming), allow (outgoing), deny (routed)", runbook
        )
        self.assertIn("API: `removeHeader: false`", runbook)
        self.assertIn("Web: `removeHeader: true`", runbook)
        self.assertIn("Abgeschlossenes B2-Deployment-Gate", runbook)
        self.assertIn("milestone-2-verification.md", runbook)
        self.assertIn("SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU", runbook)

    def test_m2_verification_contract_is_complete_and_accepted(self) -> None:
        verification_path = ROOT / "docs/milestone-2-verification.md"
        self.assertTrue(verification_path.is_file())
        verification = verification_path.read_text(encoding="utf-8")

        required_sections = (
            "## 1. Zweck, Scope und Architektur",
            "## 2. Verifizierte Versionen und unveränderliche Artefakte",
            "## 3. Git-, PR- und CI-Linie",
            "## 4. Flux-Bootstrap, Verschlüsselung und aktueller Graph",
            "## 5. Workloads, Daten, Migration und Storage",
            "## 6. HTTPS, Authentifizierung, CORS und TLS",
            "## 7. NetworkPolicy-Verifikation",
            "## 8. Browser-Transition, Persistenz und Drift",
            "## 9. Deklarativer PR-Rollout und Revert",
            "## 10. Backup- und Restore-Nachweis",
            "## 11. Security, Firewall und Listener",
            "## 12. Finalinventar und Evidenz",
            "## 13. Restmaßnahmen und Risikoklassifikation",
            "## 14. Nutzerabnahme",
        )
        for section in required_sections:
            self.assertIn(section, verification)

        self.assertIn("m2-final-network-policy.json", verification)
        self.assertIn("m2-controller-backup.json", verification)
        self.assertIn("**Nutzerabnahme:** erteilt", verification)
        self.assertIn("- [x] Ich, Sascha", verification)
        self.assertIn("**Datum:** 15. Oktober 2026", verification)
        self.assertIn("Discord-Nachricht `1560181857251885118`", verification)
        self.assertNotIn("- [ ] Ich, Sascha", verification)

        def extract_section(number: int, next_number: int) -> str:
            start = verification.index(f"## {number}.")
            end = verification.index(f"## {next_number}.", start)
            return verification[start:end]

        network_policy = extract_section(7, 8)
        network_policy_compact = " ".join(network_policy.split())
        for scenario in (
            "dns_udp_tcp_allowed",
            "postgres_unlabeled_same_namespace_denied",
            "postgres_labeled_same_namespace_allowed",
            "postgres_labeled_cross_namespace_denied",
            "external_https_unlabeled_denied",
            "traefik_to_api_allowed",
            "traefik_to_web_allowed",
        ):
            self.assertIn(scenario, network_policy)
        for classification in (
            "dns_resolution_and_tcp53_allowed",
            "tcp_timeout_after_successful_dns_network_policy_denied",
            "postgres_accepting_connections",
            "http_200_service_allowed",
        ):
            self.assertIn(classification, network_policy)
        self.assertIn(
            "Schema `coop-dashboard-network-policy-live-evidence-v2`",
            network_policy_compact,
        )
        self.assertRegex(network_policy_compact, r"SHA-256 `[0-9a-f]{64}`")
        self.assertRegex(network_policy, r"[0-9][0-9.]* Byte")
        self.assertIn(
            "DNS wurde bei allen zielnamensbasierten Fällen separat erfolgreich geprüft",
            network_policy_compact,
        )
        self.assertIn("keine Probe-Pods verblieben", network_policy)

        backup = extract_section(10, 11)
        backup_compact = " ".join(backup.split())
        backup_cronjob = document("infrastructure/backup/backup-cronjob.yaml")
        self.assertEqual(backup_cronjob["kind"], "CronJob")
        schedule = backup_cronjob["spec"]["schedule"]
        timezone = backup_cronjob["spec"]["timeZone"]
        self.assertEqual(schedule, "17 2 * * *")
        self.assertEqual(timezone, "Europe/Berlin")
        for proof in (
            "vom CronJob-Controller geplant",
            "batch.kubernetes.io/cronjob-scheduled-timestamp",
            "OwnerReference",
            "Complete=True",
            "pg_restore --list",
            "m2-controller-backup.json",
            "Seed-CronJob blieb suspendiert",
            f"Zeitplan `{schedule}`",
            f"`{timezone}`",
            "Exakt ein Controller-Lauf",
            "keinen weiteren Controller-Lauf",
            "keine aktiven oder fehlgeschlagenen Backup-Jobs",
        ):
            self.assertIn(proof, backup_compact)
        self.assertRegex(
            backup, r"Job-UID `[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}`"
        )
        self.assertRegex(backup, r"Scheduled-Timestamp-Annotation")
        self.assertRegex(backup, r"Exit 0")
        self.assertRegex(backup, r"15\.911 Byte")
        self.assertIn("47 erfolgreiche Zeilen", backup_compact)
        self.assertIn("PR #14", backup_compact)
        self.assertIn("PR #15", backup_compact)
        for run_id in (
            "38681431135",
            "38681436517",
            "38682065639",
            "38685425590",
            "38685429273",
            "38685912239",
        ):
            self.assertIn(f"actions/runs/{run_id}", backup_compact)
        self.assertIn(
            "Schema `coop-dashboard-controller-backup-evidence-v1`", backup_compact
        )
        self.assertIn("13.799 Byte", backup_compact)
        self.assertIn(
            "d0c48c4db3c99077a41182446a0f546dbc8597149b95abc513633ab0a1fcaca3",
            backup_compact,
        )
        self.assertIn("5662e306c7d06c402f05536b92a0005f25f95da0", verification)

        inventory = extract_section(12, 13)
        self.assertIn("gemeinsamen root-only Store", inventory)
        self.assertIn("Verzeichnismodus `0700`", inventory)
        self.assertIn("Dateimodus `0600`", inventory)
        self.assertNotIn("getrennt geschützten root-only Stores", verification)
        self.assertNotIn("physisch getrennt", verification)

        private_path_patterns = (
            r"(?<![\w.])/(?:root|home|Users|tmp|var/lib|etc|run|srv|opt|mnt)/",
            r"\b[A-Za-z]:\\",
            r"\\\\[^\s]+\\[^\s]+",
        )
        for pattern in private_path_patterns:
            self.assertNotRegex(verification, pattern)

        secret_patterns = (
            r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
            r"AGE-SECRET-KEY-[A-Z0-9]+",
            r"\b(?:github_pat_|gh[opusr]_)[A-Za-z0-9_]+",
            r"\$2[aby]\$\d{2}\$[./A-Za-z0-9]{20,}",
            r"Authorization:\s*(?:Basic|Bearer)\s+[A-Za-z0-9._~+/=-]+",
            r"(?im)^\s*(?:password|api_key|client_secret|access_token)\s*[:=]\s*(?!<redacted>|\$\{)[^\s]+",
        )
        for pattern in secret_patterns:
            self.assertNotRegex(verification, pattern)

    def test_retained_pv_namespace_loss_runbook_is_fail_closed_and_identity_checked(
        self,
    ) -> None:
        runbook = (ROOT / "docs/gitops-m2.md").read_text(encoding="utf-8")
        heading = "## Retained-PV-Recovery nach Namespaceverlust"
        start = runbook.index(heading)
        end = runbook.find("\n## ", start + len(heading))
        recovery = runbook[start : end if end >= 0 else None]

        required_guards = (
            "Namespaceverlust",
            "Released",
            "claimRef",
            "hostPath",
            "NodeAffinity",
            "read-only",
            "volumeName",
            "UID",
            "Kapazität",
            "StorageClass",
            "leere neue Volumes",
            "niemals vertauscht",
            "Backup-First",
            "Abbruch",
        )
        for guard in required_guards:
            self.assertIn(guard, recovery)

        pre_inventory = recovery.index(
            "Unveränderlichen Checkpoint vor Suspension erstellen"
        )
        suspension = recovery.index("Source und alle Kustomizations suspendieren")
        quiescence = recovery.index("Echte Flux-Quieszenz beweisen")
        post_inventory = recovery.index("Inventur nach Quieszenz wiederholen")
        drift_gate = recovery.index("Storage-Drift fail-closed ausschließen")
        claim_ref_release = recovery.index("claimRef kontrolliert lösen")
        self.assertLess(pre_inventory, suspension)
        self.assertLess(suspension, quiescence)
        self.assertLess(quiescence, post_inventory)
        self.assertLess(post_inventory, drift_gate)
        self.assertLess(drift_gate, claim_ref_release)

        suspend_block = recovery[suspension:quiescence]
        self.assertIn("Git-Source `coop-git`", suspend_block)
        for boundary in (
            "coop-root",
            "coop-database",
            "coop-migration",
            "coop-import",
            "coop-apps",
            "coop-ingress",
            "coop-backup",
        ):
            self.assertIn(f"`{boundary}`", suspend_block)
        self.assertIn("spec.suspend=true", suspend_block)
        self.assertIn("read-back", suspend_block)

        quiescence_block = recovery[quiescence:post_inventory]
        self.assertIn("Reconciling=True", quiescence_block)
        self.assertIn("keine aktive", quiescence_block)
        self.assertIn("laufende Controller-Operationen", quiescence_block)
        self.assertIn("Timeout", quiescence_block)

        drift_block = recovery[post_inventory:claim_ref_release]
        for drift_guard in (
            "Neue oder ersetzte PVCs/PVs",
            "UIDs",
            "claimRefs",
            "Kapazität",
            "StorageClass",
            "hostPath",
            "NodeAffinity",
            "unerwartete Claims",
            "unmittelbar vor dem Lösen",
        ):
            self.assertIn(drift_guard, drift_block)
        self.assertIn("automatische Neuprovisionierung", drift_block)

        binding = recovery[claim_ref_release:]
        self.assertIn("spec.volumeName", binding)
        self.assertIn("postgres-data", binding)
        self.assertIn("postgres-backups", binding)
        self.assertIn("Bound", binding)
        self.assertIn("PVC-UID", binding)

        resume = recovery.index("Vollständig und einzeln fortsetzen")
        resume_block = recovery[resume:]
        resume_order = (
            "Source",
            "coop-database",
            "coop-migration",
            "coop-import",
            "coop-apps",
            "coop-ingress",
            "coop-backup",
            "coop-root",
        )
        positions = [resume_block.index(item) for item in resume_order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("reconcile", resume_block)
        self.assertIn("Ready", resume_block)
        self.assertIn("Stop on failure", resume_block)


class DurableStorageTests(unittest.TestCase):
    def test_local_path_storage_class_is_retained_and_waits_for_consumer(self) -> None:
        storage_class = document("infrastructure/storage/storageclass.yaml")
        self.assertEqual(storage_class["apiVersion"], "storage.k8s.io/v1")
        self.assertEqual(storage_class["kind"], "StorageClass")
        self.assertEqual(storage_class["metadata"]["name"], "coop-local-path")
        self.assertEqual(storage_class["provisioner"], "rancher.io/local-path")
        self.assertEqual(storage_class["reclaimPolicy"], "Retain")
        self.assertEqual(storage_class["volumeBindingMode"], "WaitForFirstConsumer")

        storage_kustomization = document("infrastructure/storage/kustomization.yaml")
        self.assertEqual(storage_kustomization["resources"], ["storageclass.yaml"])

    def test_database_and_backup_use_explicit_prune_protected_claims(self) -> None:
        claims = {
            claim["metadata"]["name"]: claim
            for claim in (
                document("infrastructure/database/postgres-data-pvc.yaml"),
                document("infrastructure/backup/backup-pvc.yaml"),
            )
        }
        self.assertEqual(set(claims), {"postgres-data", "postgres-backups"})
        for name, claim in claims.items():
            self.assertEqual(claim["kind"], "PersistentVolumeClaim")
            self.assertEqual(claim["spec"]["storageClassName"], "coop-local-path", name)
            self.assertEqual(
                claim["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/prune"],
                "disabled",
                name,
            )

        stateful_set = document("infrastructure/database/postgres-statefulset.yaml")
        self.assertNotIn("volumeClaimTemplates", stateful_set["spec"])
        data_volume = next(
            volume
            for volume in stateful_set["spec"]["template"]["spec"]["volumes"]
            if volume["name"] == "data"
        )
        self.assertEqual(
            data_volume["persistentVolumeClaim"]["claimName"], "postgres-data"
        )
        database_kustomization = document("infrastructure/database/kustomization.yaml")
        self.assertIn("postgres-data-pvc.yaml", database_kustomization["resources"])

    def test_namespace_is_prune_protected_and_storage_precedes_database(self) -> None:
        namespace = document("infrastructure/database/namespace.yaml")
        self.assertEqual(
            namespace["metadata"]["annotations"]["kustomize.toolkit.fluxcd.io/prune"],
            "disabled",
        )

        syncs = {
            item["metadata"]["name"]: item
            for item in documents("clusters/production/flux-kustomizations.yaml")
        }
        self.assertEqual(
            syncs["coop-storage"]["spec"]["path"], "./infrastructure/storage"
        )
        self.assertTrue(syncs["coop-storage"]["spec"]["prune"])
        self.assertEqual(
            syncs["coop-database"]["spec"]["dependsOn"], [{"name": "coop-storage"}]
        )

        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("python3 -m unittest scripts.tests.test_gitops_static", workflow)
        self.assertIn("scripts.tests.test_operational_scripts", workflow)
        render_loop = re.search(r"for dir in ([^;]+); do", workflow)
        if render_loop is None:
            self.fail("GitOps render loop is missing")
        self.assertIn("infrastructure/storage", render_loop.group(1))


if __name__ == "__main__":
    unittest.main()
