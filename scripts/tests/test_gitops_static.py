from __future__ import annotations

import base64
import hashlib
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
        raise AssertionError(f"expected one YAML document in {relative_path}, got {len(parsed)}")
    return parsed[0]


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
    def test_github_ed25519_host_key_is_versioned_and_no_keyscan_trust_remains(self) -> None:
        script = (ROOT / "scripts/bootstrap-production-host.sh").read_text(encoding="utf-8")
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
        script = (ROOT / "scripts/bootstrap-production-host.sh").read_text(encoding="utf-8")
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
            for item in documents("clusters/production/flux-system/gotk-components.yaml")
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
        source_apply = script.index("apply -f clusters/production/source.yaml", stage_two)
        source_ready = script.index(
            "gitrepository/coop-video-planning --timeout=5m", stage_two
        )
        root_apply = script.index("root-kustomization.yaml", stage_two)
        self.assertLess(readiness, secret_creation)
        self.assertLess(secret_creation, source_apply)
        self.assertLess(source_apply, source_ready)
        self.assertLess(source_ready, root_apply)

        self.assertEqual(root["kind"], "Kustomization")
        self.assertEqual(root["metadata"], {"name": "coop-root", "namespace": "flux-system"})
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

    def test_flux_export_stays_official_and_overlay_pins_controller_indexes(self) -> None:
        export = (ROOT / "clusters/production/flux-system/gotk-components.yaml").read_bytes()
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
            self.assertNotRegex(content, r"(?<![\w/])postgres:17-alpine(?!@)", relative_path)

        bootstrap = (ROOT / "scripts/bootstrap-production-host.sh").read_text(encoding="utf-8")
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
                    self.assertRegex(line.split()[1], r"@sha256:[0-9a-f]{64}$", relative_path)

    def test_every_rendered_container_image_is_digest_pinned(self) -> None:
        roots = (
            "infrastructure/storage",
            "infrastructure/database",
            "infrastructure/migration",
            "infrastructure/import",
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
                if item.get("kind") in {"Deployment", "StatefulSet", "DaemonSet", "Job"}:
                    pod_specs.append(spec.get("template", {}).get("spec", {}))
                elif item.get("kind") == "CronJob":
                    pod_specs.append(
                        spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec", {})
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

    def test_rendered_postgres_client_jobs_are_selectable_by_restore_guard(self) -> None:
        selector = "coop.example.com/postgres-client"
        migration = self.by_kind_name("infrastructure/migration")
        backup = self.by_kind_name("infrastructure/backup")
        migration_job = migration[("Job", "coop-migration-9e31c2b8a4f0")]
        seed = migration[("CronJob", "coop-seed")]
        backup_cron = backup[("CronJob", "postgres-backup")]

        self.assertEqual(migration_job["metadata"]["labels"][selector], "true")
        for cronjob in (seed, backup_cron):
            self.assertEqual(cronjob["metadata"]["labels"][selector], "true")
            self.assertEqual(cronjob["spec"]["jobTemplate"]["metadata"]["labels"][selector], "true")

    def test_backup_waits_boundedly_for_network_policy_egress(self) -> None:
        backup = self.by_kind_name("infrastructure/backup")
        cronjob = backup[("CronJob", "postgres-backup")]
        command = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0][
            "args"
        ][0]
        self.assertIn("until pg_isready --quiet", command)
        self.assertIn('if [ "$attempts" -ge 30 ]', command)
        self.assertIn("sleep 2", command)

    def test_ingress_has_only_exact_public_health_and_expected_auth_contracts(self) -> None:
        objects = self.by_kind_name("infrastructure/ingress")
        ingress = objects[("IngressRoute", "coop-https")]
        routes = {route["match"]: route for route in ingress["spec"]["routes"]}
        public = {match for match, route in routes.items() if not route.get("middlewares")}
        self.assertEqual(
            public,
            {"Path(`/api/v1/health/live`)", "Path(`/api/v1/health/ready`)"},
        )
        self.assertEqual(
            routes["PathPrefix(`/api/v1`)"]["middlewares"],
            [{"name": "coop-api-auth"}],
        )
        self.assertEqual(
            routes["PathPrefix(`/`)"]["middlewares"],
            [{"name": "coop-web-auth"}],
        )
        self.assertEqual(ingress["spec"]["tls"], {"secretName": "coop-tls"})

        api_auth = objects[("Middleware", "coop-api-auth")]["spec"]["basicAuth"]
        web_auth = objects[("Middleware", "coop-web-auth")]["spec"]["basicAuth"]
        self.assertEqual(api_auth, {"secret": "coop-basic-auth", "removeHeader": False})
        self.assertEqual(web_auth, {"secret": "coop-basic-auth", "removeHeader": True})
        self.assertEqual(objects[("Secret", "coop-basic-auth")]["type"], "Opaque")
        self.assertEqual(objects[("Secret", "coop-tls")]["type"], "kubernetes.io/tls")

    def test_ingress_configures_coop_tls_as_the_single_default_certificate(self) -> None:
        objects = self.by_kind_name("infrastructure/ingress")
        stores = [
            item
            for (kind, _name), item in objects.items()
            if kind == "TLSStore"
        ]
        self.assertEqual(len(stores), 1)
        self.assertEqual(stores[0]["apiVersion"], "traefik.io/v1alpha1")
        self.assertEqual(stores[0]["metadata"], {"name": "default", "namespace": "coop-prod"})
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
            ["openssl", "verify", "-x509_strict", "-CAfile", str(ca_path), str(ca_path)],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )

        self.assertEqual(strict.returncode, 0, strict.stderr)
        self.assertIn("Public Key Algorithm: id-ecPublicKey", details)
        self.assertIn("ASN1 OID: prime256v1", details)
        self.assertRegex(details, r"X509v3 Basic Constraints: critical\s+CA:TRUE, pathlen:0")
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
            encrypted_values = (secret.get("stringData") or secret.get("data") or {}).values()
            self.assertTrue(encrypted_values)
            for value in encrypted_values:
                self.assertRegex(value, r"^ENC\[AES256_GCM,")

    @unittest.skipUnless(os.environ.get("SOPS_AGE_KEY_FILE"), "requires root-only SOPS key")
    def test_decryptable_tls_secret_matches_the_public_ca_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            decrypted = subprocess.run(
                [
                    "sops",
                    "--decrypt",
                    str(ROOT / "infrastructure/ingress/ingress-secrets.secret.sops.yaml"),
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
                ["openssl", "x509", "-in", str(leaf_path), "-noout", "-ext", "subjectAltName"],
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
            self.assertRegex(leaf_details, r"X509v3 Basic Constraints: critical\s+CA:FALSE")
            self.assertRegex(leaf_details, r"X509v3 Key Usage: critical\s+Digital Signature")
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
            container = objects[("Deployment", name)]["spec"]["template"]["spec"]["containers"][0]
            for probe, path in probes.items():
                self.assertEqual(container[probe]["httpGet"]["path"], path, f"{name}/{probe}")

    def test_application_network_policies_are_default_deny_and_least_privilege(self) -> None:
        objects = self.by_kind_name("infrastructure/database")
        policies = {
            name: item
            for (kind, name), item in objects.items()
            if kind == "NetworkPolicy"
        }
        self.assertEqual(
            set(policies),
            {"default-deny", "allow-dns-egress", "allow-traefik-to-apps", "allow-postgres-clients", "allow-postgres-egress"},
        )
        self.assertEqual(policies["default-deny"]["spec"]["policyTypes"], ["Ingress", "Egress"])
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

    def test_import_jobs_are_suspended_hardened_and_share_one_immutable_image(self) -> None:
        objects = self.by_kind_name("infrastructure/import")
        cronjobs = {
            name: item
            for (kind, name), item in objects.items()
            if kind == "CronJob"
        }
        self.assertEqual(set(cronjobs), {"gmail-import-full"})
        images = set()
        for name, cronjob in cronjobs.items():
            spec = cronjob["spec"]
            self.assertTrue(spec["suspend"], name)
            self.assertEqual(spec["concurrencyPolicy"], "Forbid", name)
            self.assertIn("startingDeadlineSeconds", spec, name)
            pod = spec["jobTemplate"]["spec"]["template"]["spec"]
            self.assertFalse(pod["automountServiceAccountToken"], name)
            self.assertTrue(pod["securityContext"]["runAsNonRoot"], name)
            self.assertEqual(pod["securityContext"]["seccompProfile"], {"type": "RuntimeDefault"})
            container = pod["containers"][0]
            images.add(container["image"])
            self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"], name)
            self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"], name)
            self.assertEqual(container["securityContext"]["capabilities"], {"drop": ["ALL"]})
            self.assertTrue(container["resources"]["requests"], name)
            self.assertTrue(container["resources"]["limits"], name)
        self.assertEqual(
            images,
            {
                "127.0.0.1:5000/coop-api:git-000000000000@"
                "sha256:0000000000000000000000000000000000000000000000000000000000000000"
            },
        )

    def test_import_egress_is_only_dns_postgres_and_controlled_proxy(self) -> None:
        objects = self.by_kind_name("infrastructure/import")
        policies = {
            name: item for (kind, name), item in objects.items() if kind == "NetworkPolicy"
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
            rule for rule in proxy_rules if {port["port"] for port in rule["ports"]} == {443}
        )
        self.assertEqual(https_rule["to"][0]["ipBlock"]["cidr"], "0.0.0.0/0")
        self.assertIn("10.0.0.0/8", https_rule["to"][0]["ipBlock"]["except"])
        self.assertNotIn("192.0.2.10/32", yaml.safe_dump_all(policies.values()))

    def test_import_secret_example_is_value_free_and_not_rendered(self) -> None:
        example = document("infrastructure/import/gmail-oauth.secret.example.yaml")
        self.assertEqual(example["metadata"]["name"], "gmail-oauth")
        self.assertEqual(example["data"], {})
        text = (ROOT / "infrastructure/import/gmail-oauth.secret.example.yaml").read_text()
        for key in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "GMAIL_ACCOUNT"):
            self.assertIn(key, text)
        rendered_names = {
            item.get("metadata", {}).get("name") for item in rendered_documents("infrastructure/import")
        }
        self.assertNotIn("gmail-oauth", rendered_names)

    def test_import_flux_dependency_promotion_and_ci_render_are_integrated(self) -> None:
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
        self.assertIn('update_image("infrastructure/import/kustomization.yaml"', promotion)
        self.assertIn("infrastructure/import", promotion[promotion.index("for dir in") :])
        workflow = (ROOT / ".github/workflows/ci.yml").read_text()
        render_loop = re.search(r"for dir in ([^;]+); do", workflow)
        self.assertIsNotNone(render_loop)
        self.assertIn("infrastructure/import", render_loop.group(1))

    def test_flux_egress_overlay_removes_allow_all_and_keeps_required_paths(self) -> None:
        objects = self.by_kind_name("clusters/production/flux-system")
        policy = objects[("NetworkPolicy", "allow-egress")]
        egress = policy["spec"]["egress"]
        self.assertNotIn({}, egress)
        ports = {
            (port.get("protocol", "TCP"), port["port"])
            for rule in egress
            for port in rule.get("ports", [])
        }
        self.assertTrue({("UDP", 53), ("TCP", 53), ("TCP", 22), ("TCP", 443), ("TCP", 6443)} <= ports)
        self.assertTrue(
            any(
                target.get("namespaceSelector", {}).get("matchLabels", {}).get("kubernetes.io/metadata.name")
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
        self.assertIn("\tcd apps/api && DATABASE_URL=$(DATABASE_URL) uv run pytest", makefile)

    def test_actions_are_full_sha_pinned_and_dependency_updates_are_configured(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        uses = re.findall(r"^\s*(?:-\s*)?uses:\s*([^\s#]+)(?:\s+#\s*(.+))?$", workflow, re.MULTILINE)
        self.assertGreater(len(uses), 0)
        for action, comment in uses:
            self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$", action)
            self.assertRegex(comment or "", r"^v?\d", action)
        dependabot = document(".github/dependabot.yml")
        ecosystems = {item["package-ecosystem"] for item in dependabot["updates"]}
        self.assertIn("github-actions", ecosystems)

    def test_ci_builds_each_real_image_once_and_smokes_read_only_non_root(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"docker build[^\n]+apps/api/Dockerfile", workflow)), 1)
        self.assertEqual(len(re.findall(r"docker build[^\n]+apps/web/Dockerfile", workflow)), 1)
        self.assertIn("--file apps/api/Dockerfile apps/api", workflow)
        self.assertIn("--file apps/web/Dockerfile .", workflow)
        self.assertGreaterEqual(workflow.count("--read-only"), 2)
        self.assertIn("Config.User", workflow)
        self.assertIn("id -u", workflow)

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
    def test_m3_import_trust_boundary_is_explicit_and_fail_closed(self) -> None:
        runbook = (ROOT / "docs/gmail-import-operations.md").read_text(encoding="utf-8")
        for contract in (
            "PENDING_REVIEW",
            "technische Gmail-IDs",
            "keinen** Gmail-History-Sync",
            "keine** fachliche Manifest-Mutation",
            "https://www.googleapis.com/auth/gmail.readonly",
            "oauth2.googleapis.com:443",
            "gmail.googleapis.com:443",
            "BLOCKED_REQUIRES_EVIDENCE_ENRICHMENT",
            "0700",
            "0600",
        ):
            self.assertIn(contract, runbook)

    def test_runbook_matches_firewall_auth_and_live_gate_reality(self) -> None:
        runbook = (ROOT / "docs/gitops-m2.md").read_text(encoding="utf-8")
        self.assertIn("scripts/bootstrap-production-host.sh --verify-firewall", runbook)
        self.assertIn("Default: deny (incoming), allow (outgoing), deny (routed)", runbook)
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
        self.assertIn("Schema `coop-dashboard-network-policy-live-evidence-v2`", network_policy_compact)
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
        self.assertRegex(backup, r"Job-UID `[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}`")
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
        self.assertIn("Schema `coop-dashboard-controller-backup-evidence-v1`", backup_compact)
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

    def test_retained_pv_namespace_loss_runbook_is_fail_closed_and_identity_checked(self) -> None:
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

        pre_inventory = recovery.index("Unveränderlichen Checkpoint vor Suspension erstellen")
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
        self.assertEqual(data_volume["persistentVolumeClaim"]["claimName"], "postgres-data")
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
        self.assertEqual(syncs["coop-storage"]["spec"]["path"], "./infrastructure/storage")
        self.assertTrue(syncs["coop-storage"]["spec"]["prune"])
        self.assertEqual(syncs["coop-database"]["spec"]["dependsOn"], [{"name": "coop-storage"}])

        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        self.assertIn("python3 -m unittest scripts.tests.test_gitops_static", workflow)
        self.assertIn("scripts.tests.test_operational_scripts", workflow)
        render_loop = re.search(r"for dir in ([^;]+); do", workflow)
        if render_loop is None:
            self.fail("GitOps render loop is missing")
        self.assertIn("infrastructure/storage", render_loop.group(1))


if __name__ == "__main__":
    unittest.main()
