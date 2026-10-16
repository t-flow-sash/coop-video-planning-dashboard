from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

import yaml

from scripts import gmail_full_pilot as pilot

ROOT = Path(__file__).resolve().parents[2]
QUERY = (
    "in:anywhere -in:chats after:2026/01/01 "
    "{label:Legal to:legal@tsh-social.com from:legal@tsh-social.com}"
)
QUERY_HASH = "e362825c8b3be3c500d6da685c7e0b2043641fa2136fd801acf57395154f967d"
ACCOUNT_HASH = pilot.EXPECTED_ACCOUNT_SHA256
IMAGE = "127.0.0.1:5000/coop-api:git-123456789abc@sha256:" + "a" * 64
ADMISSION_DOCS = {
    (item["kind"].lower(), item["metadata"]["name"]): item
    for item in yaml.safe_load_all(
        subprocess.run(
            ["kubectl", "kustomize", str(ROOT / "infrastructure/import")],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        ).stdout
    )
    if item
    and item["kind"]
    in {"ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding"}
}


def authorize(_: pilot.PilotContract) -> int:
    return int(time.time()) + 3600


def cronjob() -> dict[str, object]:
    return {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {
            "name": "gmail-import-full",
            "namespace": "coop-prod",
            "annotations": {
                "coop.example.com/image-source-revision": "123456789abc0000000000000000000000000000"
            },
        },
        "spec": {
            "suspend": True,
            "concurrencyPolicy": "Forbid",
            "jobTemplate": {
                "spec": {
                    "activeDeadlineSeconds": 3600,
                    "backoffLimit": 0,
                    "template": {
                        "metadata": {
                            "labels": {
                                "app.kubernetes.io/name": "gmail-import-full",
                                "app.kubernetes.io/part-of": "coop-video-planning",
                                "coop.example.com/gmail-import": "true",
                                "coop.example.com/postgres-client": "true",
                            }
                        },
                        "spec": {
                            "restartPolicy": "Never",
                            "serviceAccountName": "gmail-full-pilot-runtime",
                            "automountServiceAccountToken": False,
                            "enableServiceLinks": False,
                            "securityContext": {
                                "runAsNonRoot": True,
                                "runAsUser": 10001,
                                "runAsGroup": 10001,
                                "fsGroup": 10001,
                                "seccompProfile": {"type": "RuntimeDefault"},
                            },
                            "containers": [
                                {
                                    "name": "gmail-import",
                                    "image": IMAGE,
                                    "imagePullPolicy": "IfNotPresent",
                                    "command": ["python", "-m", "app.ingestion.gmail"],
                                    "args": ["full"],
                                    "volumeMounts": [
                                        {"name": "tmp", "mountPath": "/tmp"}
                                    ],
                                    "securityContext": {
                                        "allowPrivilegeEscalation": False,
                                        "readOnlyRootFilesystem": True,
                                        "capabilities": {"drop": ["ALL"]},
                                    },
                                    "resources": {
                                        "requests": {"cpu": "50m", "memory": "128Mi"},
                                        "limits": {"cpu": "500m", "memory": "512Mi"},
                                    },
                                    "env": [
                                        {
                                            "name": "HTTPS_PROXY",
                                            "value": "http://gmail-egress-proxy:10000",
                                        },
                                        {
                                            "name": "HTTP_PROXY",
                                            "value": "http://gmail-egress-proxy:10000",
                                        },
                                        {
                                            "name": "NO_PROXY",
                                            "value": (
                                                "postgres,postgres.coop-prod.svc,"
                                                "gmail-egress-proxy,127.0.0.1,localhost"
                                            ),
                                        },
                                        {
                                            "name": "GMAIL_IMPORT_ARMED",
                                            "value": "false",
                                        },
                                        {"name": "GMAIL_IMPORT_QUERY", "value": QUERY},
                                        {
                                            "name": "GMAIL_IMPORT_QUERY_SHA256",
                                            "value": QUERY_HASH,
                                        },
                                        {
                                            "name": "GMAIL_ACCOUNT_SHA256",
                                            "value": ACCOUNT_HASH,
                                        },
                                        {
                                            "name": "GMAIL_CLIENT_ID",
                                            "valueFrom": {
                                                "secretKeyRef": {
                                                    "name": "gmail-oauth",
                                                    "key": "GMAIL_CLIENT_ID",
                                                }
                                            },
                                        },
                                        *[
                                            {
                                                "name": key,
                                                "valueFrom": {
                                                    "secretKeyRef": {
                                                        "name": "gmail-oauth",
                                                        "key": key,
                                                    }
                                                },
                                            }
                                            for key in (
                                                "GMAIL_CLIENT_SECRET",
                                                "GMAIL_REFRESH_TOKEN",
                                                "GMAIL_ACCOUNT",
                                            )
                                        ],
                                        {
                                            "name": "DATABASE_URL",
                                            "valueFrom": {
                                                "secretKeyRef": {
                                                    "name": "gmail-collector-db",
                                                    "key": "COLLECTOR_DATABASE_URL",
                                                }
                                            },
                                        },
                                    ],
                                }
                            ],
                            "volumes": [
                                {"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}}
                            ],
                        },
                    },
                }
            },
        },
    }


def ready_flux(
    revision: str = "main@sha1:123456789abc0000000000000000000000000000",
    *,
    name: str = "coop-root",
) -> dict[str, object]:
    common: dict[str, object] = {
        "decryption": {"provider": "sops", "secretRef": {"name": "sops-age"}},
        "force": False,
        "interval": "10m",
        "prune": True,
        "retryInterval": "1m",
        "sourceRef": {"kind": "GitRepository", "name": "coop-video-planning"},
    }
    specs: dict[str, dict[str, object]] = {
        "coop-root": {
            **common,
            "path": "./clusters/production",
            "timeout": "20m",
            "wait": False,
        },
        "coop-database": {
            **common,
            "dependsOn": [{"name": "coop-storage"}],
            "path": "./infrastructure/database",
            "timeout": "10m",
            "wait": True,
        },
        "coop-migration": {
            **common,
            "dependsOn": [{"name": "coop-database"}],
            "path": "./infrastructure/migration",
            "timeout": "15m",
            "wait": True,
        },
        "coop-import": {
            **common,
            "dependsOn": [{"name": "coop-database"}, {"name": "coop-migration"}],
            "path": "./infrastructure/import",
            "timeout": "10m",
            "wait": True,
        },
    }
    return {
        "metadata": {"name": name, "namespace": "flux-system", "generation": 3},
        "spec": specs[name],
        "status": {
            "observedGeneration": 3,
            "lastAppliedRevision": revision,
            "conditions": [{"type": "Ready", "status": "True"}],
        },
    }


def ready_flux_source() -> dict[str, object]:
    resource = ready_flux()
    resource["metadata"] = {
        "name": "coop-video-planning",
        "namespace": "flux-system",
        "generation": 3,
    }
    resource["spec"] = {
        "interval": "1m",
        "ref": {"branch": "main"},
        "secretRef": {"name": "coop-git-readonly"},
        "timeout": "60s",
        "url": "ssh://git@github.com/t-flow-sash/coop-video-planning-dashboard.git",
    }
    status = cast(dict[str, object], resource["status"])
    status["artifact"] = {
        "revision": status.pop("lastAppliedRevision"),  # type: ignore[union-attr]
        "digest": "sha256:" + "1" * 64,
    }
    return resource


def ready_proxy() -> dict[str, object]:
    documents = list(
        yaml.safe_load_all(
            (ROOT / "infrastructure/import/gmail-egress-proxy.yaml").read_text()
        )
    )
    deployment = copy.deepcopy(documents[1])
    deployment["metadata"]["generation"] = 2
    deployment["metadata"]["uid"] = "proxy-deployment-uid"
    deployment["status"] = {
        "observedGeneration": 2,
        "availableReplicas": 1,
        "updatedReplicas": 1,
        "readyReplicas": 1,
        "unavailableReplicas": 0,
    }
    return deployment


class FakeKubectl:
    def __init__(self) -> None:
        self.cron = cronjob()
        self.calls: list[tuple[object, ...]] = []
        self.created: dict[str, object] | None = None
        self.log_output = json.dumps(
            {"ok": True, "pending_review": 2, "status": "SUCCEEDED"},
            separators=(",", ":"),
        )

    def get_json(
        self,
        resource: str,
        name: str | None = None,
        *,
        namespace: str,
        labels: str | None = None,
    ) -> dict[str, object]:
        self.calls.append(("get_json", namespace, resource, name, labels))
        if resource == "configmap" and name == "gmail-full-pilot-admission-params":
            return {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": name,
                    "namespace": namespace,
                    "uid": "admission-params-uid",
                    "resourceVersion": "303",
                },
                "data": {
                    "image": IMAGE,
                    "imageSourceRevision": "123456789abc0000000000000000000000000000",
                    "query": QUERY,
                    "querySha256": QUERY_HASH,
                    "accountSha256": ACCOUNT_HASH,
                },
            }
        if (resource, name) in ADMISSION_DOCS:
            return copy.deepcopy(ADMISSION_DOCS[(resource, name)])
        if resource == "cronjob":
            return copy.deepcopy(self.cron)
        if resource == "kustomization":
            assert name is not None
            return ready_flux(name=name)
        if resource == "gitrepository":
            return ready_flux_source()
        if resource == "deployment":
            return ready_proxy()
        if resource == "replicasets":
            deployment = ready_proxy()
            return {
                "items": [
                    {
                        "metadata": {
                            "name": "gmail-egress-proxy-rs",
                            "uid": "proxy-rs-uid",
                            "ownerReferences": [
                                {
                                    "uid": "proxy-deployment-uid",
                                    "controller": True,
                                }
                            ],
                        },
                        "spec": {
                            "replicas": 1,
                            "template": copy.deepcopy(
                                deployment["spec"]["template"]  # type: ignore[index]
                            ),
                        },
                        "status": {
                            "replicas": 1,
                            "readyReplicas": 1,
                            "availableReplicas": 1,
                        },
                    }
                ]
            }
        if resource == "configmap":
            return next(
                iter(
                    yaml.safe_load_all(
                        (
                            ROOT / "infrastructure/import/gmail-egress-proxy.yaml"
                        ).read_text()
                    )
                )
            )
        if resource == "service":
            return {
                "spec": {
                    "type": "ClusterIP",
                    "selector": {"app.kubernetes.io/name": "gmail-egress-proxy"},
                    "ports": [
                        {
                            "name": "proxy",
                            "port": 10000,
                            "targetPort": "proxy",
                            "protocol": "TCP",
                        }
                    ],
                }
            }
        if resource == "endpoints":
            return {
                "subsets": [
                    {
                        "addresses": [
                            {
                                "ip": "10.0.0.8",
                                "targetRef": {
                                    "kind": "Pod",
                                    "namespace": "coop-prod",
                                    "name": "proxy-1",
                                    "uid": "proxy-uid-1",
                                },
                            }
                        ],
                        "ports": [{"name": "proxy", "port": 10000, "protocol": "TCP"}],
                    }
                ]
            }
        if resource == "networkpolicy":
            documents: list[dict[str, object]] = []
            for path in (
                ROOT / "infrastructure/database/network-policies.yaml",
                ROOT / "infrastructure/import/network-policy.yaml",
            ):
                documents.extend(yaml.safe_load_all(path.read_text()))
            return {"items": documents}
        if resource == "pods" and labels == "app.kubernetes.io/name=gmail-egress-proxy":
            deployment = ready_proxy()
            return {
                "items": [
                    {
                        "metadata": {
                            "name": "proxy-1",
                            "uid": "proxy-uid-1",
                            "labels": {"app.kubernetes.io/name": "gmail-egress-proxy"},
                            "annotations": {
                                "coop.example.com/proxy-config-sha256": pilot.PROXY_CONFIG_SHA256
                            },
                            "ownerReferences": [
                                {"uid": "proxy-rs-uid", "controller": True}
                            ],
                        },
                        "spec": copy.deepcopy(
                            deployment["spec"]["template"]["spec"]  # type: ignore[index]
                        ),
                        "status": {
                            "phase": "Running",
                            "podIP": "10.0.0.8",
                            "containerStatuses": [
                                {
                                    "ready": True,
                                    "restartCount": 0,
                                    "imageID": "docker-pullable://envoy@sha256:"
                                    + pilot.PROXY_IMAGE.split("@sha256:", 1)[1],
                                }
                            ],
                        },
                    }
                ]
            }
        if resource in {"jobs", "pods"}:
            return {"items": []}
        raise AssertionError((resource, name, namespace, labels))

    def secret_contract(
        self, name: str, *, namespace: str
    ) -> tuple[set[str], str, str]:
        self.calls.append(("secret_contract", namespace, name))
        if name == "gmail-oauth":
            return set(pilot.OAUTH_SECRET_KEYS), "oauth-uid", "101"
        if name == "gmail-collector-db":
            return ({"COLLECTOR_DATABASE_URL"}, "runtime-uid", "202")
        raise AssertionError(name)

    def create_json(self, manifest: dict[str, object], *, namespace: str) -> str:
        self.calls.append(("create_json", namespace))
        self.created = copy.deepcopy(manifest)
        return "job-uid-1"

    def activate_job(
        self,
        expected: dict[str, object],
        uid: str,
        expires_at: int,
        *,
        namespace: str,
    ) -> None:
        self.calls.append(("activate_job", namespace, uid))

    def consume_pilot_slot(
        self, contract: pilot.PilotContract, ownership_token: str, *, namespace: str
    ) -> str:
        self.calls.append(("consume_pilot_slot", namespace, ownership_token))
        return "marker-uid-1"

    def assert_pilot_snapshots_absent(self, *, namespace: str) -> None:
        self.calls.append(("assert_pilot_snapshots_absent", namespace))

    def assert_permissions(self, *, namespace: str) -> None:
        self.calls.append(("assert_permissions", namespace))

    def assert_pilot_marker(self, expected_uid: str | None, *, namespace: str) -> None:
        self.calls.append(("assert_pilot_marker", namespace, expected_uid))

    def get_job_optional(
        self, name: str, *, namespace: str
    ) -> dict[str, object] | None:
        self.calls.append(("get_job_optional", namespace, name))
        return None

    def delete_job(self, name: str, uid: str, *, namespace: str, timeout: str) -> None:
        self.calls.append(("delete_job", namespace, name, uid, timeout))

    def wait_complete(
        self, name: str, uid: str, *, namespace: str, timeout: str
    ) -> tuple[str, str]:
        self.calls.append(("wait_complete", namespace, name, uid, timeout))
        return "2026-10-15T20:00:00Z", "2026-10-15T20:01:00Z"

    def attest_single_pod(
        self,
        name: str,
        uid: str,
        image: str,
        expected_job: dict[str, object],
        *,
        namespace: str,
    ) -> tuple[str, str]:
        self.calls.append(("attest_single_pod", namespace, name, uid, image))
        return "pilot-pod-1", "pod-uid-1"

    def logs(self, pod_name: str, pod_uid: str, job_uid: str, *, namespace: str) -> str:
        self.calls.append(("logs", namespace, pod_name, pod_uid, job_uid))
        return self.log_output


class PilotContractTests(unittest.TestCase):
    def test_permission_preflight_uses_resource_names_for_scoped_gets(self) -> None:
        kubectl = pilot.Kubectl("production-context")
        calls: list[tuple[tuple[str, ...], str]] = []
        forbidden = {
            ("create", "secrets"),
            ("update", "secrets"),
            ("patch", "secrets"),
            ("delete", "secrets"),
            ("create", "pods"),
            ("delete", "pods"),
            ("patch", "jobs.batch"),
            ("update", "cronjobs.batch"),
            ("patch", "cronjobs.batch"),
            ("delete", "cronjobs.batch"),
            ("update", "deployments.apps"),
            ("patch", "deployments.apps"),
            ("delete", "deployments.apps"),
            ("create", "networkpolicies.networking.k8s.io"),
            ("update", "networkpolicies.networking.k8s.io"),
            ("patch", "networkpolicies.networking.k8s.io"),
            ("delete", "networkpolicies.networking.k8s.io"),
            ("patch", "configmaps"),
            ("update", "configmaps"),
            ("delete", "configmaps"),
        }

        def auth_can_i(command: list[str], *, namespace: str, operation: str) -> str:
            self.assertEqual(operation, "permission_check")
            calls.append((tuple(command), namespace))
            verb, resource = command[2], command[3]
            base_resource = resource.split("/", 1)[0]
            if (verb, base_resource) in forbidden:
                return "no\n"
            if command == [
                "auth",
                "can-i",
                "delete",
                "configmap",
                pilot.PILOT_MARKER_NAME,
            ]:
                return "no\n"
            return "yes\n"

        with patch.object(kubectl, "_run", side_effect=auth_can_i):
            kubectl.assert_permissions(namespace="coop-prod")

        checked_resources = {command[3] for command, _namespace in calls}
        self.assertIn(
            (
                ("auth", "can-i", "list", "networkpolicies.networking.k8s.io"),
                "coop-prod",
            ),
            calls,
        )
        for expected in (
            "cronjobs.batch/gmail-import-full",
            "deployments.apps/gmail-egress-proxy",
            "services/gmail-egress-proxy",
            "endpoints/gmail-egress-proxy",
            "configmaps/gmail-egress-proxy-config",
            f"configmaps/{pilot.PILOT_MARKER_NAME}",
            "configmaps/gmail-full-pilot-admission-params",
            "secrets/gmail-oauth",
            "secrets/gmail-collector-db",
            "kustomizations.kustomize.toolkit.fluxcd.io/coop-root",
            "gitrepositories.source.toolkit.fluxcd.io/coop-video-planning",
            "validatingadmissionpolicies.admissionregistration.k8s.io/gmail-full-pilot-operator",
            "validatingadmissionpolicybindings.admissionregistration.k8s.io/gmail-full-pilot-slot",
        ):
            self.assertIn(expected, checked_resources)

    def test_live_attestation_is_digest_bound_short_lived_and_exact(self) -> None:
        contract = pilot.replace(
            pilot.validate_cronjob(cronjob()),
            flux_revision="main@sha1:123456789abc0000000000000000000000000000",
            oauth_secret_uid="oauth-uid",
            oauth_secret_version="101",
            runtime_secret_uid="runtime-uid",
            runtime_secret_version="202",
            admission_params_uid="admission-params-uid",
            admission_params_version="303",
        )
        now = int(time.time())
        value = {
            "schema": "coop.gmail-full-live-attestation/v1",
            "context": "production-context",
            "namespace": "coop-prod",
            "image": contract.image,
            "image_source_revision": contract.image_source_revision,
            "query_hash": contract.query_hash,
            "account_hash": pilot.EXPECTED_ACCOUNT_SHA256,
            "admission_params_uid": contract.admission_params_uid,
            "admission_params_version": contract.admission_params_version,
            "source_enabled": True,
            "collector_login": True,
            "collector_business_write_denied": True,
            "network_allow": True,
            "network_deny": True,
            "proxy_allow": True,
            "proxy_deny": True,
            "issued_at": now,
            "expires_at": now + 1800,
            "flux_revision": contract.flux_revision,
            "oauth_secret_uid": contract.oauth_secret_uid,
            "oauth_secret_version": contract.oauth_secret_version,
            "runtime_secret_uid": contract.runtime_secret_uid,
            "runtime_secret_version": contract.runtime_secret_version,
            "approved_by": "operator@example.invalid",
            "evidence_id": "change-123",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "attestation.json"
            raw = json.dumps(value, sort_keys=True).encode()
            path.write_bytes(raw)
            path.chmod(0o600)
            digest = hashlib.sha256(raw).hexdigest()
            pilot._validate_live_attestation(
                str(path),
                digest,
                context="production-context",
                namespace="coop-prod",
                contract=contract,
            )
            value["network_deny"] = False
            raw = json.dumps(value, sort_keys=True).encode()
            path.write_bytes(raw)
            with self.assertRaisesRegex(pilot.PilotError, "live_attestation_invalid"):
                pilot._validate_live_attestation(
                    str(path),
                    hashlib.sha256(raw).hexdigest(),
                    context="production-context",
                    namespace="coop-prod",
                    contract=contract,
                )

    def test_real_kustomize_render_matches_the_live_pilot_contract(self) -> None:
        rendered = subprocess.run(
            ["kubectl", "kustomize", "infrastructure/import"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        cronjobs = [
            document
            for document in yaml.safe_load_all(rendered)
            if isinstance(document, dict)
            and document.get("kind") == "CronJob"
            and document.get("metadata", {}).get("name") == pilot.CRONJOB_NAME
        ]
        self.assertEqual(len(cronjobs), 1)
        rendered_cronjob = cronjobs[0]
        rendered_cronjob["metadata"]["annotations"][
            "coop.example.com/image-source-revision"
        ] = "123456789abc0000000000000000000000000000"
        rendered_cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"][
            "containers"
        ][0]["image"] = IMAGE
        for item in rendered_cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"][
            "containers"
        ][0]["env"]:
            if item["name"] == "GMAIL_ACCOUNT_SHA256":
                item["value"] = ACCOUNT_HASH
        contract = pilot.validate_cronjob(rendered_cronjob)
        self.assertEqual(contract.query_hash, QUERY_HASH)

    def test_cronjob_contract_is_suspended_unarmed_digest_and_query_pinned(
        self,
    ) -> None:
        contract = pilot.validate_cronjob(cronjob())
        self.assertEqual(contract.query_hash, QUERY_HASH)
        self.assertEqual(contract.image, IMAGE)

    def test_cronjob_contract_rejects_every_activation_or_integrity_drift(self) -> None:
        mutations = []
        unsuspended = cronjob()
        unsuspended["spec"]["suspend"] = False  # type: ignore[index]
        mutations.append(unsuspended)
        armed = cronjob()
        armed_env = armed["spec"]["jobTemplate"]["spec"]["template"]["spec"][
            "containers"
        ][0]["env"]  # type: ignore[index]
        next(item for item in armed_env if item["name"] == "GMAIL_IMPORT_ARMED")[
            "value"
        ] = "true"
        mutations.append(armed)
        tampered = cronjob()
        tampered_env = tampered["spec"]["jobTemplate"]["spec"]["template"]["spec"][
            "containers"
        ][0]["env"]  # type: ignore[index]
        next(item for item in tampered_env if item["name"] == "GMAIL_IMPORT_QUERY")[
            "value"
        ] = "tampered"
        mutations.append(tampered)
        sentinel = cronjob()
        sentinel["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0][  # type: ignore[index]
            "image"
        ] = "127.0.0.1:5000/coop-api:git-000000000000@sha256:" + "0" * 64
        mutations.append(sentinel)
        admin_credentials = cronjob()
        admin_env = admin_credentials["spec"]["jobTemplate"]["spec"]["template"][
            "spec"
        ][  # type: ignore[index]
            "containers"
        ][0]["env"]
        next(item for item in admin_env if item["name"] == "DATABASE_URL")["valueFrom"][  # type: ignore[index]
            "secretKeyRef"
        ]["key"] = "DATABASE_URL"
        mutations.append(admin_credentials)
        parallel = cronjob()
        parallel["spec"]["jobTemplate"]["spec"]["parallelism"] = 3  # type: ignore[index]
        parallel["spec"]["jobTemplate"]["spec"]["completions"] = 3  # type: ignore[index]
        mutations.append(parallel)

        for manifest in mutations:
            with self.subTest(manifest=manifest), self.assertRaises(pilot.PilotError):
                pilot.validate_cronjob(manifest)

    def test_cronjob_contract_rejects_pod_security_and_egress_drift(self) -> None:
        missing_label = cronjob()
        del missing_label["spec"]["jobTemplate"]["spec"]["template"]["metadata"][
            "labels"
        ]["coop.example.com/gmail-import"]
        with self.assertRaises(pilot.PilotError):
            pilot.validate_cronjob(missing_label)

        service_account_token = cronjob()
        service_account_token["spec"]["jobTemplate"]["spec"]["template"]["spec"][
            "automountServiceAccountToken"
        ] = True
        with self.assertRaises(pilot.PilotError):
            pilot.validate_cronjob(service_account_token)

        privileged = cronjob()
        privileged["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0][
            "securityContext"
        ]["allowPrivilegeEscalation"] = True
        with self.assertRaises(pilot.PilotError):
            pilot.validate_cronjob(privileged)

        proxy_drift = cronjob()
        proxy_env = proxy_drift["spec"]["jobTemplate"]["spec"]["template"]["spec"][
            "containers"
        ][0]["env"]
        next(item for item in proxy_env if item["name"] == "HTTPS_PROXY")["value"] = (
            "http://untrusted-proxy:10000"
        )
        with self.assertRaises(pilot.PilotError):
            pilot.validate_cronjob(proxy_drift)

        host_network = cronjob()
        host_network["spec"]["jobTemplate"]["spec"]["template"]["spec"][  # type: ignore[index]
            "hostNetwork"
        ] = True
        with self.assertRaises(pilot.PilotError):
            pilot.validate_cronjob(host_network)

        init_container = cronjob()
        init_container["spec"]["jobTemplate"]["spec"]["template"]["spec"][  # type: ignore[index]
            "initContainers"
        ] = [{"name": "bypass", "image": IMAGE}]
        with self.assertRaises(pilot.PilotError):
            pilot.validate_cronjob(init_container)

        extra_env = cronjob()
        extra_env["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0][  # type: ignore[index]
            "env"
        ].append({"name": "UNEXPECTED_SECRET", "value": "not-allowed"})
        with self.assertRaises(pilot.PilotError):
            pilot.validate_cronjob(extra_env)

        self_consistent_query_drift = cronjob()
        drift_container = self_consistent_query_drift["spec"]["jobTemplate"]["spec"][  # type: ignore[index]
            "template"
        ]["spec"]["containers"][0]
        drift_env = pilot._environment(drift_container)
        drift_env["GMAIL_IMPORT_QUERY"]["value"] = "in:anywhere changed"
        drift_env["GMAIL_IMPORT_QUERY_SHA256"]["value"] = pilot._canonical_hash(
            "in:anywhere changed"
        )
        with self.assertRaisesRegex(pilot.PilotError, "cronjob_query_contract_invalid"):
            pilot.validate_cronjob(self_consistent_query_drift)

    def test_cronjob_rejects_retry_and_identity_extension_fields(self) -> None:
        for field, value in (
            (
                "podFailurePolicy",
                {
                    "rules": [
                        {
                            "action": "Ignore",
                            "onExitCodes": {"operator": "In", "values": [1]},
                        }
                    ]
                },
            ),
            ("backoffLimitPerIndex", 1),
            ("completionMode", "Indexed"),
            ("successPolicy", {"rules": [{"succeededCount": 1}]}),
        ):
            unsafe = cronjob()
            unsafe["spec"]["jobTemplate"]["spec"][field] = value  # type: ignore[index]
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(
                    pilot.PilotError, "cronjob_retry_contract_invalid"
                ),
            ):
                pilot.validate_cronjob(unsafe)
        for field, value in (
            ("serviceAccountName", "privileged"),
            ("imagePullSecrets", [{"name": "foreign"}]),
            ("hostNetwork", True),
        ):
            unsafe = cronjob()
            unsafe["spec"]["jobTemplate"]["spec"]["template"]["spec"][field] = value  # type: ignore[index]
            with self.subTest(field=field), self.assertRaises(pilot.PilotError):
                pilot.validate_cronjob(unsafe)

    def test_pilot_job_is_one_shot_and_only_job_copy_is_armed(self) -> None:
        source = cronjob()
        job = pilot.build_pilot_job(source, "gmail-import-full-pilot", "f" * 64)

        self.assertEqual(job["kind"], "Job")
        self.assertEqual(job["metadata"]["name"], "gmail-import-full-pilot")  # type: ignore[index]
        self.assertEqual(job["metadata"]["labels"][pilot.PILOT_LABEL], "true")  # type: ignore[index]
        self.assertEqual(job["spec"]["backoffLimit"], 0)  # type: ignore[index]
        self.assertEqual(job["spec"]["parallelism"], 1)  # type: ignore[index]
        self.assertEqual(job["spec"]["completions"], 1)  # type: ignore[index]
        self.assertIs(job["spec"]["suspend"], True)  # type: ignore[index]
        env = {
            item["name"]: item
            for item in job["spec"]["template"]["spec"]["containers"][0]["env"]  # type: ignore[index]
        }
        self.assertEqual(env["GMAIL_IMPORT_ARMED"]["value"], "true")
        source_env = {
            item["name"]: item
            for item in source["spec"]["jobTemplate"]["spec"]["template"]["spec"][  # type: ignore[index]
                "containers"
            ][0]["env"]
        }
        self.assertEqual(source_env["GMAIL_IMPORT_ARMED"]["value"], "false")

        snapshot_job = pilot.build_pilot_job(
            source,
            pilot.PILOT_JOB_NAME,
            "e" * 64,
            oauth_secret_name=pilot.PILOT_OAUTH_SECRET_NAME,
            runtime_secret_name=pilot.PILOT_RUNTIME_SECRET_NAME,
        )
        snapshot_env = pilot._environment(
            snapshot_job["spec"]["template"]["spec"]["containers"][0]  # type: ignore[index]
        )
        for key in pilot.OAUTH_SECRET_KEYS:
            self.assertEqual(
                snapshot_env[key]["valueFrom"]["secretKeyRef"]["name"],  # type: ignore[index]
                pilot.PILOT_OAUTH_SECRET_NAME,
            )
        self.assertEqual(
            snapshot_env["DATABASE_URL"]["valueFrom"]["secretKeyRef"]["name"],  # type: ignore[index]
            pilot.PILOT_RUNTIME_SECRET_NAME,
        )

    def test_activation_retries_only_definitive_resource_version_conflicts(
        self,
    ) -> None:
        kubectl = pilot.Kubectl("production-context")
        expected = pilot.build_pilot_job(cronjob(), pilot.PILOT_JOB_NAME, "e" * 64)
        persisted = copy.deepcopy(expected)
        persisted["metadata"]["uid"] = "job-uid-1"  # type: ignore[index]
        persisted["metadata"]["resourceVersion"] = "1"  # type: ignore[index]
        activated = copy.deepcopy(persisted)
        activated["metadata"]["resourceVersion"] = "2"  # type: ignore[index]
        activated["spec"]["suspend"] = False  # type: ignore[index]
        with (
            patch.object(
                kubectl, "get_json", side_effect=[persisted, persisted]
            ) as get,
            patch.object(
                kubectl,
                "_run",
                side_effect=[
                    pilot.PilotUpdateConflict("pilot_job_activation_conflict"),
                    json.dumps(activated),
                ],
            ) as run,
            patch.object(pilot.time, "sleep"),
        ):
            kubectl.activate_job(
                expected,
                "job-uid-1",
                int(pilot.time.time()) + 60,
                namespace="coop-prod",
            )
        self.assertEqual(get.call_count, 2)
        self.assertEqual(run.call_count, 2)

    def test_persisted_job_rejects_proxy_egress_label(self) -> None:
        expected = pilot.build_pilot_job(cronjob(), pilot.PILOT_JOB_NAME, "e" * 64)
        actual = copy.deepcopy(expected)
        pilot._validate_persisted_pilot_job(actual, expected)
        actual["spec"]["template"]["metadata"]["labels"][  # type: ignore[index]
            "app.kubernetes.io/name"
        ] = "gmail-egress-proxy"
        with self.assertRaisesRegex(pilot.PilotError, "pilot_job_contract_invalid"):
            pilot._validate_persisted_pilot_job(actual, expected)

    def test_preflight_is_read_only_and_checks_live_dependencies(self) -> None:
        kubectl = FakeKubectl()
        contract = pilot.run_preflight(kubectl, namespace="coop-prod")

        self.assertEqual(contract.query_hash, QUERY_HASH)
        self.assertFalse(
            any(
                call[0] in {"create_json", "wait_complete", "logs"}
                for call in kubectl.calls
            )
        )
        self.assertIn(("secret_contract", "coop-prod", "gmail-oauth"), kubectl.calls)
        self.assertIn(
            ("secret_contract", "coop-prod", "gmail-collector-db"), kubectl.calls
        )

    def test_preflight_rejects_incomplete_secret_keysets_and_active_jobs(self) -> None:
        class UnsafeKubectl(FakeKubectl):
            def secret_contract(
                self, name: str, *, namespace: str
            ) -> tuple[set[str], str, str]:
                if name == "gmail-oauth":
                    return {"GMAIL_CLIENT_ID"}, "oauth-uid", "101"
                return super().secret_contract(name, namespace=namespace)

        with self.assertRaises(pilot.PilotError):
            pilot.run_preflight(UnsafeKubectl(), namespace="coop-prod")

        class BusyKubectl(FakeKubectl):
            def get_json(
                self,
                resource: str,
                name: str | None = None,
                *,
                namespace: str,
                labels: str | None = None,
            ) -> dict[str, object]:
                if resource == "jobs":
                    return {
                        "items": [
                            {"metadata": {"name": "running"}, "status": {"active": 1}}
                        ]
                    }
                return super().get_json(
                    resource, name, namespace=namespace, labels=labels
                )

        with self.assertRaises(pilot.PilotError):
            pilot.run_preflight(BusyKubectl(), namespace="coop-prod")

    def test_preflight_rejects_orphan_pod_with_reserved_job_name(self) -> None:
        class OrphanPodKubectl(FakeKubectl):
            def get_json(
                self,
                resource: str,
                name: str | None = None,
                *,
                namespace: str,
                labels: str | None = None,
            ) -> dict[str, object]:
                if resource == "pods" and labels == f"job-name={pilot.PILOT_JOB_NAME}":
                    return {"items": [{"metadata": {"name": "orphan"}}]}
                return super().get_json(
                    resource, name, namespace=namespace, labels=labels
                )

        with self.assertRaisesRegex(pilot.PilotError, "pilot_pod_already_exists"):
            pilot.run_preflight(OrphanPodKubectl(), namespace="coop-prod")

    def test_preflight_binds_flux_source_and_policy_semantics(self) -> None:
        class RevisionDriftKubectl(FakeKubectl):
            def get_json(
                self,
                resource: str,
                name: str | None = None,
                *,
                namespace: str,
                labels: str | None = None,
            ) -> dict[str, object]:
                if resource == "kustomization" and name == "coop-import":
                    return ready_flux(
                        "main@sha1:abcdefabcdefabcdefabcdefabcdefabcdefabcd",
                        name="coop-import",
                    )
                return super().get_json(
                    resource, name, namespace=namespace, labels=labels
                )

        with self.assertRaisesRegex(pilot.PilotError, "flux_revision_mismatch"):
            pilot.run_preflight(RevisionDriftKubectl(), namespace="coop-prod")

        class PolicyDriftKubectl(FakeKubectl):
            def get_json(
                self,
                resource: str,
                name: str | None = None,
                *,
                namespace: str,
                labels: str | None = None,
            ) -> dict[str, object]:
                value = super().get_json(
                    resource, name, namespace=namespace, labels=labels
                )
                if resource == "networkpolicy":
                    value["items"][0]["spec"]["egress"] = []  # type: ignore[index]
                return value

        with self.assertRaisesRegex(
            pilot.PilotError, "network_policy_contract_invalid"
        ):
            pilot.run_preflight(PolicyDriftKubectl(), namespace="coop-prod")

        class AdditivePolicyKubectl(FakeKubectl):
            def get_json(
                self,
                resource: str,
                name: str | None = None,
                *,
                namespace: str,
                labels: str | None = None,
            ) -> dict[str, object]:
                value = super().get_json(
                    resource, name, namespace=namespace, labels=labels
                )
                if resource == "networkpolicy":
                    items = cast(list[dict[str, object]], value["items"])
                    items.append(
                        {
                            "metadata": {"name": "allow-all-egress"},
                            "spec": {
                                "podSelector": {},
                                "policyTypes": ["Egress"],
                                "egress": [{}],
                            },
                        }
                    )
                return value

        with self.assertRaisesRegex(
            pilot.PilotError, "network_policy_contract_invalid"
        ):
            pilot.run_preflight(AdditivePolicyKubectl(), namespace="coop-prod")

        class FluxSpecDriftKubectl(FakeKubectl):
            def get_json(
                self,
                resource: str,
                name: str | None = None,
                *,
                namespace: str,
                labels: str | None = None,
            ) -> dict[str, object]:
                value = super().get_json(
                    resource, name, namespace=namespace, labels=labels
                )
                if resource == "kustomization" and name == "coop-import":
                    spec = cast(dict[str, object], value["spec"])
                    spec["path"] = "./unrelated"
                return value

        with self.assertRaisesRegex(pilot.PilotError, "flux_spec_invalid"):
            pilot.run_preflight(FluxSpecDriftKubectl(), namespace="coop-prod")

        class EndpointDriftKubectl(FakeKubectl):
            def get_json(
                self,
                resource: str,
                name: str | None = None,
                *,
                namespace: str,
                labels: str | None = None,
            ) -> dict[str, object]:
                if resource == "endpoints":
                    return {"subsets": []}
                return super().get_json(
                    resource, name, namespace=namespace, labels=labels
                )

        with self.assertRaisesRegex(pilot.PilotError, "proxy_endpoints_not_ready"):
            pilot.run_preflight(EndpointDriftKubectl(), namespace="coop-prod")

        class EndpointIpDriftKubectl(FakeKubectl):
            def get_json(
                self,
                resource: str,
                name: str | None = None,
                *,
                namespace: str,
                labels: str | None = None,
            ) -> dict[str, object]:
                value = super().get_json(
                    resource, name, namespace=namespace, labels=labels
                )
                if resource == "endpoints":
                    subsets = cast(list[dict[str, object]], value["subsets"])
                    addresses = cast(list[dict[str, object]], subsets[0]["addresses"])
                    addresses[0]["ip"] = "203.0.113.77"
                return value

        with self.assertRaisesRegex(pilot.PilotError, "proxy_endpoints_not_ready"):
            pilot.run_preflight(EndpointIpDriftKubectl(), namespace="coop-prod")

        class ProxyConfigIdentityDriftKubectl(FakeKubectl):
            def get_json(
                self,
                resource: str,
                name: str | None = None,
                *,
                namespace: str,
                labels: str | None = None,
            ) -> dict[str, object]:
                value = super().get_json(
                    resource, name, namespace=namespace, labels=labels
                )
                if resource == "deployment":
                    template = cast(
                        dict[str, object],
                        cast(dict[str, object], value["spec"])["template"],
                    )
                    metadata = cast(dict[str, object], template["metadata"])
                    metadata["annotations"] = {
                        "coop.example.com/proxy-config-sha256": "0" * 64
                    }
                return value

        with self.assertRaisesRegex(pilot.PilotError, "proxy_invalid"):
            pilot.run_preflight(
                ProxyConfigIdentityDriftKubectl(), namespace="coop-prod"
            )

    def test_run_creates_exactly_one_job_and_rechecks_dormant_cronjob(self) -> None:
        kubectl = FakeKubectl()
        report = pilot.run_pilot(
            kubectl,
            namespace="coop-prod",
            job_name="gmail-import-full-pilot",
            authorize=authorize,
            timeout="70m",
        )

        self.assertIsNotNone(kubectl.created)
        self.assertEqual(report["status"], "SUCCEEDED")
        self.assertEqual(report["pending_review"], 2)
        self.assertEqual(report["query_hash"], QUERY_HASH)
        self.assertEqual(report["job_uid"], "job-uid-1")
        self.assertEqual(report["pod"], "pilot-pod-1")
        self.assertEqual(sum(call[0] == "create_json" for call in kubectl.calls), 1)
        self.assertEqual(
            sum(call[0] == "consume_pilot_slot" for call in kubectl.calls), 1
        )
        self.assertIn(
            ("delete_job", "coop-prod", pilot.PILOT_JOB_NAME, "job-uid-1", "5m"),
            kubectl.calls,
        )
        self.assertEqual(
            sum(
                call[0] == "get_json" and call[2] == "cronjob" for call in kubectl.calls
            ),
            4,
        )

    def test_run_cleans_up_after_failure_and_always_rechecks_source(self) -> None:
        class FailedWaitKubectl(FakeKubectl):
            def wait_complete(
                self, name: str, uid: str, *, namespace: str, timeout: str
            ) -> tuple[str, str]:
                super().wait_complete(name, uid, namespace=namespace, timeout=timeout)
                raise pilot.PilotError("kubectl_wait_failed")

        kubectl = FailedWaitKubectl()
        with self.assertRaisesRegex(pilot.PilotError, "kubectl_wait_failed"):
            pilot.run_pilot(
                kubectl,
                namespace="coop-prod",
                job_name=pilot.PILOT_JOB_NAME,
                authorize=authorize,
                timeout="70m",
            )
        self.assertIn(
            ("delete_job", "coop-prod", pilot.PILOT_JOB_NAME, "job-uid-1", "5m"),
            kubectl.calls,
        )
        self.assertEqual(
            sum(
                call[0] == "get_json" and call[2] == "cronjob" for call in kubectl.calls
            ),
            4,
        )

    def test_uncertain_create_never_deletes_unacknowledged_uid(self) -> None:
        class UncertainCreateKubectl(FakeKubectl):
            def create_json(
                self, manifest: dict[str, object], *, namespace: str
            ) -> str:
                super().create_json(manifest, namespace=namespace)
                raise pilot.PilotCreateUncertain("kubectl_create_uncertain")

            def get_job_optional(
                self, name: str, *, namespace: str
            ) -> dict[str, object] | None:
                assert self.created is not None
                observed = copy.deepcopy(self.created)
                observed["metadata"]["uid"] = "job-uid-1"  # type: ignore[index]
                return observed

        kubectl = UncertainCreateKubectl()
        with self.assertRaisesRegex(pilot.PilotError, "pilot_job_uncertain"):
            pilot.run_pilot(
                kubectl,
                namespace="coop-prod",
                job_name=pilot.PILOT_JOB_NAME,
                authorize=authorize,
                timeout="70m",
            )
        self.assertFalse(any(call[0] == "delete_job" for call in kubectl.calls))

    def test_uncertain_create_waits_for_delayed_owned_readback_without_retry(
        self,
    ) -> None:
        class DelayedReadbackKubectl(FakeKubectl):
            reads = 0

            def create_json(
                self, manifest: dict[str, object], *, namespace: str
            ) -> str:
                super().create_json(manifest, namespace=namespace)
                raise pilot.PilotCreateUncertain("kubectl_create_uncertain")

            def get_job_optional(
                self, name: str, *, namespace: str
            ) -> dict[str, object] | None:
                self.reads += 1
                if self.reads < 3:
                    return None
                assert self.created is not None
                observed = copy.deepcopy(self.created)
                observed["metadata"]["uid"] = "job-uid-delayed"  # type: ignore[index]
                return observed

        kubectl = DelayedReadbackKubectl()
        with (
            patch.object(pilot.time, "sleep", return_value=None),
            self.assertRaisesRegex(pilot.PilotError, "pilot_job_uncertain"),
        ):
            pilot.run_pilot(
                kubectl,
                namespace="coop-prod",
                job_name=pilot.PILOT_JOB_NAME,
                authorize=authorize,
                timeout="70m",
            )
        self.assertEqual(sum(call[0] == "create_json" for call in kubectl.calls), 1)
        self.assertFalse(any(call[0] == "delete_job" for call in kubectl.calls))

    def test_already_exists_never_deletes_foreign_job(self) -> None:
        class AlreadyExistsKubectl(FakeKubectl):
            def create_json(
                self, manifest: dict[str, object], *, namespace: str
            ) -> str:
                raise pilot.PilotJobAlreadyExists("pilot_job_already_exists")

        kubectl = AlreadyExistsKubectl()
        with self.assertRaisesRegex(pilot.PilotError, "pilot_job_already_exists"):
            pilot.run_pilot(
                kubectl,
                namespace="coop-prod",
                job_name=pilot.PILOT_JOB_NAME,
                authorize=authorize,
                timeout="70m",
            )
        self.assertFalse(any(call[0] == "delete_job" for call in kubectl.calls))

    def test_expired_attestation_never_reaches_create(self) -> None:
        kubectl = FakeKubectl()
        with (
            patch.object(pilot.time, "time", side_effect=[1000, 1200]),
            self.assertRaisesRegex(pilot.PilotError, "live_attestation_expired"),
        ):
            pilot.run_pilot(
                kubectl,
                namespace="coop-prod",
                job_name=pilot.PILOT_JOB_NAME,
                authorize=lambda contract: 1100,
                timeout="70m",
            )
        self.assertFalse(any(call[0] == "create_json" for call in kubectl.calls))

    def test_attestation_expiry_during_wait_never_publishes_success(self) -> None:
        clock = {"now": 1000}

        class ExpiringWaitKubectl(FakeKubectl):
            def wait_complete(
                self, name: str, uid: str, *, namespace: str, timeout: str
            ) -> tuple[str, str]:
                result = super().wait_complete(
                    name, uid, namespace=namespace, timeout=timeout
                )
                clock["now"] = 1200
                return result

        kubectl = ExpiringWaitKubectl()
        with (
            patch.object(pilot.time, "time", side_effect=lambda: clock["now"]),
            self.assertRaisesRegex(pilot.PilotError, "live_attestation_expired"),
        ):
            pilot.run_pilot(
                kubectl,
                namespace="coop-prod",
                job_name=pilot.PILOT_JOB_NAME,
                authorize=lambda contract: 1100,
                timeout="70m",
            )
        self.assertFalse(any(call[0] == "logs" for call in kubectl.calls))
        self.assertTrue(any(call[0] == "delete_job" for call in kubectl.calls))

    def test_uncertain_create_never_deletes_foreign_owner(self) -> None:
        class ForeignOwnerKubectl(FakeKubectl):
            def create_json(
                self, manifest: dict[str, object], *, namespace: str
            ) -> str:
                raise pilot.PilotCreateUncertain("kubectl_create_uncertain")

            def get_job_optional(
                self, name: str, *, namespace: str
            ) -> dict[str, object] | None:
                return {
                    "metadata": {
                        "name": name,
                        "uid": "foreign-uid",
                        "annotations": {
                            "coop.example.com/pilot-ownership-token": "0" * 64
                        },
                    }
                }

        kubectl = ForeignOwnerKubectl()
        with self.assertRaisesRegex(pilot.PilotError, "pilot_job_uncertain"):
            pilot.run_pilot(
                kubectl,
                namespace="coop-prod",
                job_name=pilot.PILOT_JOB_NAME,
                authorize=authorize,
                timeout="70m",
            )
        self.assertFalse(any(call[0] == "delete_job" for call in kubectl.calls))

    def test_uncertain_create_lookup_failure_is_named_uncertain(self) -> None:
        class LookupFailureKubectl(FakeKubectl):
            def create_json(
                self, manifest: dict[str, object], *, namespace: str
            ) -> str:
                raise pilot.PilotCreateUncertain("kubectl_create_uncertain")

            def get_job_optional(
                self, name: str, *, namespace: str
            ) -> dict[str, object] | None:
                raise pilot.PilotError("kubectl_get_optional_failed")

        with self.assertRaisesRegex(
            pilot.PilotError, f"pilot_job_uncertain:{pilot.PILOT_JOB_NAME}"
        ):
            pilot.run_pilot(
                LookupFailureKubectl(),
                namespace="coop-prod",
                job_name=pilot.PILOT_JOB_NAME,
                authorize=authorize,
                timeout="70m",
            )

    def test_consumed_marker_is_permanent_atomic_slot(self) -> None:
        kubectl = pilot.Kubectl("production-context")
        contract = pilot.replace(
            pilot.validate_cronjob(cronjob()),
            flux_revision="main@sha1:123456789abc0000000000000000000000000000",
        )
        completed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="AlreadyExists"
        )
        with (
            patch("subprocess.run", return_value=completed),
            self.assertRaisesRegex(pilot.PilotError, "pilot_slot_already_consumed"),
        ):
            kubectl.consume_pilot_slot(contract, "f" * 64, namespace="coop-prod")

    def test_run_rejects_dependency_drift_after_creation(self) -> None:
        class PolicyDriftKubectl(FakeKubectl):
            policy_reads = 0

            def get_json(
                self,
                resource: str,
                name: str | None = None,
                *,
                namespace: str,
                labels: str | None = None,
            ) -> dict[str, object]:
                value = super().get_json(
                    resource, name, namespace=namespace, labels=labels
                )
                if resource == "networkpolicy":
                    self.policy_reads += 1
                    if self.policy_reads == 2:
                        value["items"][0]["spec"]["egress"] = []  # type: ignore[index]
                return value

        with self.assertRaisesRegex(
            pilot.PilotError, "network_policy_contract_invalid"
        ):
            pilot.run_pilot(
                PolicyDriftKubectl(),
                namespace="coop-prod",
                job_name=pilot.PILOT_JOB_NAME,
                authorize=authorize,
                timeout="70m",
            )

    def test_run_rejects_any_source_drift_after_creation(self) -> None:
        class DriftKubectl(FakeKubectl):
            cron_reads = 0

            def get_json(
                self,
                resource: str,
                name: str | None = None,
                *,
                namespace: str,
                labels: str | None = None,
            ) -> dict[str, object]:
                value = super().get_json(
                    resource, name, namespace=namespace, labels=labels
                )
                if resource == "cronjob":
                    self.cron_reads += 1
                    if self.cron_reads == 2:
                        value["spec"]["jobTemplate"]["spec"][  # type: ignore[index]
                            "activeDeadlineSeconds"
                        ] = 1
                return value

        with self.assertRaisesRegex(
            pilot.PilotError, "cronjob_(retry_contract_invalid|postcondition_failed)"
        ):
            pilot.run_pilot(
                DriftKubectl(),
                namespace="coop-prod",
                job_name=pilot.PILOT_JOB_NAME,
                authorize=authorize,
                timeout="70m",
            )

    def test_kubectl_decode_errors_are_sanitized(self) -> None:
        kubectl = pilot.Kubectl("production-context")
        decode_error = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")
        with (
            patch("subprocess.run", side_effect=decode_error),
            self.assertRaisesRegex(pilot.PilotError, "kubectl_get_failed"),
        ):
            kubectl.get_json("job", pilot.PILOT_JOB_NAME, namespace="coop-prod")

    def test_job_terminal_state_fails_fast_and_rejects_contradictions(self) -> None:
        self.assertIsNone(pilot._job_terminal_state({}))
        self.assertEqual(
            pilot._job_terminal_state(
                {"status": {"conditions": [{"type": "Complete", "status": "True"}]}}
            ),
            "Complete",
        )
        self.assertEqual(
            pilot._job_terminal_state(
                {"status": {"conditions": [{"type": "Failed", "status": "True"}]}}
            ),
            "Failed",
        )
        with self.assertRaises(pilot.PilotError):
            pilot._job_terminal_state(
                {
                    "status": {
                        "conditions": [
                            {"type": "Complete", "status": "True"},
                            {"type": "Failed", "status": "True"},
                        ]
                    }
                }
            )

    def test_real_kubectl_wait_stops_immediately_on_failed_job(self) -> None:
        kubectl = pilot.Kubectl("production-context")
        failed = {
            "metadata": {"uid": "job-uid-1"},
            "status": {"conditions": [{"type": "Failed", "status": "True"}]},
        }
        with (
            patch.object(kubectl, "get_json", return_value=failed) as get_json,
            self.assertRaisesRegex(pilot.PilotError, "kubectl_wait_failed"),
        ):
            kubectl.wait_complete(
                "gmail-import-full-pilot",
                "job-uid-1",
                namespace="coop-prod",
                timeout="70m",
            )
        get_json.assert_called_once()

    def test_kubectl_delete_uses_server_side_uid_precondition(self) -> None:
        kubectl = pilot.Kubectl("production-context")
        with (
            patch.object(kubectl, "_run", return_value="{}") as run,
            patch.object(kubectl, "get_job_optional", return_value=None),
            patch.object(kubectl, "get_json", return_value={"items": []}),
        ):
            kubectl.delete_job(
                pilot.PILOT_JOB_NAME,
                "job-uid-1",
                namespace="coop-prod",
                timeout="5m",
            )
        arguments = run.call_args.args[0]
        payload = json.loads(run.call_args.kwargs["input_text"])
        self.assertEqual(
            arguments,
            [
                "delete",
                "--raw",
                "/apis/batch/v1/namespaces/coop-prod/jobs/gmail-import-full-pilot",
                "-f",
                "-",
            ],
        )
        self.assertEqual(payload["preconditions"], {"uid": "job-uid-1"})
        self.assertEqual(payload["propagationPolicy"], "Foreground")

    def test_kubectl_binds_created_job_uid_and_single_succeeded_pod(self) -> None:
        kubectl = pilot.Kubectl("production-context")
        with patch.object(
            kubectl,
            "_run",
            return_value=json.dumps(
                {
                    "metadata": {
                        "name": pilot.PILOT_JOB_NAME,
                        "uid": "job-uid-1",
                        "annotations": {
                            "coop.example.com/pilot-ownership-token": "f" * 64
                        },
                    }
                }
            ),
        ):
            self.assertEqual(
                kubectl.create_json(
                    {
                        "metadata": {
                            "annotations": {
                                "coop.example.com/pilot-ownership-token": "f" * 64
                            }
                        }
                    },
                    namespace="coop-prod",
                ),
                "job-uid-1",
            )

        expected_job = pilot.build_pilot_job(cronjob(), pilot.PILOT_JOB_NAME, "f" * 64)
        expected_template = expected_job["spec"]["template"]  # type: ignore[index]
        pod = {
            "items": [
                {
                    "metadata": {
                        "name": "pilot-pod-1",
                        "uid": "pod-uid-1",
                        "labels": copy.deepcopy(
                            expected_template["metadata"]["labels"]
                        ),  # type: ignore[index]
                        "ownerReferences": [{"uid": "job-uid-1", "controller": True}],
                    },
                    "spec": copy.deepcopy(expected_template["spec"]),  # type: ignore[index]
                    "status": {
                        "phase": "Succeeded",
                        "containerStatuses": [
                            {
                                "restartCount": 0,
                                "imageID": f"docker-pullable://coop-api@{IMAGE.rsplit('@', 1)[1]}",
                            }
                        ],
                    },
                }
            ]
        }
        realized_spec = cast(dict[str, object], pod["items"][0]["spec"])  # type: ignore[index]
        realized_spec["nodeName"] = "kind-control-plane"
        realized_spec["serviceAccount"] = "gmail-full-pilot-runtime"
        realized_spec["priority"] = 0
        realized_spec["preemptionPolicy"] = "PreemptLowerPriority"
        realized_spec["tolerations"] = [
            {
                "effect": "NoExecute",
                "key": "node.kubernetes.io/not-ready",
                "operator": "Exists",
                "tolerationSeconds": 300,
            },
            {
                "effect": "NoExecute",
                "key": "node.kubernetes.io/unreachable",
                "operator": "Exists",
                "tolerationSeconds": 300,
            },
        ]
        realized_containers = cast(list[dict[str, object]], realized_spec["containers"])
        realized_containers[0]["terminationMessagePath"] = "/dev/termination-log"
        realized_containers[0]["terminationMessagePolicy"] = "File"
        with patch.object(kubectl, "get_json", return_value=pod):
            self.assertEqual(
                kubectl.attest_single_pod(
                    pilot.PILOT_JOB_NAME,
                    "job-uid-1",
                    IMAGE,
                    expected_job,
                    namespace="coop-prod",
                ),
                ("pilot-pod-1", "pod-uid-1"),
            )

        for field, value in (
            ("restartPolicy", "OnFailure"),
            ("schedulerName", "attacker-scheduler"),
            ("serviceAccount", "attacker"),
            ("dnsPolicy", "Default"),
            ("dnsConfig", {"options": [{"name": "ndots", "value": "1"}]}),
            ("terminationGracePeriodSeconds", 120),
            ("shareProcessNamespace", True),
            ("nodeSelector", {"attacker": "true"}),
            ("affinity", {"nodeAffinity": {}}),
            ("tolerations", [{"operator": "Exists"}]),
            ("priorityClassName", "system-node-critical"),
            ("priority", 2_000_001_000),
            ("preemptionPolicy", "Never"),
            ("activeDeadlineSeconds", 1),
            ("hostUsers", True),
            ("os", {"name": "linux"}),
            ("hostname", "attacker"),
            ("hostnameOverride", "attacker.example"),
            ("overhead", {"cpu": "1"}),
            ("resources", {"requests": {"cpu": "1"}}),
            ("schedulingGroup", "attacker"),
            ("readinessGates", [{"conditionType": "attacker.example/ready"}]),
            ("topologySpreadConstraints", [{"maxSkew": 1}]),
            ("schedulingGates", [{"name": "attacker.example/gate"}]),
            ("resourceClaims", [{"name": "attacker"}]),
        ):
            extended = copy.deepcopy(pod)
            items = cast(list[dict[str, object]], extended["items"])
            spec = cast(dict[str, object], items[0]["spec"])
            spec[field] = value
            with (
                self.subTest(pod_field=field),
                patch.object(kubectl, "get_json", return_value=extended),
                self.assertRaisesRegex(pilot.PilotError, "pilot_pod_invalid"),
            ):
                kubectl.attest_single_pod(
                    pilot.PILOT_JOB_NAME,
                    "job-uid-1",
                    IMAGE,
                    expected_job,
                    namespace="coop-prod",
                )

        pod["items"].append(copy.deepcopy(pod["items"][0]))  # type: ignore[union-attr]
        with (
            patch.object(kubectl, "get_json", return_value=pod),
            self.assertRaisesRegex(pilot.PilotError, "pilot_pod_invalid"),
        ):
            kubectl.attest_single_pod(
                pilot.PILOT_JOB_NAME,
                "job-uid-1",
                IMAGE,
                expected_job,
                namespace="coop-prod",
            )

        unsafe = copy.deepcopy(pod)
        unsafe["items"] = [unsafe["items"][0]]
        unsafe["items"][0]["spec"]["hostNetwork"] = True  # type: ignore[index]
        unsafe["items"][0]["spec"]["initContainers"] = [  # type: ignore[index]
            {
                "name": "injected",
                "image": IMAGE,
                "securityContext": {"privileged": True},
            }
        ]
        with (
            patch.object(kubectl, "get_json", return_value=unsafe),
            self.assertRaisesRegex(pilot.PilotError, "pilot_pod_invalid"),
        ):
            kubectl.attest_single_pod(
                pilot.PILOT_JOB_NAME,
                "job-uid-1",
                IMAGE,
                expected_job,
                namespace="coop-prod",
            )

        for field, value in (
            ("envFrom", [{"secretRef": {"name": "extra-secret"}}]),
            ("lifecycle", {"postStart": {"exec": {"command": ["true"]}}}),
            ("livenessProbe", {"exec": {"command": ["true"]}}),
            ("readinessProbe", {"exec": {"command": ["true"]}}),
            ("startupProbe", {"exec": {"command": ["true"]}}),
            ("workingDir", "/tmp"),
            ("stdinOnce", True),
            ("restartPolicy", "Always"),
            (
                "restartPolicyRules",
                [{"action": "Restart", "exitCodes": {"operator": "In", "values": [1]}}],
            ),
            ("resizePolicy", [{"resourceName": "cpu", "restartPolicy": "NotRequired"}]),
            ("terminationMessagePath", "/tmp/attacker-termination"),
            ("terminationMessagePolicy", "FallbackToLogsOnError"),
        ):
            extended = copy.deepcopy(pod)
            items = cast(list[dict[str, object]], extended["items"])
            extended["items"] = [items[0]]
            spec = cast(dict[str, object], items[0]["spec"])
            containers = cast(list[dict[str, object]], spec["containers"])
            containers[0][field] = value
            with (
                self.subTest(field=field),
                patch.object(kubectl, "get_json", return_value=extended),
                self.assertRaisesRegex(pilot.PilotError, "pilot_pod_invalid"),
            ):
                kubectl.attest_single_pod(
                    pilot.PILOT_JOB_NAME,
                    "job-uid-1",
                    IMAGE,
                    expected_job,
                    namespace="coop-prod",
                )

        for field, value in (
            ("runAsUser", 65532),
            ("runAsGroup", 65532),
            ("runAsNonRoot", True),
            ("capabilities", {"drop": ["ALL"], "add": ["NET_BIND_SERVICE"]}),
        ):
            extended = copy.deepcopy(pod)
            items = cast(list[dict[str, object]], extended["items"])
            extended["items"] = [items[0]]
            spec = cast(dict[str, object], items[0]["spec"])
            containers = cast(list[dict[str, object]], spec["containers"])
            security = cast(dict[str, object], containers[0]["securityContext"])
            security[field] = value
            with (
                self.subTest(security_field=field),
                patch.object(kubectl, "get_json", return_value=extended),
                self.assertRaisesRegex(pilot.PilotError, "pilot_pod_invalid"),
            ):
                kubectl.attest_single_pod(
                    pilot.PILOT_JOB_NAME,
                    "job-uid-1",
                    IMAGE,
                    expected_job,
                    namespace="coop-prod",
                )

        privileged_service_account = copy.deepcopy(pod)
        items = cast(list[dict[str, object]], privileged_service_account["items"])
        privileged_service_account["items"] = [items[0]]
        spec = cast(dict[str, object], items[0]["spec"])
        spec["serviceAccountName"] = "privileged"
        with (
            patch.object(kubectl, "get_json", return_value=privileged_service_account),
            self.assertRaisesRegex(pilot.PilotError, "pilot_pod_invalid"),
        ):
            kubectl.attest_single_pod(
                pilot.PILOT_JOB_NAME,
                "job-uid-1",
                IMAGE,
                expected_job,
                namespace="coop-prod",
            )

    def test_log_read_rejects_pod_replacement(self) -> None:
        kubectl = pilot.Kubectl("production-context")
        original = {
            "metadata": {
                "name": "pilot-pod-1",
                "uid": "pod-uid-1",
                "ownerReferences": [{"uid": "job-uid-1", "controller": True}],
            }
        }
        replacement = copy.deepcopy(original)
        replacement["metadata"]["uid"] = "pod-uid-2"  # type: ignore[index]
        with (
            patch.object(kubectl, "get_json", side_effect=[original, replacement]),
            patch.object(kubectl, "_run", return_value='{"status":"SUCCEEDED"}'),
            self.assertRaisesRegex(pilot.PilotError, "pilot_pod_identity_invalid"),
        ):
            kubectl.logs("pilot-pod-1", "pod-uid-1", "job-uid-1", namespace="coop-prod")

    def test_run_rejects_invalid_timeout_before_cluster_access(self) -> None:
        kubectl = FakeKubectl()
        with self.assertRaises(pilot.PilotError):
            pilot.run_pilot(
                kubectl,
                namespace="coop-prod",
                job_name="gmail-import-full-pilot",
                authorize=authorize,
                timeout="121m",
            )
        self.assertEqual(kubectl.calls, [])

    def test_run_rejects_unsanitized_or_unsuccessful_collector_output(self) -> None:
        for output in (
            "debug\n"
            + json.dumps({"ok": True, "pending_review": 0, "status": "SUCCEEDED"}),
            json.dumps({"ok": False, "pending_review": 0, "status": "FAILED"}),
            json.dumps({"ok": True, "pending_review": "2", "status": "SUCCEEDED"}),
        ):
            kubectl = FakeKubectl()
            kubectl.log_output = output
            with self.subTest(output=output), self.assertRaises(pilot.PilotError):
                pilot.run_pilot(
                    kubectl,
                    namespace="coop-prod",
                    job_name="gmail-import-full-pilot",
                    authorize=authorize,
                    timeout="70m",
                )


if __name__ == "__main__":
    unittest.main()
