#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import secrets
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Protocol

CRONJOB_NAME = "gmail-import-full"
PILOT_JOB_NAME = "gmail-import-full-pilot"
PILOT_MARKER_NAME = "gmail-import-full-pilot-consumed"
PILOT_OAUTH_SECRET_NAME = "gmail-oauth"
PILOT_RUNTIME_SECRET_NAME = "gmail-collector-db"
PILOT_LABEL = "coop.example.com/gmail-import-pilot"
IMPORT_LABEL = "coop.example.com/gmail-import"
OAUTH_SECRET_KEYS = frozenset(
    {"GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN", "GMAIL_ACCOUNT"}
)
RUNTIME_SECRET_KEYS = frozenset({"COLLECTOR_DATABASE_URL"})
EXPECTED_ENV_KEYS = frozenset(
    {
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "GMAIL_IMPORT_ARMED",
        "GMAIL_IMPORT_QUERY",
        "GMAIL_IMPORT_QUERY_SHA256",
        "GMAIL_ACCOUNT_SHA256",
        "DATABASE_URL",
        *OAUTH_SECRET_KEYS,
    }
)
NETWORK_POLICIES = frozenset({"gmail-import-egress", "gmail-egress-proxy"})
PILOT_MARKER_FINALIZER = "coop.example.com/gmail-full-pilot-retain"
PROXY_IMAGE = "docker.io/envoyproxy/envoy:distroless-v1.35.3@sha256:f9080877fdbaecc788e79e200d912382e57f780b63752160e27dfe34f5d0aa36"
PROXY_CONFIG_SHA256 = "f0eacb17a77630828b66b8801370209d6a27fd5417a2b6ff5c701040d0441279"
EXPECTED_QUERY_SHA256 = (
    "e362825c8b3be3c500d6da685c7e0b2043641fa2136fd801acf57395154f967d"
)
EXPECTED_ACCOUNT_SHA256 = (
    "d260486537baa3f006b0e0e3168df3ba58b784b56f0846e150690af7457b14bb"
)
ADMISSION_SPEC_SHA256 = {
    (
        "validatingadmissionpolicy",
        "gmail-full-pilot-operator",
    ): "9e71ccd8c9a019b3279e4f29b73658f95d1aaf6d3517792c456d01a7709e8990",
    (
        "validatingadmissionpolicybinding",
        "gmail-full-pilot-operator",
    ): "ecf692cdc1324e6387a98bdd24d300f3534fa39b88afc8dcebe59455996ee828",
    (
        "validatingadmissionpolicy",
        "gmail-full-pilot-slot",
    ): "82e232508576ab1a719e3453c048da4317c2d3cdd229e76fda7700f73908855b",
    (
        "validatingadmissionpolicybinding",
        "gmail-full-pilot-slot",
    ): "75c836f17d162c692eb601e739a0dabe7180ec4eab7757651f6168e9a4129894",
}
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
DNS_LABEL_RE = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?\Z")
FLUX_REVISION_RE = re.compile(r"(?:refs/heads/)?main@sha1:([0-9a-f]{40})\Z")
IMAGE_RE = re.compile(
    r"127\.0\.0\.1:5000/coop-api:git-([0-9a-f]{12})@sha256:([0-9a-f]{64})\Z"
)
TIMEOUT_RE = re.compile(r"([1-9]|[1-9][0-9]|1[01][0-9]|120)m\Z")


class PilotError(RuntimeError):
    pass


class PilotJobAlreadyExists(PilotError):
    pass


class PilotCreateUncertain(PilotError):
    pass


class PilotUpdateConflict(PilotError):
    pass


class PilotJobNotFound(PilotError):
    pass


def _timeout_seconds(timeout: str) -> int:
    match = TIMEOUT_RE.fullmatch(timeout)
    if match is None:
        raise PilotError("timeout_invalid")
    return int(match.group(1)) * 60 + 30


@dataclass(frozen=True)
class PilotContract:
    query_hash: str
    image: str
    image_source_revision: str
    source_fingerprint: str
    flux_revision: str = ""
    oauth_secret_uid: str = ""
    oauth_secret_version: str = ""
    runtime_secret_uid: str = ""
    runtime_secret_version: str = ""
    admission_params_uid: str = ""
    admission_params_version: str = ""


class KubectlProtocol(Protocol):
    def get_json(
        self,
        resource: str,
        name: str | None = None,
        *,
        namespace: str,
        labels: str | None = None,
    ) -> dict[str, object]: ...

    def secret_contract(
        self, name: str, *, namespace: str
    ) -> tuple[set[str], str, str]: ...

    def create_json(self, manifest: dict[str, object], *, namespace: str) -> str: ...

    def activate_job(
        self,
        expected: dict[str, object],
        uid: str,
        expires_at: int,
        *,
        namespace: str,
    ) -> None: ...

    def consume_pilot_slot(
        self, contract: PilotContract, ownership_token: str, *, namespace: str
    ) -> str: ...

    def assert_permissions(self, *, namespace: str) -> None: ...

    def assert_pilot_marker(
        self, expected_uid: str | None, *, namespace: str
    ) -> None: ...

    def get_job_optional(
        self, name: str, *, namespace: str
    ) -> dict[str, object] | None: ...

    def delete_job(
        self, name: str, uid: str, *, namespace: str, timeout: str
    ) -> None: ...

    def wait_complete(
        self, name: str, uid: str, *, namespace: str, timeout: str
    ) -> tuple[str, str]: ...

    def attest_single_pod(
        self,
        name: str,
        uid: str,
        image: str,
        expected_job: dict[str, object],
        *,
        namespace: str,
    ) -> tuple[str, str]: ...

    def logs(
        self, pod_name: str, pod_uid: str, job_uid: str, *, namespace: str
    ) -> str: ...


class Kubectl:
    def __init__(self, context: str) -> None:
        if not context or context != context.strip():
            raise PilotError("context_invalid")
        self._context = context

    def _run(
        self,
        arguments: list[str],
        *,
        namespace: str,
        input_text: str | None = None,
        operation: str,
        timeout_seconds: int = 120,
    ) -> str:
        command = ["kubectl", "--context", self._context, "-n", namespace, *arguments]
        try:
            completed = subprocess.run(
                command,
                input=input_text,
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout_seconds,
            )
        except (OSError, UnicodeError, subprocess.SubprocessError) as error:
            if operation in {"create", "consume_slot", "create_snapshot"}:
                raise PilotCreateUncertain("kubectl_create_uncertain") from error
            raise PilotError(f"kubectl_{operation}_failed") from error
        if completed.returncode != 0:
            diagnostic = f"{completed.stdout}\n{completed.stderr}"
            if operation in {"create", "consume_slot", "create_snapshot"}:
                if "AlreadyExists" in diagnostic:
                    raise PilotJobAlreadyExists("pilot_job_already_exists")
                raise PilotCreateUncertain("kubectl_create_uncertain")
            if operation == "get_optional" and "NotFound" in diagnostic:
                raise PilotJobNotFound("pilot_job_not_found")
            if operation == "activate" and "Conflict" in diagnostic:
                raise PilotUpdateConflict("pilot_job_activation_conflict")
            raise PilotError(f"kubectl_{operation}_failed")
        return completed.stdout

    def get_json(
        self,
        resource: str,
        name: str | None = None,
        *,
        namespace: str,
        labels: str | None = None,
    ) -> dict[str, object]:
        arguments = ["get", resource]
        if name is not None:
            arguments.append(name)
        if labels is not None:
            arguments.extend(["-l", labels])
        arguments.extend(["-o", "json"])
        raw = self._run(arguments, namespace=namespace, operation="get")
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise PilotError("kubectl_response_invalid") from error
        if not isinstance(value, dict):
            raise PilotError("kubectl_response_invalid")
        return value

    def secret_contract(
        self, name: str, *, namespace: str
    ) -> tuple[set[str], str, str]:
        template = '{{.metadata.uid}}{{"\\n"}}{{.metadata.resourceVersion}}{{"\\n"}}{{range $key, $value := .data}}{{if $value}}{{$key}}{{"\\n"}}{{end}}{{end}}'
        raw = self._run(
            ["get", "secret", name, "-o", f"go-template={template}"],
            namespace=namespace,
            operation="secret_contract",
        )
        lines = raw.splitlines()
        if len(lines) < 2 or not lines[0] or not lines[1]:
            raise PilotError("secret_contract_invalid")
        uid, resource_version, *keys = lines
        if any(not key or key != key.strip() for key in keys):
            raise PilotError("secret_key_inventory_invalid")
        return set(keys), uid, resource_version

    def create_json(self, manifest: dict[str, object], *, namespace: str) -> str:
        payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
        raw = self._run(
            ["create", "-f", "-", "-o", "json"],
            namespace=namespace,
            input_text=payload,
            operation="create",
        )
        try:
            created = json.loads(raw)
            metadata = _mapping(
                _mapping(created, "kubectl_create_response_invalid").get("metadata"),
                "kubectl_create_response_invalid",
            )
            expected_metadata = _mapping(
                manifest.get("metadata"), "kubectl_create_response_invalid"
            )
            expected_annotations = _mapping(
                expected_metadata.get("annotations"), "kubectl_create_response_invalid"
            )
            annotations = _mapping(
                metadata.get("annotations"), "kubectl_create_response_invalid"
            )
            uid = metadata.get("uid")
            if (
                metadata.get("name") != PILOT_JOB_NAME
                or not isinstance(uid, str)
                or not uid
                or annotations.get("coop.example.com/pilot-ownership-token")
                != expected_annotations.get("coop.example.com/pilot-ownership-token")
            ):
                raise PilotError("kubectl_create_response_invalid")
        except (json.JSONDecodeError, PilotError) as error:
            raise PilotCreateUncertain("kubectl_create_uncertain") from error
        return uid

    def activate_job(
        self,
        expected: dict[str, object],
        uid: str,
        expires_at: int,
        *,
        namespace: str,
    ) -> None:
        raw = ""
        persisted: dict[str, object] = {}
        expected_metadata = _mapping(
            expected.get("metadata"), "pilot_job_contract_invalid"
        )
        for attempt in range(5):
            persisted = self.get_json("job", PILOT_JOB_NAME, namespace=namespace)
            metadata = _mapping(persisted.get("metadata"), "pilot_job_contract_invalid")
            resource_version = metadata.get("resourceVersion")
            if (
                persisted.get("apiVersion") != "batch/v1"
                or persisted.get("kind") != "Job"
                or metadata.get("name") != PILOT_JOB_NAME
                or metadata.get("uid") != uid
                or not isinstance(resource_version, str)
                or not resource_version
                or metadata.get("labels") != expected_metadata.get("labels")
                or metadata.get("annotations") != expected_metadata.get("annotations")
            ):
                raise PilotError("pilot_job_contract_invalid")
            _validate_persisted_pilot_job(persisted, expected)
            activated = copy.deepcopy(persisted)
            activated_spec = _mapping(
                activated.get("spec"), "pilot_job_contract_invalid"
            )
            if activated_spec.get("suspend") is not True:
                raise PilotError("pilot_job_contract_invalid")
            activated_spec["suspend"] = False
            payload = json.dumps(activated, sort_keys=True, separators=(",", ":"))
            if expires_at <= int(time.time()):
                raise PilotError("live_attestation_expired")
            try:
                raw = self._run(
                    ["replace", "-f", "-", "-o", "json"],
                    namespace=namespace,
                    input_text=payload,
                    operation="activate",
                )
                break
            except PilotUpdateConflict:
                if attempt == 4:
                    raise PilotError("pilot_job_activation_conflict") from None
                time.sleep(0.2)
        try:
            response = _mapping(json.loads(raw), "pilot_job_activation_invalid")
            response_metadata = _mapping(
                response.get("metadata"), "pilot_job_activation_invalid"
            )
        except (json.JSONDecodeError, PilotError) as error:
            raise PilotError("pilot_job_activation_invalid") from error
        if (
            response.get("apiVersion") != "batch/v1"
            or response.get("kind") != "Job"
            or response_metadata.get("name") != PILOT_JOB_NAME
            or response_metadata.get("uid") != uid
            or response_metadata.get("labels") != expected_metadata.get("labels")
            or response_metadata.get("annotations")
            != expected_metadata.get("annotations")
        ):
            raise PilotError("pilot_job_activation_invalid")
        expected_active = copy.deepcopy(expected)
        expected_active_spec = _mapping(
            expected_active.get("spec"), "pilot_job_activation_invalid"
        )
        expected_active_spec["suspend"] = False
        try:
            _validate_persisted_pilot_job(response, expected_active)
        except PilotError as error:
            raise PilotError("pilot_job_activation_invalid") from error

    def _create_owned_resource(
        self,
        manifest: dict[str, object],
        *,
        namespace: str,
        expected_name: str,
        ownership_token: str,
        operation: str,
    ) -> str:
        payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
        try:
            raw = self._run(
                ["create", "-f", "-", "-o", "json"],
                namespace=namespace,
                input_text=payload,
                operation=operation,
            )
            created = _mapping(json.loads(raw), "resource_create_response_invalid")
            metadata = _mapping(
                created.get("metadata"), "resource_create_response_invalid"
            )
            annotations = _mapping(
                metadata.get("annotations"), "resource_create_response_invalid"
            )
            uid = metadata.get("uid")
            if (
                metadata.get("name") != expected_name
                or not isinstance(uid, str)
                or not uid
                or annotations.get("coop.example.com/pilot-ownership-token")
                != ownership_token
            ):
                raise PilotError("resource_create_response_invalid")
            return uid
        except (json.JSONDecodeError, PilotError) as error:
            if isinstance(error, PilotJobAlreadyExists):
                raise
            raise PilotCreateUncertain(f"kubectl_{operation}_uncertain") from error

    def consume_pilot_slot(
        self, contract: PilotContract, ownership_token: str, *, namespace: str
    ) -> str:
        marker = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": PILOT_MARKER_NAME,
                "labels": {PILOT_LABEL: "consumed"},
                "finalizers": [PILOT_MARKER_FINALIZER],
                "annotations": {
                    "coop.example.com/pilot-ownership-token": ownership_token,
                    "coop.example.com/image-source-revision": contract.image_source_revision,
                    "coop.example.com/flux-revision": contract.flux_revision,
                    "coop.example.com/query-sha256": contract.query_hash,
                },
            },
            "immutable": True,
            "data": {"state": "CONSUMED"},
        }
        try:
            return self._create_owned_resource(
                marker,
                namespace=namespace,
                expected_name=PILOT_MARKER_NAME,
                ownership_token=ownership_token,
                operation="consume_slot",
            )
        except PilotJobAlreadyExists as error:
            raise PilotError("pilot_slot_already_consumed") from error
        except PilotCreateUncertain as error:
            raise PilotError("pilot_slot_uncertain") from error

    def assert_permissions(self, *, namespace: str) -> None:
        required = (
            ("get", "cronjobs.batch", namespace, ("gmail-import-full",)),
            ("get", "deployments.apps", namespace, ("gmail-egress-proxy",)),
            ("list", "replicasets.apps", namespace, ()),
            ("get", "services", namespace, ("gmail-egress-proxy",)),
            ("get", "endpoints", namespace, ("gmail-egress-proxy",)),
            ("get", "networkpolicies.networking.k8s.io", namespace, ()),
            ("list", "networkpolicies.networking.k8s.io", namespace, ()),
            (
                "get",
                "configmaps",
                namespace,
                (
                    "gmail-egress-proxy-config",
                    PILOT_MARKER_NAME,
                    "gmail-full-pilot-admission-params",
                ),
            ),
            ("create", "configmaps", namespace, ()),
            ("get", "secrets", namespace, ("gmail-oauth", "gmail-collector-db")),
            ("get", "jobs.batch", namespace, ()),
            ("list", "jobs.batch", namespace, ()),
            ("create", "jobs.batch", namespace, ()),
            ("update", "jobs.batch", namespace, ()),
            ("delete", "jobs.batch", namespace, ()),
            ("get", "pods", namespace, ()),
            ("list", "pods", namespace, ()),
            ("get", "pods/log", namespace, ()),
            (
                "get",
                "kustomizations.kustomize.toolkit.fluxcd.io",
                "flux-system",
                ("coop-root", "coop-database", "coop-migration", "coop-import"),
            ),
            (
                "get",
                "gitrepositories.source.toolkit.fluxcd.io",
                "flux-system",
                ("coop-video-planning",),
            ),
            (
                "get",
                "validatingadmissionpolicies.admissionregistration.k8s.io",
                namespace,
                ("gmail-full-pilot-operator", "gmail-full-pilot-slot"),
            ),
            (
                "get",
                "validatingadmissionpolicybindings.admissionregistration.k8s.io",
                namespace,
                ("gmail-full-pilot-operator", "gmail-full-pilot-slot"),
            ),
        )
        for verb, resource, target_namespace, resource_names in required:
            targets = resource_names or (None,)
            for resource_name in targets:
                resource_arg = (
                    resource if resource_name is None else f"{resource}/{resource_name}"
                )
                answer = self._run(
                    ["auth", "can-i", verb, resource_arg],
                    namespace=target_namespace,
                    operation="permission_check",
                ).strip()
                if answer != "yes":
                    raise PilotError("pilot_permissions_invalid")
        forbidden = (
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
        )
        for verb, resource in forbidden:
            answer = self._run(
                ["auth", "can-i", verb, resource],
                namespace=namespace,
                operation="permission_check",
            ).strip()
            if answer != "no":
                raise PilotError("pilot_permissions_overbroad")
        # The execution identity must be unable to remove the durable slot marker.
        answer = self._run(
            ["auth", "can-i", "delete", "configmap", PILOT_MARKER_NAME],
            namespace=namespace,
            operation="permission_check",
        ).strip()
        if answer != "no":
            raise PilotError("pilot_marker_delete_permission_forbidden")

    def assert_pilot_marker(self, expected_uid: str | None, *, namespace: str) -> None:
        try:
            raw = self._run(
                ["get", "configmap", PILOT_MARKER_NAME, "-o", "json"],
                namespace=namespace,
                operation="get_optional",
            )
            marker = _mapping(json.loads(raw), "pilot_slot_invalid")
        except PilotJobNotFound:
            if expected_uid is None:
                return
            raise
        except (json.JSONDecodeError, PilotError) as error:
            raise PilotError("pilot_slot_uncertain") from error
        metadata = _mapping(marker.get("metadata"), "pilot_slot_invalid")
        valid = (
            metadata.get("name") == PILOT_MARKER_NAME
            and metadata.get("labels") == {PILOT_LABEL: "consumed"}
            and metadata.get("finalizers") == [PILOT_MARKER_FINALIZER]
            and marker.get("immutable") is True
            and marker.get("data") == {"state": "CONSUMED"}
        )
        if expected_uid is None:
            raise PilotError("pilot_slot_already_consumed")
        if not valid or metadata.get("uid") != expected_uid:
            raise PilotError("pilot_slot_uncertain")

    def _get_secret_optional(
        self, name: str, *, namespace: str
    ) -> dict[str, object] | None:
        try:
            raw = self._run(
                ["get", "secret", name, "-o", "json"],
                namespace=namespace,
                operation="get_optional",
            )
        except PilotJobNotFound:
            return None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise PilotError("secret_snapshot_response_invalid") from error
        if not isinstance(value, dict):
            raise PilotError("secret_snapshot_response_invalid")
        return value

    def assert_pilot_snapshots_absent(self, *, namespace: str) -> None:
        for name in (PILOT_OAUTH_SECRET_NAME, PILOT_RUNTIME_SECRET_NAME):
            if self._get_secret_optional(name, namespace=namespace) is not None:
                raise PilotError(f"pilot_secret_exists:{name}")

    def _delete_job_resource(
        self,
        name: str,
        uid: str,
        *,
        namespace: str,
        timeout: str,
    ) -> None:
        if not uid:
            raise PilotError("resource_identity_invalid")
        endpoint = f"/apis/batch/v1/namespaces/{namespace}/jobs/{name}"
        payload = json.dumps(
            {
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "preconditions": {"uid": uid},
                "propagationPolicy": "Foreground",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        self._run(
            ["delete", "--raw", endpoint, "-f", "-"],
            namespace=namespace,
            input_text=payload,
            operation="delete",
            timeout_seconds=min(_timeout_seconds(timeout), 60),
        )

    def get_job_optional(
        self, name: str, *, namespace: str
    ) -> dict[str, object] | None:
        try:
            raw = self._run(
                ["get", "job", name, "-o", "json"],
                namespace=namespace,
                operation="get_optional",
            )
        except PilotJobNotFound:
            return None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise PilotError("kubectl_response_invalid") from error
        if not isinstance(value, dict):
            raise PilotError("kubectl_response_invalid")
        return value

    def delete_job(self, name: str, uid: str, *, namespace: str, timeout: str) -> None:
        if not uid:
            raise PilotError("job_identity_invalid")
        deadline = time.monotonic() + _timeout_seconds(timeout)
        self._delete_job_resource(name, uid, namespace=namespace, timeout=timeout)
        while True:
            observed = self.get_job_optional(name, namespace=namespace)
            pods = self.get_json("pods", namespace=namespace)
            all_pods = _list(pods.get("items"), "pilot_pod_invalid")
            pod_items = []
            for raw_pod in all_pods:
                pod = _mapping(raw_pod, "pilot_pod_invalid")
                metadata = _mapping(pod.get("metadata"), "pilot_pod_invalid")
                owners = _list(metadata.get("ownerReferences"), "pilot_pod_invalid")
                if any(
                    isinstance(owner, dict) and owner.get("uid") == uid
                    for owner in owners
                ):
                    pod_items.append(pod)
            if observed is None and not pod_items:
                return
            if observed is not None:
                metadata = _mapping(observed.get("metadata"), "job_identity_invalid")
                if metadata.get("uid") != uid:
                    raise PilotError("job_identity_changed")
            if time.monotonic() >= deadline:
                raise PilotError("kubectl_delete_failed")
            time.sleep(1)

    def wait_complete(
        self, name: str, uid: str, *, namespace: str, timeout: str
    ) -> tuple[str, str]:
        deadline = time.monotonic() + _timeout_seconds(timeout) - 30
        while True:
            job = self.get_json("job", name, namespace=namespace)
            metadata = _mapping(job.get("metadata"), "job_identity_invalid")
            if metadata.get("uid") != uid:
                raise PilotError("job_identity_invalid")
            state = _job_terminal_state(job)
            if state == "Complete":
                status = _mapping(job.get("status"), "job_status_invalid")
                if (
                    status.get("succeeded") != 1
                    or status.get("failed", 0) != 0
                    or status.get("active", 0) != 0
                    or not isinstance(status.get("startTime"), str)
                    or not isinstance(status.get("completionTime"), str)
                ):
                    raise PilotError("job_status_invalid")
                return status["startTime"], status["completionTime"]
            if state == "Failed":
                raise PilotError("kubectl_wait_failed")
            if time.monotonic() >= deadline:
                raise PilotError("kubectl_wait_failed")
            time.sleep(5)

    def attest_single_pod(
        self,
        name: str,
        uid: str,
        image: str,
        expected_job: dict[str, object],
        *,
        namespace: str,
    ) -> tuple[str, str]:
        pods = self.get_json("pods", namespace=namespace, labels=f"job-name={name}")
        items = _list(pods.get("items"), "pilot_pod_invalid")
        if len(items) != 1:
            raise PilotError("pilot_pod_invalid")
        pod = _mapping(items[0], "pilot_pod_invalid")
        metadata = _mapping(pod.get("metadata"), "pilot_pod_invalid")
        owners = _list(metadata.get("ownerReferences"), "pilot_pod_invalid")
        spec = _mapping(pod.get("spec"), "pilot_pod_invalid")
        status = _mapping(pod.get("status"), "pilot_pod_invalid")
        containers = _list(spec.get("containers"), "pilot_pod_invalid")
        statuses = _list(status.get("containerStatuses"), "pilot_pod_invalid")
        image_digest = image.rsplit("@", 1)[1]
        expected_job_spec = _mapping(expected_job.get("spec"), "pilot_pod_invalid")
        expected_template = _mapping(
            expected_job_spec.get("template"), "pilot_pod_invalid"
        )
        expected_metadata = _mapping(
            expected_template.get("metadata"), "pilot_pod_invalid"
        )
        expected_labels = _mapping(expected_metadata.get("labels"), "pilot_pod_invalid")
        actual_labels = _mapping(metadata.get("labels"), "pilot_pod_invalid")
        expected_spec = _mapping(expected_template.get("spec"), "pilot_pod_invalid")
        generated_labels = {
            "batch.kubernetes.io/controller-uid",
            "batch.kubernetes.io/job-name",
            "controller-uid",
            "job-name",
        }
        if (
            len(owners) != 1
            or _mapping(owners[0], "pilot_pod_invalid").get("uid") != uid
            or _mapping(owners[0], "pilot_pod_invalid").get("controller") is not True
            or len(containers) != 1
            or _mapping(containers[0], "pilot_pod_invalid").get("image") != image
            or status.get("phase") != "Succeeded"
            or len(statuses) != 1
            or any(
                actual_labels.get(key) != value
                for key, value in expected_labels.items()
            )
            or set(actual_labels) - set(expected_labels) - generated_labels
            or metadata.get("annotations", {})
            != expected_metadata.get("annotations", {})
            or spec.get("automountServiceAccountToken", False) is not False
            or spec.get("enableServiceLinks", False) is not False
            or spec.get("serviceAccountName") != "gmail-full-pilot-runtime"
            or spec.get("serviceAccount") != "gmail-full-pilot-runtime"
            or spec.get("restartPolicy") != expected_spec.get("restartPolicy")
            or spec.get("schedulerName", "default-scheduler") != "default-scheduler"
            or spec.get("dnsPolicy", "ClusterFirst") != "ClusterFirst"
            or spec.get("terminationGracePeriodSeconds", 30) != 30
            or spec.get("hostNetwork", False) is not False
            or spec.get("hostPID", False) is not False
            or spec.get("hostIPC", False) is not False
            or spec.get("shareProcessNamespace", False) is not False
            or not isinstance(spec.get("nodeName"), str)
            or not spec.get("nodeName")
            or spec.get("nodeSelector", {}) != {}
            or spec.get("affinity") is not None
            or spec.get("tolerations")
            != [
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
            or spec.get("priorityClassName", "") != ""
            or spec.get("priority") not in (None, 0)
            or spec.get("preemptionPolicy") not in (None, "PreemptLowerPriority")
            or spec.get("activeDeadlineSeconds") is not None
            or spec.get("hostUsers") is not None
            or spec.get("os") is not None
            or spec.get("topologySpreadConstraints", []) != []
            or spec.get("readinessGates", []) != []
            or spec.get("schedulingGates", []) != []
            or spec.get("resourceClaims", []) != []
            or spec.get("hostname", "") != ""
            or spec.get("hostnameOverride") is not None
            or spec.get("subdomain", "") != ""
            or spec.get("overhead") is not None
            or spec.get("resources") is not None
            or spec.get("schedulingGroup") is not None
            or spec.get("setHostnameAsFQDN", False) is not False
            or spec.get("initContainers", []) != []
            or spec.get("ephemeralContainers", []) != []
            or spec.get("hostAliases", []) != []
            or spec.get("imagePullSecrets", []) != []
            or spec.get("dnsConfig") is not None
            or spec.get("runtimeClassName") is not None
            or spec.get("volumes") != expected_spec.get("volumes")
            or spec.get("securityContext") != expected_spec.get("securityContext")
            or len(containers) != 1
            or _mapping(containers[0], "pilot_pod_invalid").get(
                "terminationMessagePath"
            )
            != "/dev/termination-log"
            or _mapping(containers[0], "pilot_pod_invalid").get(
                "terminationMessagePolicy"
            )
            != "File"
            or any(
                _mapping(containers[0], "pilot_pod_invalid").get(key)
                != _mapping(
                    _list(expected_spec.get("containers"), "pilot_pod_invalid")[0],
                    "pilot_pod_invalid",
                ).get(key)
                for key in (
                    "args",
                    "command",
                    "env",
                    "image",
                    "imagePullPolicy",
                    "name",
                    "resources",
                    "securityContext",
                    "volumeMounts",
                )
            )
            or _mapping(containers[0], "pilot_pod_invalid").get("envFrom", []) != []
            or _mapping(containers[0], "pilot_pod_invalid").get("lifecycle") is not None
            or _mapping(containers[0], "pilot_pod_invalid").get("ports", []) != []
            or _mapping(containers[0], "pilot_pod_invalid").get("livenessProbe")
            is not None
            or _mapping(containers[0], "pilot_pod_invalid").get("readinessProbe")
            is not None
            or _mapping(containers[0], "pilot_pod_invalid").get("startupProbe")
            is not None
            or _mapping(containers[0], "pilot_pod_invalid").get("workingDir", "") != ""
            or _mapping(containers[0], "pilot_pod_invalid").get("restartPolicy")
            is not None
            or _mapping(containers[0], "pilot_pod_invalid").get("restartPolicyRules")
            is not None
            or _mapping(containers[0], "pilot_pod_invalid").get("resizePolicy", [])
            != []
            or _mapping(containers[0], "pilot_pod_invalid").get("volumeDevices", [])
            != []
            or _mapping(containers[0], "pilot_pod_invalid").get("stdin", False)
            is not False
            or _mapping(containers[0], "pilot_pod_invalid").get("stdinOnce", False)
            is not False
            or _mapping(containers[0], "pilot_pod_invalid").get("tty", False)
            is not False
        ):
            raise PilotError("pilot_pod_invalid")
        container_status = _mapping(statuses[0], "pilot_pod_invalid")
        if (
            container_status.get("restartCount") != 0
            or not isinstance(container_status.get("imageID"), str)
            or not container_status["imageID"].endswith(f"@{image_digest}")
        ):
            raise PilotError("pilot_pod_invalid")
        pod_name = metadata.get("name")
        pod_uid = metadata.get("uid")
        if (
            not isinstance(pod_name, str)
            or not pod_name
            or not isinstance(pod_uid, str)
            or not pod_uid
        ):
            raise PilotError("pilot_pod_invalid")
        return pod_name, pod_uid

    def logs(self, pod_name: str, pod_uid: str, job_uid: str, *, namespace: str) -> str:
        self._validate_pod_identity(pod_name, pod_uid, job_uid, namespace=namespace)
        raw = self._run(
            ["logs", f"pod/{pod_name}"], namespace=namespace, operation="logs"
        )
        self._validate_pod_identity(pod_name, pod_uid, job_uid, namespace=namespace)
        return raw

    def _validate_pod_identity(
        self, pod_name: str, pod_uid: str, job_uid: str, *, namespace: str
    ) -> None:
        pod = self.get_json("pod", pod_name, namespace=namespace)
        metadata = _mapping(pod.get("metadata"), "pilot_pod_identity_invalid")
        owners = _list(metadata.get("ownerReferences"), "pilot_pod_identity_invalid")
        owner = (
            _mapping(owners[0], "pilot_pod_identity_invalid")
            if len(owners) == 1
            else {}
        )
        if (
            metadata.get("uid") != pod_uid
            or owner.get("uid") != job_uid
            or owner.get("controller") is not True
        ):
            raise PilotError("pilot_pod_identity_invalid")


def _mapping(value: object, error_code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PilotError(error_code)
    return value


def _list(value: object, error_code: str) -> list[Any]:
    if not isinstance(value, list):
        raise PilotError(error_code)
    return value


def _validate_persisted_pilot_job(
    actual: dict[str, object], expected: dict[str, object]
) -> None:
    error = "pilot_job_contract_invalid"
    actual_spec = _mapping(actual.get("spec"), error)
    expected_spec = _mapping(expected.get("spec"), error)
    allowed_job_spec = set(expected_spec) | {
        "completionMode",
        "manualSelector",
        "podReplacementPolicy",
        "selector",
    }
    if set(actual_spec) - allowed_job_spec:
        raise PilotError(error)
    if actual_spec.get("manualSelector", False) is not False:
        raise PilotError(error)
    if actual_spec.get("completionMode", "NonIndexed") != "NonIndexed":
        raise PilotError(error)
    if actual_spec.get("podReplacementPolicy") != "Failed":
        raise PilotError(error)
    raw_selector = actual_spec.get("selector")
    if raw_selector is not None:
        selector = _mapping(raw_selector, error)
        match_labels = _mapping(selector.get("matchLabels"), error)
        job_metadata = _mapping(actual.get("metadata"), error)
        job_uid = job_metadata.get("uid")
        if (
            not isinstance(job_uid, str)
            or not job_uid
            or set(selector) != {"matchLabels"}
            or not match_labels
            or set(match_labels)
            - {"batch.kubernetes.io/controller-uid", "controller-uid"}
            or any(value != job_uid for value in match_labels.values())
        ):
            raise PilotError(error)
    for key in (
        "activeDeadlineSeconds",
        "backoffLimit",
        "completions",
        "parallelism",
        "suspend",
        "ttlSecondsAfterFinished",
    ):
        if actual_spec.get(key) != expected_spec.get(key):
            raise PilotError(error)
    actual_template = _mapping(actual_spec.get("template"), error)
    expected_template = _mapping(expected_spec.get("template"), error)
    actual_template_metadata = _mapping(actual_template.get("metadata"), error)
    expected_template_metadata = _mapping(expected_template.get("metadata"), error)
    if set(actual_template_metadata) - {"annotations", "creationTimestamp", "labels"}:
        raise PilotError(error)
    actual_labels = _mapping(actual_template_metadata.get("labels"), error)
    expected_labels = _mapping(expected_template_metadata.get("labels"), error)
    generated_labels = {
        "batch.kubernetes.io/controller-uid",
        "batch.kubernetes.io/job-name",
        "controller-uid",
        "job-name",
    }
    if (
        any(actual_labels.get(key) != value for key, value in expected_labels.items())
        or set(actual_labels) - set(expected_labels) - generated_labels
        or actual_template_metadata.get("annotations", {})
        != expected_template_metadata.get("annotations", {})
    ):
        raise PilotError(error)
    actual_pod = _mapping(actual_template.get("spec"), error)
    expected_pod = _mapping(expected_template.get("spec"), error)
    allowed_pod_spec = set(expected_pod) | {
        "dnsPolicy",
        "preemptionPolicy",
        "schedulerName",
        "terminationGracePeriodSeconds",
    }
    if set(actual_pod) - allowed_pod_spec:
        raise PilotError(error)
    if (
        actual_pod.get("dnsPolicy", "ClusterFirst") != "ClusterFirst"
        or actual_pod.get("preemptionPolicy", "PreemptLowerPriority")
        != "PreemptLowerPriority"
        or actual_pod.get("schedulerName", "default-scheduler") != "default-scheduler"
        or actual_pod.get("terminationGracePeriodSeconds", 30) != 30
    ):
        raise PilotError(error)
    for key in (
        "automountServiceAccountToken",
        "enableServiceLinks",
        "hostIPC",
        "hostNetwork",
        "hostPID",
        "initContainers",
        "restartPolicy",
        "securityContext",
        "volumes",
    ):
        if actual_pod.get(
            key, False if key.startswith("host") else None
        ) != expected_pod.get(key, False if key.startswith("host") else None):
            raise PilotError(error)
    for forbidden_key in (
        "ephemeralContainers",
        "hostAliases",
        "hostUsers",
        "nodeName",
        "runtimeClassName",
        "serviceAccount",
        "serviceAccountName",
        "shareProcessNamespace",
    ):
        if forbidden_key in actual_pod and forbidden_key not in expected_pod:
            raise PilotError(error)
    actual_containers = _list(actual_pod.get("containers"), error)
    expected_containers = _list(expected_pod.get("containers"), error)
    if len(actual_containers) != 1 or len(expected_containers) != 1:
        raise PilotError(error)
    actual_container = _mapping(actual_containers[0], error)
    expected_container = _mapping(expected_containers[0], error)
    if (
        set(actual_container)
        - set(expected_container)
        - {
            "terminationMessagePath",
            "terminationMessagePolicy",
        }
    ):
        raise PilotError(error)
    if (
        actual_container.get("terminationMessagePath", "/dev/termination-log")
        != "/dev/termination-log"
        or actual_container.get("terminationMessagePolicy", "File") != "File"
    ):
        raise PilotError(error)
    for key in (
        "args",
        "command",
        "env",
        "envFrom",
        "image",
        "imagePullPolicy",
        "name",
        "resources",
        "securityContext",
        "volumeMounts",
    ):
        if actual_container.get(
            key, [] if key == "envFrom" else None
        ) != expected_container.get(key, [] if key == "envFrom" else None):
            raise PilotError(error)


def _job_terminal_state(resource: dict[str, object]) -> str | None:
    raw_status = resource.get("status")
    if raw_status is None:
        return None
    status = _mapping(raw_status, "job_status_invalid")
    conditions = _list(status.get("conditions", []), "job_status_invalid")
    terminal: set[str] = set()
    for raw in conditions:
        condition = _mapping(raw, "job_status_invalid")
        if (
            condition.get("type") in {"Complete", "Failed"}
            and condition.get("status") == "True"
        ):
            terminal.add(condition["type"])
    if len(terminal) > 1:
        raise PilotError("job_status_invalid")
    return next(iter(terminal), None)


def _condition_ready(resource: dict[str, object]) -> bool:
    metadata = _mapping(resource.get("metadata"), "resource_invalid")
    status = _mapping(resource.get("status"), "resource_invalid")
    generation = metadata.get("generation")
    if (
        not isinstance(generation, int)
        or status.get("observedGeneration") != generation
    ):
        return False
    conditions = _list(status.get("conditions"), "resource_invalid")
    return any(
        isinstance(item, dict)
        and item.get("type") == "Ready"
        and item.get("status") == "True"
        for item in conditions
    )


def _environment(container: dict[str, Any]) -> dict[str, dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    for raw in _list(container.get("env"), "cronjob_env_invalid"):
        item = _mapping(raw, "cronjob_env_invalid")
        name = item.get("name")
        if not isinstance(name, str) or not name or name in values:
            raise PilotError("cronjob_env_invalid")
        values[name] = item
    return values


def _require_secret_ref(
    env: dict[str, dict[str, Any]], variable: str, secret: str, key: str
) -> None:
    item = env.get(variable)
    if item is None or set(item) != {"name", "valueFrom"}:
        raise PilotError("cronjob_secret_contract_invalid")
    value_from = _mapping(item.get("valueFrom"), "cronjob_secret_contract_invalid")
    if set(value_from) != {"secretKeyRef"}:
        raise PilotError("cronjob_secret_contract_invalid")
    reference = _mapping(
        value_from.get("secretKeyRef"), "cronjob_secret_contract_invalid"
    )
    if reference != {"name": secret, "key": key}:
        raise PilotError("cronjob_secret_contract_invalid")


def _canonical_hash(value: str) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _source_fingerprint(cronjob: dict[str, object]) -> str:
    metadata = _mapping(cronjob.get("metadata"), "cronjob_invalid")
    spec = _mapping(cronjob.get("spec"), "cronjob_invalid")
    source = {
        "metadata": {
            "annotations": metadata.get("annotations", {}),
            "labels": metadata.get("labels", {}),
            "name": metadata.get("name"),
            "namespace": metadata.get("namespace"),
        },
        "spec": spec,
    }
    payload = json.dumps(source, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def validate_cronjob(cronjob: dict[str, object]) -> PilotContract:
    if cronjob.get("apiVersion") != "batch/v1" or cronjob.get("kind") != "CronJob":
        raise PilotError("cronjob_invalid")
    metadata = _mapping(cronjob.get("metadata"), "cronjob_invalid")
    if metadata.get("name") != CRONJOB_NAME:
        raise PilotError("cronjob_invalid")
    spec = _mapping(cronjob.get("spec"), "cronjob_invalid")
    if spec.get("suspend") is not True or spec.get("concurrencyPolicy") != "Forbid":
        raise PilotError("cronjob_not_dormant")
    job_template = _mapping(spec.get("jobTemplate"), "cronjob_invalid")
    job_spec = _mapping(job_template.get("spec"), "cronjob_invalid")
    if (
        set(job_spec) != {"activeDeadlineSeconds", "backoffLimit", "template"}
        or job_spec.get("backoffLimit") != 0
        or job_spec.get("activeDeadlineSeconds") != 3600
        or job_spec.get("parallelism", 1) != 1
        or job_spec.get("completions", 1) != 1
    ):
        raise PilotError("cronjob_retry_contract_invalid")
    pod_template = _mapping(job_spec.get("template"), "cronjob_invalid")
    pod_metadata = _mapping(pod_template.get("metadata"), "cronjob_invalid")
    pod_labels = _mapping(pod_metadata.get("labels"), "cronjob_invalid")
    allowed_pod_labels = {
        "app.kubernetes.io/name": CRONJOB_NAME,
        "app.kubernetes.io/part-of": "coop-video-planning",
        IMPORT_LABEL: "true",
        "coop.example.com/postgres-client": "true",
    }
    if (
        set(pod_labels) - set(allowed_pod_labels)
        or any(
            pod_labels.get(key) != value
            for key, value in allowed_pod_labels.items()
            if key != "app.kubernetes.io/part-of" or key in pod_labels
        )
        or pod_labels.get("app.kubernetes.io/name") != CRONJOB_NAME
        or pod_labels.get(IMPORT_LABEL) != "true"
        or pod_labels.get("coop.example.com/postgres-client") != "true"
    ):
        raise PilotError("cronjob_selector_contract_invalid")
    pod_spec = _mapping(pod_template.get("spec"), "cronjob_invalid")
    pod_security = _mapping(pod_spec.get("securityContext"), "cronjob_invalid")
    if (
        pod_spec.get("restartPolicy") != "Never"
        or pod_spec.get("automountServiceAccountToken") is not False
        or pod_spec.get("enableServiceLinks") is not False
        or pod_spec.get("hostNetwork", False) is not False
        or pod_spec.get("hostPID", False) is not False
        or pod_spec.get("hostIPC", False) is not False
        or pod_spec.get("initContainers", []) != []
        or set(pod_spec)
        != {
            "automountServiceAccountToken",
            "containers",
            "enableServiceLinks",
            "restartPolicy",
            "securityContext",
            "serviceAccountName",
            "volumes",
        }
        or pod_spec.get("serviceAccountName") != "gmail-full-pilot-runtime"
        or pod_spec.get("volumes")
        != [{"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}}]
        or pod_security.get("runAsNonRoot") is not True
        or pod_security.get("runAsUser") != 10001
        or pod_security.get("runAsGroup") != 10001
        or pod_security.get("fsGroup") != 10001
        or pod_security.get("seccompProfile") != {"type": "RuntimeDefault"}
    ):
        raise PilotError("cronjob_pod_security_invalid")
    containers = _list(pod_spec.get("containers"), "cronjob_invalid")
    if len(containers) != 1:
        raise PilotError("cronjob_invalid")
    container = _mapping(containers[0], "cronjob_invalid")
    if (
        container.get("name") != "gmail-import"
        or container.get("command") != ["python", "-m", "app.ingestion.gmail"]
        or container.get("args") != ["full"]
        or container.get("imagePullPolicy") != "IfNotPresent"
        or container.get("envFrom", []) != []
        or container.get("volumeMounts") != [{"name": "tmp", "mountPath": "/tmp"}]
    ):
        raise PilotError("cronjob_command_invalid")
    container_security = _mapping(
        container.get("securityContext"), "cronjob_pod_security_invalid"
    )
    if (
        container_security.get("allowPrivilegeEscalation") is not False
        or container_security.get("privileged", False) is not False
        or container_security.get("readOnlyRootFilesystem") is not True
        or container_security.get("capabilities") != {"drop": ["ALL"]}
    ):
        raise PilotError("cronjob_pod_security_invalid")
    resources = _mapping(container.get("resources"), "cronjob_resources_invalid")
    if not _mapping(
        resources.get("requests"), "cronjob_resources_invalid"
    ) or not _mapping(resources.get("limits"), "cronjob_resources_invalid"):
        raise PilotError("cronjob_resources_invalid")
    image = container.get("image")
    if not isinstance(image, str):
        raise PilotError("cronjob_image_invalid")
    match = IMAGE_RE.fullmatch(image)
    if match is None or match.group(1) == "0" * 12 or match.group(2) == "0" * 64:
        raise PilotError("cronjob_image_invalid")
    annotations = _mapping(
        metadata.get("annotations"), "cronjob_image_provenance_invalid"
    )
    image_source_revision = annotations.get("coop.example.com/image-source-revision")
    if (
        not isinstance(image_source_revision, str)
        or re.fullmatch(r"[0-9a-f]{40}", image_source_revision) is None
        or image_source_revision == "0" * 40
        or not image_source_revision.startswith(match.group(1))
    ):
        raise PilotError("cronjob_image_provenance_invalid")
    env = _environment(container)
    if set(env) != EXPECTED_ENV_KEYS:
        raise PilotError("cronjob_env_invalid")
    if (
        env.get("HTTPS_PROXY", {}).get("value") != "http://gmail-egress-proxy:10000"
        or env.get("HTTP_PROXY", {}).get("value") != "http://gmail-egress-proxy:10000"
        or env.get("NO_PROXY", {}).get("value")
        != "postgres,postgres.coop-prod.svc,gmail-egress-proxy,127.0.0.1,localhost"
    ):
        raise PilotError("cronjob_proxy_contract_invalid")
    if env.get("GMAIL_IMPORT_ARMED", {}).get("value") != "false":
        raise PilotError("cronjob_not_dormant")
    query = env.get("GMAIL_IMPORT_QUERY", {}).get("value")
    query_hash = env.get("GMAIL_IMPORT_QUERY_SHA256", {}).get("value")
    if (
        not isinstance(query, str)
        or not query
        or query != query.strip()
        or not isinstance(query_hash, str)
        or query_hash != EXPECTED_QUERY_SHA256
        or _canonical_hash(query) != query_hash
    ):
        raise PilotError("cronjob_query_contract_invalid")
    account_hash = env.get("GMAIL_ACCOUNT_SHA256", {}).get("value")
    if not isinstance(account_hash, str) or account_hash != EXPECTED_ACCOUNT_SHA256:
        raise PilotError("cronjob_account_contract_invalid")
    for key in OAUTH_SECRET_KEYS:
        _require_secret_ref(env, key, "gmail-oauth", key)
    _require_secret_ref(
        env, "DATABASE_URL", "gmail-collector-db", "COLLECTOR_DATABASE_URL"
    )
    return PilotContract(
        query_hash=query_hash,
        image=image,
        image_source_revision=image_source_revision,
        source_fingerprint=_source_fingerprint(cronjob),
    )


def build_pilot_job(
    cronjob: dict[str, object],
    name: str,
    ownership_token: str,
    *,
    oauth_secret_name: str = "gmail-oauth",
    runtime_secret_name: str = "gmail-collector-db",
) -> dict[str, object]:
    contract = validate_cronjob(cronjob)
    if name != PILOT_JOB_NAME:
        raise PilotError("job_name_invalid")
    if re.fullmatch(r"[0-9a-f]{64}", ownership_token) is None:
        raise PilotError("ownership_token_invalid")
    cron_spec = _mapping(cronjob["spec"], "cronjob_invalid")
    job_template = _mapping(cron_spec["jobTemplate"], "cronjob_invalid")
    job_spec = copy.deepcopy(_mapping(job_template["spec"], "cronjob_invalid"))
    job_spec["backoffLimit"] = 0
    job_spec["parallelism"] = 1
    job_spec["completions"] = 1
    job_spec["podReplacementPolicy"] = "Failed"
    job_spec["suspend"] = True
    job_spec["ttlSecondsAfterFinished"] = 86400
    pod_template = _mapping(job_spec["template"], "cronjob_invalid")
    pod_metadata = _mapping(pod_template.setdefault("metadata", {}), "cronjob_invalid")
    pod_labels = _mapping(pod_metadata.setdefault("labels", {}), "cronjob_invalid")
    pod_labels[PILOT_LABEL] = "true"
    pod_spec = _mapping(pod_template["spec"], "cronjob_invalid")
    container = _mapping(
        _list(pod_spec["containers"], "cronjob_invalid")[0], "cronjob_invalid"
    )
    env = _environment(container)
    env["GMAIL_IMPORT_ARMED"]["value"] = "true"
    for key in OAUTH_SECRET_KEYS:
        _mapping(
            _mapping(env[key]["valueFrom"], "cronjob_secret_contract_invalid")[
                "secretKeyRef"
            ],
            "cronjob_secret_contract_invalid",
        )["name"] = oauth_secret_name
    _mapping(
        _mapping(env["DATABASE_URL"]["valueFrom"], "cronjob_secret_contract_invalid")[
            "secretKeyRef"
        ],
        "cronjob_secret_contract_invalid",
    )["name"] = runtime_secret_name
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "labels": {PILOT_LABEL: "true", IMPORT_LABEL: "true"},
            "annotations": {
                "coop.example.com/gmail-import-query-sha256": contract.query_hash,
                "coop.example.com/gmail-import-image": contract.image,
                "coop.example.com/image-source-revision": contract.image_source_revision,
                "coop.example.com/pilot-ownership-token": ownership_token,
            },
        },
        "spec": job_spec,
    }


def _validate_flux(resource: dict[str, object], name: str) -> str:
    common = {
        "decryption": {"provider": "sops", "secretRef": {"name": "sops-age"}},
        "force": False,
        "interval": "10m",
        "prune": True,
        "retryInterval": "1m",
        "sourceRef": {"kind": "GitRepository", "name": "coop-video-planning"},
    }
    expected = {
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
    metadata = _mapping(resource.get("metadata"), "flux_invalid")
    spec = _mapping(resource.get("spec"), "flux_invalid")
    if (
        name not in expected
        or metadata.get("name") != name
        or metadata.get("namespace") != "flux-system"
        or spec != expected[name]
    ):
        raise PilotError("flux_spec_invalid")
    if not _condition_ready(resource):
        raise PilotError("flux_not_ready")
    status = _mapping(resource.get("status"), "flux_invalid")
    revision = status.get("lastAppliedRevision")
    if not isinstance(revision, str) or FLUX_REVISION_RE.fullmatch(revision) is None:
        raise PilotError("flux_revision_invalid")
    return revision


def _validate_flux_source(resource: dict[str, object]) -> str:
    metadata = _mapping(resource.get("metadata"), "flux_source_invalid")
    spec = _mapping(resource.get("spec"), "flux_source_invalid")
    if (
        metadata.get("name") != "coop-video-planning"
        or metadata.get("namespace") != "flux-system"
        or spec
        != {
            "interval": "1m",
            "ref": {"branch": "main"},
            "secretRef": {"name": "coop-git-readonly"},
            "timeout": "60s",
            "url": "ssh://git@github.com/t-flow-sash/coop-video-planning-dashboard.git",
        }
    ):
        raise PilotError("flux_source_spec_invalid")
    if not _condition_ready(resource):
        raise PilotError("flux_source_not_ready")
    status = _mapping(resource.get("status"), "flux_source_invalid")
    artifact = _mapping(status.get("artifact"), "flux_source_invalid")
    revision = artifact.get("revision")
    digest = artifact.get("digest")
    if (
        not isinstance(revision, str)
        or FLUX_REVISION_RE.fullmatch(revision) is None
        or not isinstance(digest, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
    ):
        raise PilotError("flux_source_revision_invalid")
    return revision


def _validate_network_policies(resource: dict[str, object]) -> None:
    expected = {
        "default-deny": "79558fd7757e83a7e26bf87d1840b7c975e8571d0e2a458ed61e159d9d578d27",
        "allow-dns-egress": "a731827be9e9d8f0438378a373ca5fdf51a164b79d15c53a7dba295b715e17c0",
        "allow-traefik-to-apps": "508ecfa4fa134d03d10cd02cf579a970cb02457e09569854d42e7fc8ae74ce53",
        "allow-postgres-clients": "da383ce29049fe380c6c239e6f6b4eb0e0bc0d4cb8f72cae69d57bbe14f80537",
        "allow-postgres-egress": "7329f813cc9b6102c8b486e92fdf374b6d2ae5484b2e10688891229ffa6d055e",
        "gmail-import-egress": "558cb9e82cff8c7e8a074f5dcf091d5063a5730625abb11d3eef86fcb0b3a209",
        "gmail-egress-proxy": "c546626be66b018461f8761c159f4ac0b1a1cd4fc5556adc0458d50ce4f64b26",
    }
    actual: dict[str, str] = {}
    for raw in _list(resource.get("items"), "network_policy_contract_invalid"):
        item = _mapping(raw, "network_policy_contract_invalid")
        metadata = _mapping(item.get("metadata"), "network_policy_contract_invalid")
        spec = _mapping(item.get("spec"), "network_policy_contract_invalid")
        name = metadata.get("name")
        if not isinstance(name, str) or name in actual:
            raise PilotError("network_policy_contract_invalid")
        payload = json.dumps(spec, sort_keys=True, separators=(",", ":"))
        actual[name] = hashlib.sha256(payload.encode()).hexdigest()
    if actual != expected:
        raise PilotError("network_policy_contract_invalid")


def _validate_proxy_config(resource: dict[str, object]) -> None:
    data = _mapping(resource.get("data"), "proxy_config_invalid")
    payload = json.dumps(data, sort_keys=True, separators=(",", ":"))
    if hashlib.sha256(payload.encode()).hexdigest() != PROXY_CONFIG_SHA256:
        raise PilotError("proxy_config_invalid")


def _validate_proxy_config_annotation(metadata: dict[str, object], error: str) -> None:
    annotations = _mapping(metadata.get("annotations"), error)
    if annotations.get("coop.example.com/proxy-config-sha256") != PROXY_CONFIG_SHA256:
        raise PilotError(error)


def _validate_proxy_pod_contract(spec: dict[str, object], error: str) -> None:
    security = _mapping(spec.get("securityContext"), error)
    containers = _list(spec.get("containers"), error)
    volumes = _list(spec.get("volumes"), error)
    if len(containers) != 1 or len(volumes) != 2:
        raise PilotError(error)
    container = _mapping(containers[0], error)
    resources = _mapping(container.get("resources"), error)
    mounts = {
        _mapping(value, error).get("name"): _mapping(value, error)
        for value in _list(container.get("volumeMounts"), error)
    }
    volume_map = {
        _mapping(value, error).get("name"): _mapping(value, error) for value in volumes
    }
    config = _mapping(_mapping(volume_map.get("config"), error).get("configMap"), error)
    tmp = _mapping(_mapping(volume_map.get("tmp"), error).get("emptyDir"), error)
    if (
        spec.get("automountServiceAccountToken") is not False
        or spec.get("hostNetwork", False) is not False
        or spec.get("hostPID", False) is not False
        or spec.get("hostIPC", False) is not False
        or spec.get("initContainers", [])
        or spec.get("ephemeralContainers", [])
        or security.get("runAsNonRoot") is not True
        or security.get("runAsUser") != 65532
        or security.get("runAsGroup") != 65532
        or security.get("fsGroup") != 65532
        or security.get("seccompProfile") != {"type": "RuntimeDefault"}
        or container.get("name") != "envoy"
        or container.get("image") != PROXY_IMAGE
        or container.get("imagePullPolicy") != "IfNotPresent"
        or container.get("args")
        != ["-c", "/etc/envoy/envoy.yaml", "--log-level", "critical"]
        or _mapping(container.get("securityContext"), error)
        != {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        }
        or _mapping(resources.get("requests"), error)
        != {"cpu": "25m", "memory": "48Mi"}
        or _mapping(resources.get("limits"), error)
        != {"cpu": "250m", "memory": "128Mi"}
        or set(mounts) != {"config", "tmp"}
        or mounts["config"].get("mountPath") != "/etc/envoy"
        or mounts["config"].get("readOnly") is not True
        or mounts["tmp"].get("mountPath") != "/tmp"
        or set(volume_map) != {"config", "tmp"}
        or config.get("name") != "gmail-egress-proxy-config"
        or config.get("optional", False) is not False
        or tmp.get("sizeLimit") != "16Mi"
    ):
        raise PilotError(error)


def _validate_proxy(resource: dict[str, object]) -> None:
    metadata = _mapping(resource.get("metadata"), "proxy_invalid")
    spec = _mapping(resource.get("spec"), "proxy_invalid")
    status = _mapping(resource.get("status"), "proxy_invalid")
    generation = metadata.get("generation")
    replicas = spec.get("replicas")
    template = _mapping(spec.get("template"), "proxy_invalid")
    template_metadata = _mapping(template.get("metadata"), "proxy_invalid")
    _validate_proxy_config_annotation(template_metadata, "proxy_invalid")
    pod = _mapping(template.get("spec"), "proxy_invalid")
    containers = _list(pod.get("containers"), "proxy_invalid")
    container = _mapping(containers[0], "proxy_invalid") if len(containers) == 1 else {}
    if (
        not isinstance(generation, int)
        or not isinstance(replicas, int)
        or replicas < 1
        or status.get("observedGeneration") != generation
        or status.get("availableReplicas") != replicas
        or status.get("updatedReplicas") != replicas
        or status.get("readyReplicas") != replicas
        or status.get("unavailableReplicas", 0) != 0
        or spec.get("selector")
        != {"matchLabels": {"app.kubernetes.io/name": "gmail-egress-proxy"}}
        or template_metadata.get("labels", {}).get("app.kubernetes.io/name")
        != "gmail-egress-proxy"
        or pod.get("automountServiceAccountToken") is not False
        or pod.get("hostNetwork", False) is not False
        or pod.get("hostPID", False) is not False
        or pod.get("hostIPC", False) is not False
        or pod.get("initContainers", []) != []
        or container.get("name") != "envoy"
        or container.get("image") != PROXY_IMAGE
        or container.get("args")
        != ["-c", "/etc/envoy/envoy.yaml", "--log-level", "critical"]
        or _mapping(container.get("securityContext"), "proxy_invalid")
        != {
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
        }
    ):
        raise PilotError("proxy_not_ready")
    _validate_proxy_pod_contract(pod, "proxy_not_ready")


def _validate_proxy_lineage(
    deployment: dict[str, object],
    replica_sets_resource: dict[str, object],
    pods_resource: dict[str, object],
) -> None:
    deployment_metadata = _mapping(deployment.get("metadata"), "proxy_lineage_invalid")
    deployment_uid = deployment_metadata.get("uid")
    if not isinstance(deployment_uid, str) or not deployment_uid:
        raise PilotError("proxy_lineage_invalid")
    active: list[dict[str, object]] = []
    for raw in _list(replica_sets_resource.get("items"), "proxy_lineage_invalid"):
        replica_set = _mapping(raw, "proxy_lineage_invalid")
        metadata = _mapping(replica_set.get("metadata"), "proxy_lineage_invalid")
        spec = _mapping(replica_set.get("spec"), "proxy_lineage_invalid")
        status = _mapping(replica_set.get("status", {}), "proxy_lineage_invalid")
        replicas = spec.get("replicas", 0)
        if replicas == 0:
            if status.get("replicas", 0) != 0:
                raise PilotError("proxy_lineage_invalid")
            continue
        owners = _list(metadata.get("ownerReferences"), "proxy_lineage_invalid")
        if (
            replicas != 1
            or status.get("replicas") != 1
            or status.get("readyReplicas") != 1
            or status.get("availableReplicas") != 1
            or len(owners) != 1
            or _mapping(owners[0], "proxy_lineage_invalid").get("uid") != deployment_uid
            or _mapping(owners[0], "proxy_lineage_invalid").get("controller")
            is not True
        ):
            raise PilotError("proxy_lineage_invalid")
        template = _mapping(spec.get("template"), "proxy_lineage_invalid")
        _validate_proxy_config_annotation(
            _mapping(template.get("metadata"), "proxy_lineage_invalid"),
            "proxy_lineage_invalid",
        )
        _validate_proxy_pod_contract(
            _mapping(template.get("spec"), "proxy_lineage_invalid"),
            "proxy_lineage_invalid",
        )
        active.append(replica_set)
    if len(active) != 1:
        raise PilotError("proxy_lineage_invalid")
    replica_set_metadata = _mapping(active[0].get("metadata"), "proxy_lineage_invalid")
    replica_set_uid = replica_set_metadata.get("uid")
    if not isinstance(replica_set_uid, str) or not replica_set_uid:
        raise PilotError("proxy_lineage_invalid")
    pods = _list(pods_resource.get("items"), "proxy_lineage_invalid")
    if len(pods) != 1:
        raise PilotError("proxy_lineage_invalid")
    pod = _mapping(pods[0], "proxy_lineage_invalid")
    pod_metadata = _mapping(pod.get("metadata"), "proxy_lineage_invalid")
    _validate_proxy_config_annotation(pod_metadata, "proxy_lineage_invalid")
    owners = _list(pod_metadata.get("ownerReferences"), "proxy_lineage_invalid")
    if (
        len(owners) != 1
        or _mapping(owners[0], "proxy_lineage_invalid").get("uid") != replica_set_uid
        or _mapping(owners[0], "proxy_lineage_invalid").get("controller") is not True
    ):
        raise PilotError("proxy_lineage_invalid")


def _validate_proxy_service(resource: dict[str, object]) -> None:
    spec = _mapping(resource.get("spec"), "proxy_service_invalid")
    ports = _list(spec.get("ports"), "proxy_service_invalid")
    if (
        spec.get("type") != "ClusterIP"
        or spec.get("selector") != {"app.kubernetes.io/name": "gmail-egress-proxy"}
        or len(ports) != 1
    ):
        raise PilotError("proxy_service_invalid")
    port = _mapping(ports[0], "proxy_service_invalid")
    if {key: port.get(key) for key in ("name", "port", "targetPort", "protocol")} != {
        "name": "proxy",
        "port": 10000,
        "targetPort": "proxy",
        "protocol": "TCP",
    }:
        raise PilotError("proxy_service_invalid")


def _validate_proxy_endpoints(
    resource: dict[str, object], pods_resource: dict[str, object], *, namespace: str
) -> None:
    valid_pods: dict[str, tuple[str, str]] = {}
    for raw_pod in _list(pods_resource.get("items"), "proxy_pods_invalid"):
        pod = _mapping(raw_pod, "proxy_pods_invalid")
        metadata = _mapping(pod.get("metadata"), "proxy_pods_invalid")
        spec = _mapping(pod.get("spec"), "proxy_pods_invalid")
        status = _mapping(pod.get("status"), "proxy_pods_invalid")
        containers = _list(spec.get("containers"), "proxy_pods_invalid")
        statuses = _list(status.get("containerStatuses"), "proxy_pods_invalid")
        if (
            metadata.get("labels", {}).get("app.kubernetes.io/name")
            != "gmail-egress-proxy"
            or not isinstance(metadata.get("name"), str)
            or not isinstance(metadata.get("uid"), str)
            or status.get("phase") != "Running"
            or not isinstance(status.get("podIP"), str)
            or not status["podIP"]
            or len(containers) != 1
            or _mapping(containers[0], "proxy_pods_invalid").get("image") != PROXY_IMAGE
            or spec.get("automountServiceAccountToken") is not False
            or spec.get("hostNetwork", False) is not False
            or spec.get("hostPID", False) is not False
            or spec.get("hostIPC", False) is not False
            or spec.get("initContainers", []) != []
            or len(statuses) != 1
            or _mapping(statuses[0], "proxy_pods_invalid").get("ready") is not True
            or _mapping(statuses[0], "proxy_pods_invalid").get("restartCount") != 0
            or not str(
                _mapping(statuses[0], "proxy_pods_invalid").get("imageID", "")
            ).endswith(PROXY_IMAGE.split("@sha256:", 1)[1])
        ):
            raise PilotError("proxy_pods_invalid")
        _validate_proxy_pod_contract(spec, "proxy_pods_invalid")
        valid_pods[metadata["name"]] = (metadata["uid"], status["podIP"])
    subsets = _list(resource.get("subsets"), "proxy_endpoints_not_ready")
    ready = 0
    for raw in subsets:
        subset = _mapping(raw, "proxy_endpoints_not_ready")
        if _list(subset.get("notReadyAddresses", []), "proxy_endpoints_not_ready"):
            raise PilotError("proxy_endpoints_not_ready")
        ports = _list(subset.get("ports"), "proxy_endpoints_not_ready")
        if len(ports) != 1:
            raise PilotError("proxy_endpoints_not_ready")
        port = _mapping(ports[0], "proxy_endpoints_not_ready")
        if {key: port.get(key) for key in ("name", "port", "protocol")} != {
            "name": "proxy",
            "port": 10000,
            "protocol": "TCP",
        }:
            raise PilotError("proxy_endpoints_not_ready")
        for raw_address in _list(subset.get("addresses"), "proxy_endpoints_not_ready"):
            address = _mapping(raw_address, "proxy_endpoints_not_ready")
            target = _mapping(address.get("targetRef"), "proxy_endpoints_not_ready")
            if (
                not isinstance(address.get("ip"), str)
                or not address["ip"]
                or target.get("kind") != "Pod"
                or target.get("namespace") != namespace
                or not isinstance(target.get("name"), str)
                or valid_pods.get(target["name"])
                != (target.get("uid"), address.get("ip"))
            ):
                raise PilotError("proxy_endpoints_not_ready")
            ready += 1
    if ready < 1:
        raise PilotError("proxy_endpoints_not_ready")


def _assert_no_pilot_jobs(resource: dict[str, object]) -> None:
    if _list(resource.get("items"), "jobs_invalid"):
        raise PilotError("pilot_job_already_exists")


def _assert_no_pilot_pods(resource: dict[str, object]) -> None:
    if _list(resource.get("items"), "pods_invalid"):
        raise PilotError("pilot_pod_already_exists")


def _validate_admission_controls(
    kubectl: KubectlProtocol,
    cronjob: dict[str, object],
    contract: PilotContract,
    *,
    namespace: str,
) -> tuple[str, str]:
    params = kubectl.get_json(
        "configmap", "gmail-full-pilot-admission-params", namespace=namespace
    )
    metadata = _mapping(params.get("metadata"), "pilot_admission_params_invalid")
    data = _mapping(params.get("data"), "pilot_admission_params_invalid")
    cron_spec = _mapping(cronjob.get("spec"), "pilot_admission_params_invalid")
    job_template = _mapping(
        cron_spec.get("jobTemplate"), "pilot_admission_params_invalid"
    )
    job_spec = _mapping(job_template.get("spec"), "pilot_admission_params_invalid")
    pod_template = _mapping(job_spec.get("template"), "pilot_admission_params_invalid")
    pod_spec = _mapping(pod_template.get("spec"), "pilot_admission_params_invalid")
    containers = _list(pod_spec.get("containers"), "pilot_admission_params_invalid")
    if len(containers) != 1:
        raise PilotError("pilot_admission_params_invalid")
    env = _environment(_mapping(containers[0], "pilot_admission_params_invalid"))
    expected_data = {
        "image": contract.image,
        "imageSourceRevision": contract.image_source_revision,
        "query": env["GMAIL_IMPORT_QUERY"].get("value"),
        "querySha256": contract.query_hash,
        "accountSha256": EXPECTED_ACCOUNT_SHA256,
    }
    uid = metadata.get("uid")
    version = metadata.get("resourceVersion")
    if (
        metadata.get("name") != "gmail-full-pilot-admission-params"
        or metadata.get("namespace") != namespace
        or not isinstance(uid, str)
        or not uid
        or not isinstance(version, str)
        or not version
        or data != expected_data
    ):
        raise PilotError("pilot_admission_params_invalid")
    kinds = {
        "validatingadmissionpolicy": "ValidatingAdmissionPolicy",
        "validatingadmissionpolicybinding": "ValidatingAdmissionPolicyBinding",
    }
    for (resource, name), expected_hash in ADMISSION_SPEC_SHA256.items():
        observed = kubectl.get_json(resource, name, namespace=namespace)
        observed_metadata = _mapping(
            observed.get("metadata"), "pilot_admission_policy_invalid"
        )
        spec = _mapping(observed.get("spec"), "pilot_admission_policy_invalid")
        spec_hash = hashlib.sha256(
            json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if (
            observed.get("apiVersion") != "admissionregistration.k8s.io/v1"
            or observed.get("kind") != kinds[resource]
            or observed_metadata.get("name") != name
            or spec_hash != expected_hash
        ):
            raise PilotError("pilot_admission_policy_invalid")
    return uid, version


def _run_preflight(
    kubectl: KubectlProtocol,
    *,
    namespace: str,
    check_pilot_absence: bool = True,
) -> tuple[PilotContract, dict[str, object]]:
    if DNS_LABEL_RE.fullmatch(namespace) is None:
        raise PilotError("namespace_invalid")
    kubectl.assert_permissions(namespace=namespace)
    cronjob = kubectl.get_json("cronjob", CRONJOB_NAME, namespace=namespace)
    contract = validate_cronjob(cronjob)
    oauth_keys, oauth_uid, oauth_version = kubectl.secret_contract(
        "gmail-oauth", namespace=namespace
    )
    if oauth_keys != set(OAUTH_SECRET_KEYS):
        raise PilotError("oauth_secret_contract_invalid")
    runtime_keys, runtime_uid, runtime_version = kubectl.secret_contract(
        "gmail-collector-db", namespace=namespace
    )
    if runtime_keys != set(RUNTIME_SECRET_KEYS):
        raise PilotError("runtime_secret_contract_invalid")
    admission_params_uid, admission_params_version = _validate_admission_controls(
        kubectl, cronjob, contract, namespace=namespace
    )
    revisions = {
        _validate_flux(
            kubectl.get_json("kustomization", name, namespace="flux-system"), name
        )
        for name in ("coop-root", "coop-database", "coop-migration", "coop-import")
    }
    source_revision = _validate_flux_source(
        kubectl.get_json(
            "gitrepository", "coop-video-planning", namespace="flux-system"
        )
    )
    revisions.add(source_revision)
    if len(revisions) != 1:
        raise PilotError("flux_revision_mismatch")
    flux_revision = revisions.pop()
    contract = replace(
        contract,
        flux_revision=flux_revision,
        oauth_secret_uid=oauth_uid,
        oauth_secret_version=oauth_version,
        runtime_secret_uid=runtime_uid,
        runtime_secret_version=runtime_version,
        admission_params_uid=admission_params_uid,
        admission_params_version=admission_params_version,
    )
    proxy_deployment = kubectl.get_json(
        "deployment", "gmail-egress-proxy", namespace=namespace
    )
    _validate_proxy(proxy_deployment)
    _validate_proxy_config(
        kubectl.get_json("configmap", "gmail-egress-proxy-config", namespace=namespace)
    )
    _validate_proxy_service(
        kubectl.get_json("service", "gmail-egress-proxy", namespace=namespace)
    )
    proxy_pods = kubectl.get_json(
        "pods",
        namespace=namespace,
        labels="app.kubernetes.io/name=gmail-egress-proxy",
    )
    proxy_replica_sets = kubectl.get_json(
        "replicasets",
        namespace=namespace,
        labels="app.kubernetes.io/name=gmail-egress-proxy",
    )
    _validate_proxy_lineage(proxy_deployment, proxy_replica_sets, proxy_pods)
    _validate_proxy_endpoints(
        kubectl.get_json("endpoints", "gmail-egress-proxy", namespace=namespace),
        proxy_pods,
        namespace=namespace,
    )
    policies = kubectl.get_json("networkpolicy", namespace=namespace)
    _validate_network_policies(policies)
    if check_pilot_absence:
        jobs = kubectl.get_json(
            "jobs", namespace=namespace, labels=f"{PILOT_LABEL}=true"
        )
        _assert_no_pilot_jobs(jobs)
        pods = kubectl.get_json(
            "pods", namespace=namespace, labels=f"{PILOT_LABEL}=true"
        )
        _assert_no_pilot_pods(pods)
        job_pods = kubectl.get_json(
            "pods", namespace=namespace, labels=f"job-name={PILOT_JOB_NAME}"
        )
        _assert_no_pilot_pods(job_pods)

    return contract, cronjob


def run_preflight(kubectl: KubectlProtocol, *, namespace: str) -> PilotContract:
    contract, _ = _run_preflight(kubectl, namespace=namespace)
    return contract


def _validated_report(raw: str) -> dict[str, object]:
    lines = [line for line in raw.splitlines() if line]
    if len(lines) != 1:
        raise PilotError("collector_output_invalid")
    try:
        report = json.loads(lines[0])
    except json.JSONDecodeError as error:
        raise PilotError("collector_output_invalid") from error
    if (
        not isinstance(report, dict)
        or set(report) != {"ok", "pending_review", "status"}
        or report.get("ok") is not True
        or report.get("status") != "SUCCEEDED"
        or not isinstance(report.get("pending_review"), int)
        or isinstance(report.get("pending_review"), bool)
        or report["pending_review"] < 0
    ):
        raise PilotError("collector_output_invalid")
    return report


def _validate_live_attestation(
    path: str,
    expected_digest: str,
    *,
    context: str,
    namespace: str,
    contract: PilotContract,
) -> int:
    if SHA256_RE.fullmatch(expected_digest) is None:
        raise PilotError("live_attestation_digest_invalid")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise PilotError("live_attestation_unreadable") from error
    try:
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            raw = stream.read(16_385)
    except OSError as error:
        raise PilotError("live_attestation_unreadable") from error
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or len(raw) > 16_384
        or hashlib.sha256(raw).hexdigest() != expected_digest
    ):
        raise PilotError("live_attestation_invalid")
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise PilotError("live_attestation_invalid") from error
    expected_keys = {
        "account_hash",
        "admission_params_uid",
        "admission_params_version",
        "approved_by",
        "collector_business_write_denied",
        "collector_login",
        "context",
        "evidence_id",
        "expires_at",
        "flux_revision",
        "image",
        "image_source_revision",
        "issued_at",
        "namespace",
        "network_allow",
        "network_deny",
        "oauth_secret_uid",
        "oauth_secret_version",
        "proxy_allow",
        "proxy_deny",
        "query_hash",
        "runtime_secret_uid",
        "runtime_secret_version",
        "schema",
        "source_enabled",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise PilotError("live_attestation_invalid")
    now = int(time.time())
    issued_at = value.get("issued_at")
    expires_at = value.get("expires_at")
    identity_fields = (value.get("approved_by"), value.get("evidence_id"))
    if (
        value.get("schema") != "coop.gmail-full-live-attestation/v1"
        or value.get("context") != context
        or value.get("namespace") != namespace
        or value.get("image") != contract.image
        or value.get("image_source_revision") != contract.image_source_revision
        or value.get("flux_revision") != contract.flux_revision
        or value.get("oauth_secret_uid") != contract.oauth_secret_uid
        or value.get("oauth_secret_version") != contract.oauth_secret_version
        or value.get("runtime_secret_uid") != contract.runtime_secret_uid
        or value.get("runtime_secret_version") != contract.runtime_secret_version
        or value.get("query_hash") != contract.query_hash
        or value.get("account_hash") != EXPECTED_ACCOUNT_SHA256
        or value.get("admission_params_uid") != contract.admission_params_uid
        or value.get("admission_params_version") != contract.admission_params_version
        or any(
            value.get(key) is not True
            for key in expected_keys
            if key.endswith(("_allow", "_deny"))
        )
        or value.get("source_enabled") is not True
        or value.get("collector_login") is not True
        or value.get("collector_business_write_denied") is not True
        or not isinstance(issued_at, int)
        or isinstance(issued_at, bool)
        or not isinstance(expires_at, int)
        or isinstance(expires_at, bool)
        or issued_at > now + 300
        or expires_at <= now
        or expires_at - issued_at > 3600
        or any(
            not isinstance(item, str)
            or not item
            or item != item.strip()
            or len(item) > 160
            for item in identity_fields
        )
    ):
        raise PilotError("live_attestation_invalid")
    return expires_at


def run_pilot(
    kubectl: KubectlProtocol,
    *,
    namespace: str,
    job_name: str,
    timeout: str,
    authorize: Callable[[PilotContract], int],
) -> dict[str, object]:
    _timeout_seconds(timeout)
    contract, cronjob = _run_preflight(kubectl, namespace=namespace)
    expires_at = authorize(contract)
    if (
        not isinstance(expires_at, int)
        or isinstance(expires_at, bool)
        or expires_at <= int(time.time())
    ):
        raise PilotError("live_attestation_expired")
    marker_token = secrets.token_hex(32)
    job_token = secrets.token_hex(32)
    # Close the attestation-to-launch window before consuming the permanent slot.
    launch_contract, launch_cronjob = _run_preflight(kubectl, namespace=namespace)
    if launch_contract != contract or launch_cronjob != cronjob:
        raise PilotError("pilot_prelaunch_drift")
    if expires_at <= int(time.time()):
        raise PilotError("live_attestation_expired")
    job = build_pilot_job(
        cronjob,
        job_name,
        job_token,
        oauth_secret_name=PILOT_OAUTH_SECRET_NAME,
        runtime_secret_name=PILOT_RUNTIME_SECRET_NAME,
    )
    report: dict[str, object] | None = None
    marker_uid: str | None = None
    job_uid: str | None = None

    pod_name: str | None = None
    pod_uid: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    failure: PilotError | None = None
    containment_failures: list[str] = []
    try:
        termination_signals = {signal.SIGINT, signal.SIGTERM}
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, termination_signals)
        try:
            marker_uid = kubectl.consume_pilot_slot(
                contract, marker_token, namespace=namespace
            )
            kubectl.assert_pilot_marker(marker_uid, namespace=namespace)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        try:
            if expires_at <= int(time.time()):
                raise PilotError("live_attestation_expired")
            job_uid = kubectl.create_json(job, namespace=namespace)
        except PilotJobAlreadyExists:
            raise
        except PilotError as error:
            deadline = time.monotonic() + 30
            observed: dict[str, object] | None = None
            while True:
                try:
                    observed = kubectl.get_job_optional(job_name, namespace=namespace)
                except (Exception, KeyboardInterrupt) as lookup_error:
                    raise PilotError(
                        f"pilot_job_uncertain:{job_name}"
                    ) from lookup_error
                if observed is not None or time.monotonic() >= deadline:
                    break
                time.sleep(1)
            if observed is None:
                raise PilotError(f"pilot_job_uncertain:{job_name}") from error
            metadata = _mapping(observed.get("metadata"), "pilot_job_uncertain")
            annotations = _mapping(metadata.get("annotations"), "pilot_job_uncertain")
            observed_uid = metadata.get("uid")
            if (
                annotations.get("coop.example.com/pilot-ownership-token") != job_token
                or not isinstance(observed_uid, str)
                or not observed_uid
            ):
                raise PilotError(f"pilot_job_uncertain:{job_name}") from error
            try:
                _validate_persisted_pilot_job(observed, job)
            except PilotError as validation_error:
                raise PilotError(
                    f"pilot_job_uncertain:{job_name}"
                ) from validation_error
            # A matching token and manifest are correlators, not proof that this UID
            # came from the create whose response was lost. Never adopt or delete an
            # unacknowledged UID; the observed Job is still suspended and requires
            # privileged operator inspection.
            raise PilotError(f"pilot_job_uncertain:{job_name}") from error
        if expires_at <= int(time.time()):
            raise PilotError("live_attestation_expired")
        activation_contract, activation_cronjob = _run_preflight(
            kubectl, namespace=namespace, check_pilot_absence=False
        )
        if activation_contract != contract or activation_cronjob != cronjob:
            raise PilotError("pilot_activation_drift")
        if marker_uid is None:
            raise PilotError("pilot_slot_uncertain")
        kubectl.assert_pilot_marker(marker_uid, namespace=namespace)
        kubectl.activate_job(job, job_uid, expires_at, namespace=namespace)
        if expires_at <= int(time.time()):
            raise PilotError("live_attestation_expired")
        started_at, completed_at = kubectl.wait_complete(
            job_name, job_uid, namespace=namespace, timeout=timeout
        )
        if expires_at <= int(time.time()):
            raise PilotError("live_attestation_expired")
        pod_name, pod_uid = kubectl.attest_single_pod(
            job_name, job_uid, contract.image, job, namespace=namespace
        )
        if expires_at <= int(time.time()):
            raise PilotError("live_attestation_expired")
        report = _validated_report(
            kubectl.logs(pod_name, pod_uid, job_uid, namespace=namespace)
        )
        if expires_at <= int(time.time()):
            raise PilotError("live_attestation_expired")
        kubectl.delete_job(job_name, job_uid, namespace=namespace, timeout="5m")
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - armed job cleanup
        failure = (
            error
            if isinstance(error, PilotError)
            else PilotError("pilot_execution_failed")
        )
        if job_uid is not None:
            try:
                kubectl.delete_job(job_name, job_uid, namespace=namespace, timeout="5m")
            except (Exception, KeyboardInterrupt):  # noqa: BLE001 - containment fallback
                code = f"pilot_job_uncertain:{job_name}"
                containment_failures.append(code)
                failure = PilotError(code)

    try:
        post_contract, _ = _run_preflight(kubectl, namespace=namespace)
        if post_contract != contract or marker_uid is None:
            raise PilotError("pilot_postcondition_failed")
        kubectl.assert_pilot_marker(marker_uid, namespace=namespace)
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - final attestation
        code = (
            str(error)
            if isinstance(error, PilotError)
            else "pilot_postcondition_failed"
        )
        containment_failures.append(code)
        if failure is None:
            failure = error if isinstance(error, PilotError) else PilotError(code)
    if containment_failures:
        raise PilotError(
            "pilot_containment_failed:" + ",".join(dict.fromkeys(containment_failures))
        )
    if failure is not None:
        raise failure
    if expires_at <= int(time.time()):
        raise PilotError("live_attestation_expired")
    if (
        report is None
        or job_uid is None
        or pod_name is None
        or pod_uid is None
        or started_at is None
        or completed_at is None
    ):
        raise PilotError("pilot_execution_failed")
    return {
        "completed": True,
        "completed_at": completed_at,
        "flux_revision": contract.flux_revision,
        "image": contract.image,
        "image_source_revision": contract.image_source_revision,
        "job": job_name,
        "job_uid": job_uid,
        "ok": True,
        "pending_review": report["pending_review"],
        "pod": pod_name,
        "pod_uid": pod_uid,
        "query_hash": contract.query_hash,
        "started_at": started_at,
        "status": "SUCCEEDED",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail-closed Gmail FULL pilot controller"
    )
    parser.add_argument("command", choices=("preflight", "run"))
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", default="coop-prod")
    parser.add_argument("--timeout", default="70m")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--live-attestation")
    parser.add_argument("--live-attestation-sha256")
    arguments = parser.parse_args(argv)
    try:
        kubectl = Kubectl(arguments.context)
        if arguments.command == "preflight":
            contract = run_preflight(kubectl, namespace=arguments.namespace)
            result: dict[str, object] = {
                "flux_revision": contract.flux_revision,
                "image": contract.image,
                "image_source_revision": contract.image_source_revision,
                "ok": True,
                "query_hash": contract.query_hash,
                "status": "READY",
            }
        else:
            if not arguments.yes:
                raise PilotError("explicit_confirmation_required")
            if not arguments.live_attestation or not arguments.live_attestation_sha256:
                raise PilotError("live_attestation_required")

            def authorize(contract: PilotContract) -> int:
                return _validate_live_attestation(
                    arguments.live_attestation,
                    arguments.live_attestation_sha256,
                    context=arguments.context,
                    namespace=arguments.namespace,
                    contract=contract,
                )

            previous_handlers: dict[int, Any] = {}

            def terminate(signum: int, _frame: object) -> None:
                # The first termination request enters the UID-safe containment path.
                # Ignore subsequent signals until cleanup finishes, then restore handlers.
                for guarded_signal in (signal.SIGINT, signal.SIGTERM):
                    signal.signal(guarded_signal, signal.SIG_IGN)
                raise PilotError(f"pilot_termination_requested:{signum}")

            for signal_number in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signal_number] = signal.signal(
                    signal_number, terminate
                )
            try:
                result = run_pilot(
                    kubectl,
                    namespace=arguments.namespace,
                    job_name=PILOT_JOB_NAME,
                    timeout=arguments.timeout,
                    authorize=authorize,
                )
            finally:
                for restore_number, previous_handler in previous_handlers.items():
                    signal.signal(restore_number, previous_handler)
    except PilotError as error:
        print(
            json.dumps(
                {"error": str(error), "ok": False, "status": "FAILED"},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
