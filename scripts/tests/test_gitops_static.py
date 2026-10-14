from __future__ import annotations

import hashlib
import re
import subprocess
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
        migration_job = migration[("Job", "coop-migration-a4b4d9f030ee")]
        seed = migration[("CronJob", "coop-seed")]
        backup_cron = backup[("CronJob", "postgres-backup")]

        self.assertEqual(migration_job["metadata"]["labels"][selector], "true")
        for cronjob in (seed, backup_cron):
            self.assertEqual(cronjob["metadata"]["labels"][selector], "true")
            self.assertEqual(cronjob["spec"]["jobTemplate"]["metadata"]["labels"][selector], "true")

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
            ["python3", str(ROOT / "scripts/validate-rendered-images.py")],
            input=invalid,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("example.invalid/app:v1", result.stderr)


class DocumentationContractTests(unittest.TestCase):
    def test_runbook_matches_firewall_auth_and_live_gate_reality(self) -> None:
        runbook = (ROOT / "docs/gitops-m2.md").read_text(encoding="utf-8")
        self.assertIn("scripts/bootstrap-production-host.sh --verify-firewall", runbook)
        self.assertIn("Default: deny (incoming), allow (outgoing), deny (routed)", runbook)
        self.assertIn("API: `removeHeader: false`", runbook)
        self.assertIn("Web: `removeHeader: true`", runbook)
        self.assertIn("ausstehendes B2-Deployment-Gate", runbook)
        self.assertIn("SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU", runbook)

    def test_retained_pv_namespace_loss_runbook_is_fail_closed_and_identity_checked(self) -> None:
        runbook = (ROOT / "docs/gitops-m2.md").read_text(encoding="utf-8")
        required_guards = (
            "Namespaceverlust",
            "Released",
            "claimRef",
            "hostPath",
            "nodeAffinity",
            "read-only",
            "volumeName",
            "UID",
            "leere neue Volumes",
            "niemals vertauscht",
            "Backup-First",
            "Abbruch",
        )
        for guard in required_guards:
            self.assertIn(guard, runbook)
        for boundary in ("coop-root", "coop-database", "coop-apps", "coop-migration", "coop-backup"):
            self.assertIn(boundary, runbook)


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
