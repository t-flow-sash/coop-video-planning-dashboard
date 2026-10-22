from __future__ import annotations

import json
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
RESTORE = ROOT / "scripts/restore-postgres.sh"
BOOTSTRAP = ROOT / "scripts/bootstrap-production-host.sh"
BACKUP = ROOT / "infrastructure/backup/backup-cronjob.yaml"


class RestoreScriptTests(unittest.TestCase):
    flux_objects = [
        "gitrepository/coop-video-planning",
        "kustomization/coop-root",
        "kustomization/coop-database",
        "kustomization/coop-migration",
        "kustomization/coop-import",
        "kustomization/coop-semantic",
        "kustomization/coop-apps",
        "kustomization/coop-ingress",
        "kustomization/coop-backup",
    ]

    def run_restore(
        self,
        *,
        active_writer: bool = False,
        pending_writer: bool = False,
        fail_exec: bool = False,
        fail_active_check: bool = False,
        lock_collision: bool = False,
        stale_reconcile: bool = False,
        stale_marker_resource: str | None = None,
        stale_observed_generation: bool = False,
        root_stale_observed_generation: bool = False,
        root_not_ready: bool = False,
        initially_suspended: str | None = None,
        import_reconcile_polls: int = 0,
        ignore_delta_quarantine_patch: bool = False,
        lose_delta_quarantine_on_import_resume: bool = False,
        ignore_semantic_quarantine_patch: bool = False,
        suspension_ack_polls: int = 1,
        api_termination_stuck: bool = False,
        lock_create_readback_mismatch: bool = False,
        lock_recreated_before_cleanup: bool = False,
        lock_lost_before_cleanup: bool = False,
        lock_replaced_during_delete: bool = False,
        lock_mutated_before_cleanup: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], list[list[str]], dict[str, Any]]:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            state_file = temp / "state.json"
            log_file = temp / "kubectl.jsonl"
            suspended = {
                **{item: item == initially_suspended for item in self.flux_objects},
                "cronjob/postgres-backup": False,
                # Model the dangerous live drift from the declarative
                # suspend:true contract: restore must close this boundary.
                "cronjob/coop-seed": False,
                "cronjob/gmail-import-full": False,
                "cronjob/gmail-import-bootstrap": False,
                "cronjob/gmail-import-delta": False,
                "cronjob/gmail-semantic-worker": False,
            }
            state_file.write_text(
                json.dumps(
                    {
                        "suspended": suspended,
                        "annotations": {},
                        "import_reconcile_reads": 0,
                        "replicas": 2,
                        "api_pods": 2,
                        "lock": None,
                        "lock_reads": 0,
                        "generation": {item: 1 for item in self.flux_objects},
                        "observed_generation": {item: 1 for item in self.flux_objects},
                        "last_handled": {item: "old-request" for item in self.flux_objects},
                        "ready": {item: True for item in self.flux_objects},
                        "suspension_reads": {item: 0 for item in self.flux_objects},
                    }
                ),
                encoding="utf-8",
            )
            kubectl = temp / "kubectl"
            kubectl.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json, os, signal, socket, sys
                    from pathlib import Path

                    args = sys.argv[1:]
                    state_path = Path(os.environ["MOCK_STATE"])
                    log_path = Path(os.environ["MOCK_LOG"])
                    state = json.loads(state_path.read_text())
                    with log_path.open("a") as stream:
                        stream.write(json.dumps(args) + "\\n")

                    def save():
                        state_path.write_text(json.dumps(state))

                    if "proxy" in args:
                        socket_arg = next(
                            item for item in args if item.startswith("--unix-socket=")
                        )
                        socket_path = socket_arg.split("=", 1)[1]
                        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        server.bind(socket_path)
                        server.listen(1)
                        signal.pause()

                    if "get" in args and "deployment" in args and "coop-api" in args:
                        print(state["replicas"], end="")
                    elif "get" in args and ("kustomization" in args or "gitrepository" in args or "cronjob" in args):
                        if "kustomization" in args:
                            kind = "kustomization"
                        elif "gitrepository" in args:
                            kind = "gitrepository"
                        else:
                            kind = "cronjob"
                        name = args[args.index(kind) + 1]
                        resource = f"{kind}/{name}"
                        if "-o" in args and args[args.index("-o") + 1] == "json":
                            if (
                                resource in state["generation"]
                                and state["suspended"][resource]
                                and state["observed_generation"][resource] < state["generation"][resource]
                            ):
                                state["suspension_reads"][resource] += 1
                                if state["suspension_reads"][resource] >= int(os.environ.get("MOCK_SUSPENSION_ACK_POLLS", "1")):
                                    state["observed_generation"][resource] = state["generation"][resource]
                                save()
                            reconciling = False
                            if (
                                resource == "kustomization/coop-import"
                                and state["suspended"][resource]
                                and state["import_reconcile_reads"] < int(os.environ.get("MOCK_IMPORT_RECONCILE_POLLS", "0"))
                            ):
                                state["import_reconcile_reads"] += 1
                                state["suspended"]["cronjob/gmail-import-delta"] = False
                                reconciling = True
                                save()
                            print(json.dumps({
                                "metadata": {
                                    "generation": state["generation"].get(resource, 1),
                                    "annotations": state["annotations"].get(resource, {}),
                                },
                                "spec": {"suspend": state["suspended"][resource]},
                                "status": {
                                    "observedGeneration": state["observed_generation"].get(resource, 1),
                                    "lastHandledReconcileAt": state["last_handled"].get(resource, ""),
                                    "conditions": [
                                        {"type": "Ready", "status": "True" if state["ready"].get(resource, True) else "False"},
                                        {"type": "Reconciling", "status": "True" if reconciling else "False"},
                                    ],
                                },
                            }))
                        else:
                            print(str(state["suspended"][resource]).lower(), end="")
                    elif "patch" in args and ("kustomization" in args or "gitrepository" in args or "cronjob" in args):
                        if "kustomization" in args:
                            kind = "kustomization"
                        elif "gitrepository" in args:
                            kind = "gitrepository"
                        else:
                            kind = "cronjob"
                        name = args[args.index(kind) + 1]
                        resource = f"{kind}/{name}"
                        payload = args[args.index("-p") + 1]
                        patch = json.loads(payload)
                        new_suspend = state["suspended"][resource]
                        if "suspend" in patch.get("spec", {}):
                            new_suspend = patch["spec"]["suspend"]
                            if kind != "cronjob" and state["suspended"][resource] != new_suspend:
                                state["generation"][resource] += 1
                            state["suspended"][resource] = new_suspend
                        state["annotations"].setdefault(resource, {}).update(
                            patch.get("metadata", {}).get("annotations", {})
                        )
                        if (
                            resource == "cronjob/gmail-import-delta"
                            and os.environ.get("MOCK_IGNORE_DELTA_QUARANTINE_PATCH") == "1"
                        ):
                            state["annotations"][resource].pop(
                                "kustomize.toolkit.fluxcd.io/reconcile", None
                            )
                        if (
                            resource == "cronjob/gmail-semantic-worker"
                            and os.environ.get("MOCK_IGNORE_SEMANTIC_QUARANTINE_PATCH") == "1"
                        ):
                            state["annotations"][resource].pop(
                                "kustomize.toolkit.fluxcd.io/reconcile", None
                            )
                        if (
                            resource == "kustomization/coop-import"
                            and new_suspend is False
                            and os.environ.get("MOCK_LOSE_DELTA_QUARANTINE") == "1"
                        ):
                            delta = "cronjob/gmail-import-delta"
                            state["suspended"][delta] = False
                            state["annotations"].setdefault(delta, {}).pop(
                                "kustomize.toolkit.fluxcd.io/reconcile", None
                            )
                        if (
                            resource == "kustomization/coop-root"
                            and not new_suspend
                        ):
                            if os.environ.get("MOCK_ROOT_STALE_OBSERVED_GENERATION") != "1":
                                state["observed_generation"][resource] = state["generation"][resource]
                            state["ready"][resource] = os.environ.get("MOCK_ROOT_NOT_READY") != "1"
                        save()
                        if "-o" in args:
                            print(state["generation"][resource], end="")
                    elif "get" in args and "jobs" in args:
                        if not all(state["suspended"].values()):
                            print("ACTIVE_CHECK_BEFORE_SUSPEND", file=sys.stderr)
                            sys.exit(90)
                        if os.environ.get("MOCK_FAIL_ACTIVE_CHECK") == "1":
                            sys.exit(52)
                        jobs = []
                        if os.environ.get("MOCK_ACTIVE_WRITER") == "1":
                            jobs.append({"metadata": {"name": "writer-job"}, "status": {"active": 1}})
                        if os.environ.get("MOCK_PENDING_WRITER") == "1":
                            jobs.append({"metadata": {"name": "pending-writer"}, "status": {}})
                        print(json.dumps({"items": jobs}))
                    elif "create" in args and "configmap" in args:
                        if os.environ.get("MOCK_LOCK_COLLISION") == "1":
                            print("Error from server (AlreadyExists)", file=sys.stderr)
                            sys.exit(1)
                        holder_arg = next(item for item in args if item.startswith("--from-literal=holder="))
                        state["lock"] = {
                            "uid": "lock-uid-1",
                            "holder": holder_arg.split("=", 2)[2],
                            "resourceVersion": "1",
                        }
                        save()
                        if "-o" in args and args[args.index("-o") + 1] == "json":
                            print(json.dumps({
                                "metadata": {
                                    "uid": state["lock"]["uid"],
                                    "resourceVersion": state["lock"]["resourceVersion"],
                                },
                                "data": {"holder": state["lock"]["holder"]},
                            }))
                    elif "get" in args and "configmap" in args:
                        state["lock_reads"] += 1
                        if (
                            state["lock_reads"] == 1
                            and os.environ.get("MOCK_LOCK_CREATE_READBACK_MISMATCH") == "1"
                        ) or (
                            state["lock_reads"] == 2
                            and os.environ.get("MOCK_LOCK_RECREATED_BEFORE_CLEANUP") == "1"
                        ):
                            state["lock"] = {"uid": "foreign-uid", "holder": "foreign-holder", "resourceVersion": "2"}
                        if (
                            state["lock_reads"] == 2
                            and os.environ.get("MOCK_LOCK_LOST_BEFORE_CLEANUP") == "1"
                        ):
                            state["lock"] = None
                        if (
                            state["lock_reads"] == 2
                            and os.environ.get("MOCK_LOCK_MUTATED_BEFORE_CLEANUP") == "1"
                            and state["lock"] is not None
                        ):
                            state["lock"]["resourceVersion"] = "2"
                        save()
                        if state["lock"] is None:
                            print("Error from server (NotFound)", file=sys.stderr)
                            sys.exit(1)
                        print(json.dumps({
                            "metadata": {
                                "uid": state["lock"]["uid"],
                                "resourceVersion": state["lock"]["resourceVersion"],
                            },
                            "data": {"holder": state["lock"]["holder"]},
                        }))
                    elif "delete" in args and "configmap" in args:
                        print("error: unknown flag: --preconditions", file=sys.stderr)
                        sys.exit(1)
                    elif "create" in args and "-f" in args and "-" in args:
                        sys.stdin.read()
                        print("coop-restore-generated", end="")
                    elif "annotate" in args:
                        resource = next(word for word in args if "/" in word and not word.startswith("reconcile."))
                        requested = next(word for word in args if word.startswith("reconcile.fluxcd.io/requestedAt=")).split("=", 1)[1]
                        self_applied_root = (
                            resource == "kustomization/coop-root"
                        )
                        if not self_applied_root:
                            marker_is_stale = (
                                os.environ.get("MOCK_STALE_RECONCILE") == "1"
                                or os.environ.get("MOCK_STALE_MARKER_RESOURCE") == resource
                            )
                            if not marker_is_stale:
                                state["last_handled"][resource] = requested
                            if os.environ.get("MOCK_STALE_OBSERVED_GENERATION") != "1":
                                state["observed_generation"][resource] = state["generation"][resource]
                            state["ready"][resource] = True
                            save()
                    elif "scale" in args:
                        replica_arg = next(item for item in args if item.startswith("--replicas="))
                        state["replicas"] = int(replica_arg.split("=", 1)[1])
                        if state["replicas"] > 0:
                            state["api_pods"] = state["replicas"]
                        save()
                    elif "rollout" in args and "status" in args and "deployment/coop-api" in args:
                        pass
                    elif "delete" in args and "pods" in args and "-l" in args:
                        if os.environ.get("MOCK_API_TERMINATION_STUCK") != "1":
                            state["api_pods"] = 0
                            save()
                        else:
                            sys.exit(53)
                    elif "wait" in args and "--for=delete" in args and "pod" in args:
                        if state["api_pods"] != 0:
                            sys.exit(53)
                    elif "get" in args and "pods" in args and "-l" in args:
                        print(json.dumps({
                            "items": [
                                {
                                    "metadata": {
                                        "name": f"coop-api-{index}",
                                        "deletionTimestamp": "2026-01-01T00:00:00Z",
                                    }
                                }
                                for index in range(state["api_pods"])
                            ]
                        }))
                    elif "exec" in args and os.environ.get("MOCK_FAIL_EXEC") == "1":
                        sys.exit(41)
                    sys.exit(0)
                    """
                ),
                encoding="utf-8",
            )
            kubectl.chmod(0o755)
            curl = temp / "curl"
            curl.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json, os, sys
                    from pathlib import Path

                    args = sys.argv[1:]
                    body = sys.stdin.read()
                    state_path = Path(os.environ["MOCK_STATE"])
                    log_path = Path(os.environ["MOCK_LOG"])
                    with log_path.open("a") as stream:
                        stream.write(json.dumps(["curl", *args, body]) + "\\n")
                    state = json.loads(state_path.read_text())
                    expected_url = (
                        "http://localhost/api/v1/namespaces/coop-prod/configmaps/"
                        "coop-postgres-restore-lock"
                    )
                    try:
                        delete_options = json.loads(body)
                    except json.JSONDecodeError:
                        sys.exit(64)
                    if (
                        "--unix-socket" not in args
                        or "DELETE" not in args
                        or expected_url not in args
                        or delete_options.get("kind") != "DeleteOptions"
                        or delete_options.get("apiVersion") != "v1"
                    ):
                        sys.exit(64)
                    if os.environ.get("MOCK_LOCK_REPLACED_DURING_DELETE") == "1":
                        state["lock"] = {
                            "uid": "foreign-uid",
                            "holder": "foreign-holder",
                            "resourceVersion": "2",
                        }
                    preconditions = delete_options.get("preconditions", {})
                    if (
                        state["lock"] is None
                        or preconditions.get("uid") != state["lock"]["uid"]
                        or preconditions.get("resourceVersion")
                        != state["lock"]["resourceVersion"]
                    ):
                        state_path.write_text(json.dumps(state))
                        sys.exit(22)
                    state["lock"] = None
                    state_path.write_text(json.dumps(state))
                    """
                ),
                encoding="utf-8",
            )
            curl.chmod(0o755)
            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{temp}:{env['PATH']}",
                    "MOCK_STATE": str(state_file),
                    "MOCK_LOG": str(log_file),
                    "MOCK_ACTIVE_WRITER": "1" if active_writer else "0",
                    "MOCK_PENDING_WRITER": "1" if pending_writer else "0",
                    "MOCK_FAIL_EXEC": "1" if fail_exec else "0",
                    "MOCK_FAIL_ACTIVE_CHECK": "1" if fail_active_check else "0",
                    "MOCK_LOCK_COLLISION": "1" if lock_collision else "0",
                    "MOCK_STALE_RECONCILE": "1" if stale_reconcile else "0",
                    "MOCK_STALE_MARKER_RESOURCE": stale_marker_resource or "",
                    "MOCK_STALE_OBSERVED_GENERATION": "1" if stale_observed_generation else "0",
                    "MOCK_ROOT_STALE_OBSERVED_GENERATION": "1" if root_stale_observed_generation else "0",
                    "MOCK_ROOT_NOT_READY": "1" if root_not_ready else "0",
                    "MOCK_IMPORT_RECONCILE_POLLS": str(import_reconcile_polls),
                    "MOCK_SUSPENSION_ACK_POLLS": str(suspension_ack_polls),
                    "MOCK_IGNORE_DELTA_QUARANTINE_PATCH": "1"
                    if ignore_delta_quarantine_patch
                    else "0",
                    "MOCK_LOSE_DELTA_QUARANTINE": "1"
                    if lose_delta_quarantine_on_import_resume
                    else "0",
                    "MOCK_API_TERMINATION_STUCK": "1" if api_termination_stuck else "0",
                    "MOCK_LOCK_CREATE_READBACK_MISMATCH": "1"
                    if lock_create_readback_mismatch
                    else "0",
                    "MOCK_LOCK_RECREATED_BEFORE_CLEANUP": "1"
                    if lock_recreated_before_cleanup
                    else "0",
                    "MOCK_LOCK_LOST_BEFORE_CLEANUP": "1" if lock_lost_before_cleanup else "0",
                    "MOCK_LOCK_REPLACED_DURING_DELETE": "1"
                    if lock_replaced_during_delete
                    else "0",
                    "MOCK_LOCK_MUTATED_BEFORE_CLEANUP": "1"
                    if lock_mutated_before_cleanup
                    else "0",
                    "RECONCILE_ATTEMPTS": "2",
                    "RECONCILE_POLL_SECONDS": "0",
                }
            )
            result = subprocess.run(
                [str(RESTORE), "daily-20260101T011700Z.dump", "--yes"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            calls = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
            state = json.loads(state_file.read_text(encoding="utf-8"))
            return result, calls, state

    @staticmethod
    def operation(call: list[str]) -> str:
        return " ".join(call)

    def test_suspends_and_verifies_every_flux_writer_boundary_before_active_check(self) -> None:
        result, calls, _ = self.run_restore(active_writer=True)
        rendered = [self.operation(call) for call in calls]
        active_index = next(index for index, call in enumerate(rendered) if " get jobs " in f" {call} ")
        for resource in self.flux_objects:
            kind, name = resource.split("/", 1)
            suspend = next(
                index
                for index, call in enumerate(rendered)
                if f"patch {kind} {name}" in call and '"suspend":true' in call
            )
            verify = next(
                index
                for index, call in enumerate(rendered)
                if index > suspend and f"get {kind} {name}" in call
            )
            self.assertLess(suspend, verify, resource)
            self.assertLess(verify, active_index, resource)
        cron_suspend = next(
            index
            for index, call in enumerate(rendered)
            if "patch cronjob postgres-backup" in call and '"suspend":true' in call
        )
        cron_verify = next(
            index
            for index, call in enumerate(rendered)
            if index > cron_suspend and "get cronjob postgres-backup" in call
        )
        self.assertLess(cron_verify, active_index)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("ACTIVE_CHECK_BEFORE_SUSPEND", result.stderr)

    def test_refuses_restore_until_flux_acknowledges_suspension_generation(self) -> None:
        result, calls, _ = self.run_restore(suspension_ack_polls=3)
        rendered = [self.operation(call) for call in calls]

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Timed out waiting for Flux quiescence", result.stderr)
        self.assertIn("could not prove the fail-closed quarantine", result.stderr)
        self.assertFalse(any(" get jobs " in f" {call} " for call in rendered))
        self.assertFalse(
            any("delete configmap coop-postgres-restore-lock" in call for call in rendered)
        )

    def test_unsuspended_seed_is_suspended_and_verified_before_first_writer_check(self) -> None:
        result, calls, state = self.run_restore(active_writer=True)
        rendered = [self.operation(call) for call in calls]
        writer_check = next(index for index, call in enumerate(rendered) if " get jobs " in f" {call} ")
        suspend = next(
            index
            for index, call in enumerate(rendered)
            if "patch cronjob coop-seed" in call and '"suspend":true' in call
        )
        verify = next(
            index
            for index, call in enumerate(rendered)
            if index > suspend and "get cronjob coop-seed" in call
        )
        self.assertLess(suspend, verify)
        self.assertLess(verify, writer_check)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(state["suspended"]["cronjob/coop-seed"])

    def test_seed_remains_suspended_after_success_while_backup_is_restored(self) -> None:
        result, calls, state = self.run_restore()
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = [self.operation(call) for call in calls]
        self.assertFalse(
            any("patch cronjob coop-seed" in call and '"suspend":false' in call for call in rendered)
        )
        self.assertTrue(state["suspended"]["cronjob/coop-seed"])
        self.assertFalse(state["suspended"]["cronjob/postgres-backup"])

    def test_gmail_full_cronjob_is_verified_and_remains_suspended(self) -> None:
        result, calls, state = self.run_restore()
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = [self.operation(call) for call in calls]
        first_writer_check = next(
            index for index, call in enumerate(rendered) if " get jobs " in f" {call} "
        )
        for name in ("gmail-import-full",):
            suspend = next(
                index
                for index, call in enumerate(rendered)
                if f"patch cronjob {name}" in call and '\"suspend\":true' in call
            )
            verify = next(
                index
                for index, call in enumerate(rendered)
                if index > suspend and f"get cronjob {name}" in call
            )
            self.assertLess(verify, first_writer_check, name)
            self.assertFalse(
                any(f"patch cronjob {name}" in call and '\"suspend\":false' in call for call in rendered)
            )
            self.assertTrue(state["suspended"][f"cronjob/{name}"])

    def test_success_quarantines_delta_from_flux_before_import_reconcile(self) -> None:
        result, calls, state = self.run_restore(import_reconcile_polls=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = [self.operation(call) for call in calls]
        quarantine = next(
            index
            for index, call in enumerate(rendered)
            if "patch cronjob gmail-import-delta" in call
            and "kustomize.toolkit.fluxcd.io/reconcile" in call
        )
        verification = next(
            index
            for index, call in enumerate(rendered)
            if index > quarantine and "get cronjob gmail-import-delta -o json" in call
        )
        writer_check = next(
            index for index, call in enumerate(rendered) if " get jobs " in f" {call} "
        )
        import_resume = next(
            index
            for index, call in enumerate(rendered)
            if "patch kustomization coop-import" in call and '"suspend":false' in call
        )
        self.assertLess(verification, writer_check)
        self.assertLess(verification, import_resume)
        self.assertTrue(state["suspended"]["cronjob/gmail-import-delta"])
        self.assertEqual(state["import_reconcile_reads"], 1)
        self.assertEqual(
            state["annotations"]["cronjob/gmail-import-delta"][
                "kustomize.toolkit.fluxcd.io/reconcile"
            ],
            "disabled",
        )

    def test_quarantine_readback_failure_aborts_before_writer_check(self) -> None:
        result, calls, state = self.run_restore(ignore_delta_quarantine_patch=True)
        rendered = [self.operation(call) for call in calls]
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Failed to establish Flux quarantine", result.stderr)
        self.assertIn("could not prove the fail-closed quarantine", result.stderr)
        self.assertFalse(any(" get jobs " in f" {call} " for call in rendered))
        self.assertFalse(
            any("delete configmap coop-postgres-restore-lock" in call for call in rendered)
        )
        self.assertTrue(all(state["suspended"].values()))
        self.assertEqual(state["replicas"], 0)
        self.assertNotIn("Restore completed", result.stdout)

    def test_quarantine_loss_aborts_immediately_after_import_reconcile(self) -> None:
        result, calls, state = self.run_restore(
            lose_delta_quarantine_on_import_resume=True
        )
        rendered = [self.operation(call) for call in calls]
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("was lost during coop-import reconciliation", result.stderr)
        import_resume = next(
            index
            for index, call in enumerate(rendered)
            if "patch kustomization coop-import" in call and '"suspend":false' in call
        )
        self.assertFalse(
            any(
                index > import_resume
                and "patch kustomization coop-apps" in call
                and '"suspend":false' in call
                for index, call in enumerate(rendered)
            )
        )
        self.assertTrue(state["suspended"]["cronjob/gmail-import-delta"])
        self.assertEqual(
            state["annotations"]["cronjob/gmail-import-delta"][
                "kustomize.toolkit.fluxcd.io/reconcile"
            ],
            "disabled",
        )
        self.assertNotIn("Restore completed", result.stdout)

    def test_seed_remains_suspended_on_restore_failure(self) -> None:
        result, _, state = self.run_restore(fail_exec=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(state["suspended"]["cronjob/coop-seed"])

    def test_active_writer_aborts_before_restore_and_leaves_everything_suspended(self) -> None:
        result, calls, state = self.run_restore(active_writer=True)
        rendered = [self.operation(call) for call in calls]
        self.assertEqual(result.returncode, 4)
        self.assertIn("active or pending PostgreSQL writer Job", result.stderr)
        self.assertFalse(any(" apply -f -" in f" {call} " for call in rendered))
        self.assertFalse(any(" exec " in f" {call} " for call in rendered))
        self.assertTrue(all(state["suspended"].values()))
        self.assertEqual(state["replicas"], 0)

    def test_pending_writer_is_dangerous_and_aborts_before_restore(self) -> None:
        result, calls, state = self.run_restore(pending_writer=True)
        rendered = [self.operation(call) for call in calls]
        self.assertEqual(result.returncode, 4)
        self.assertIn("pending-writer", result.stderr)
        self.assertFalse(any(" create -f -" in f" {call} " for call in rendered))
        self.assertFalse(any(" exec " in f" {call} " for call in rendered))
        self.assertTrue(all(state["suspended"].values()))
        self.assertEqual(state["replicas"], 0)

    def test_atomic_lock_collision_aborts_without_mutating_cluster(self) -> None:
        result, calls, state = self.run_restore(lock_collision=True)
        rendered = [self.operation(call) for call in calls]
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restore lock", result.stderr)
        self.assertFalse(any(" patch " in f" {call} " for call in rendered))
        self.assertFalse(any(" scale " in f" {call} " for call in rendered))
        self.assertFalse(any(" create -f -" in f" {call} " for call in rendered))
        self.assertFalse(any("delete configmap coop-postgres-restore-lock" in call for call in rendered))
        self.assertEqual(state["replicas"], 2)

    def test_lock_create_requires_uid_and_holder_readback(self) -> None:
        result, calls, state = self.run_restore(lock_create_readback_mismatch=True)
        rendered = [self.operation(call) for call in calls]

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Could not verify ownership of PostgreSQL restore lock", result.stderr)
        self.assertEqual(state["lock"], {"uid": "foreign-uid", "holder": "foreign-holder", "resourceVersion": "2"})
        self.assertTrue(
            any("get configmap coop-postgres-restore-lock -o json" in call for call in rendered)
        )
        self.assertFalse(any(" patch " in f" {call} " for call in rendered))
        self.assertFalse(any(" scale " in f" {call} " for call in rendered))
        self.assertFalse(
            any("delete configmap coop-postgres-restore-lock" in call for call in rendered)
        )

    def test_recreated_lock_is_preserved_and_reported_fail_closed(self) -> None:
        result, calls, state = self.run_restore(lock_recreated_before_cleanup=True)
        rendered = [self.operation(call) for call in calls]

        self.assertEqual(result.returncode, 11)
        self.assertIn("lock ownership was lost", result.stderr)
        self.assertIn("fail-closed", result.stderr)
        self.assertEqual(state["lock"], {"uid": "foreign-uid", "holder": "foreign-holder", "resourceVersion": "2"})
        self.assertFalse(
            any("delete configmap coop-postgres-restore-lock" in call for call in rendered)
        )

    def test_lost_lock_is_not_name_deleted_and_is_reported_fail_closed(self) -> None:
        result, calls, state = self.run_restore(lock_lost_before_cleanup=True)
        rendered = [self.operation(call) for call in calls]

        self.assertEqual(result.returncode, 11)
        self.assertIn("lock ownership was lost", result.stderr)
        self.assertIn("fail-closed", result.stderr)
        self.assertIsNone(state["lock"])
        self.assertFalse(
            any("delete configmap coop-postgres-restore-lock" in call for call in rendered)
        )

    def test_uid_precondition_preserves_lock_replaced_during_delete(self) -> None:
        result, calls, state = self.run_restore(lock_replaced_during_delete=True)
        rendered = [self.operation(call) for call in calls]

        self.assertEqual(result.returncode, 11)
        self.assertIn("UID precondition", result.stderr)
        self.assertIn("fail-closed", result.stderr)
        self.assertEqual(state["lock"], {"uid": "foreign-uid", "holder": "foreign-holder", "resourceVersion": "2"})
        self.assertTrue(
            any(
                call.startswith("curl ")
                and '"uid": "lock-uid-1"' in call
                and '"resourceVersion": "1"' in call
                for call in rendered
            )
        )

    def test_resource_version_change_preserves_mutated_lock(self) -> None:
        result, calls, state = self.run_restore(lock_mutated_before_cleanup=True)
        rendered = [self.operation(call) for call in calls]

        self.assertEqual(result.returncode, 11)
        self.assertIn("lock ownership was lost", result.stderr)
        self.assertEqual(state["lock"]["resourceVersion"], "2")
        self.assertFalse(
            any("delete configmap coop-postgres-restore-lock" in call for call in rendered)
        )

    def test_failed_quarantine_does_not_claim_a_lost_lock_remains_held(self) -> None:
        result, _, state = self.run_restore(
            active_writer=True,
            api_termination_stuck=True,
            lock_lost_before_cleanup=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIsNone(state["lock"])
        self.assertIn("lock ownership was lost", result.stderr)
        self.assertNotIn("restore lock remains held", result.stderr)

    def test_pre_suspended_dependency_aborts_before_restore_or_api_scale_up(self) -> None:
        result, calls, state = self.run_restore(initially_suspended="kustomization/coop-migration")
        rendered = [self.operation(call) for call in calls]
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already suspended", result.stderr)
        self.assertFalse(any(" create -f -" in f" {call} " for call in rendered))
        self.assertFalse(any(" exec " in f" {call} " for call in rendered))
        self.assertFalse(any("--replicas=2" in call for call in rendered))
        self.assertNotIn("Restore completed", result.stdout)
        self.assertEqual(state["replicas"], 2)

    def test_active_check_failure_aborts_fail_closed(self) -> None:
        result, calls, state = self.run_restore(fail_active_check=True)
        rendered = [self.operation(call) for call in calls]
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Could not determine active PostgreSQL writer Jobs", result.stderr)
        self.assertFalse(any(" apply -f -" in f" {call} " for call in rendered))
        self.assertTrue(all(state["suspended"].values()))
        self.assertEqual(state["replicas"], 0)

    def test_restore_failure_keeps_root_children_source_suspended_and_api_zero(self) -> None:
        result, calls, state = self.run_restore(fail_exec=True)
        rendered = [self.operation(call) for call in calls]
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(all(state["suspended"].values()))
        self.assertEqual(state["replicas"], 0)
        self.assertFalse(any('"suspend":false' in call for call in rendered))

    def test_failure_quarantine_retains_lock_until_api_pods_terminate(self) -> None:
        result, calls, state = self.run_restore(
            active_writer=True,
            api_termination_stuck=True,
        )
        rendered = [self.operation(call) for call in calls]

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state["replicas"], 0)
        self.assertEqual(state["api_pods"], 2)
        self.assertTrue(
            any(
                "delete pods -l app.kubernetes.io/name=coop-api" in call
                for call in rendered
            )
        )
        self.assertTrue(
            any(
                "wait --for=delete pod -l app.kubernetes.io/name=coop-api" in call
                for call in rendered
            )
        )
        self.assertTrue(
            any(
                "get pods -l app.kubernetes.io/name=coop-api -o json" in call
                for call in rendered
            )
        )
        self.assertNotIn("verified suspended/stopped", result.stderr)
        self.assertIn("could not prove the fail-closed quarantine", result.stderr)
        self.assertFalse(any(call.startswith("curl ") for call in rendered))

    def test_success_proves_no_api_pods_remain_before_restore(self) -> None:
        result, calls, state = self.run_restore()
        rendered = [self.operation(call) for call in calls]

        self.assertEqual(result.returncode, 0, result.stderr)
        delete_index = next(
            index
            for index, call in enumerate(rendered)
            if "delete pods -l app.kubernetes.io/name=coop-api" in call
        )
        wait_index = next(
            index
            for index, call in enumerate(rendered)
            if "wait --for=delete pod -l app.kubernetes.io/name=coop-api" in call
        )
        readback_index = next(
            index
            for index, call in enumerate(rendered)
            if "get pods -l app.kubernetes.io/name=coop-api -o json" in call
        )
        restore_index = next(
            index
            for index, call in enumerate(rendered)
            if "create -f - -o jsonpath={.metadata.name}" in call
        )
        self.assertLess(delete_index, wait_index)
        self.assertLess(wait_index, readback_index)
        self.assertLess(readback_index, restore_index)
        self.assertEqual(state["api_pods"], 2)

    def test_success_resumes_and_reconciles_in_dependency_order(self) -> None:
        result, calls, state = self.run_restore()
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = [self.operation(call) for call in calls]
        self.assertEqual(sum(" get jobs " in f" {call} " for call in rendered), 2)
        expected = [
            "gitrepository/coop-video-planning",
            "kustomization/coop-database",
            "kustomization/coop-migration",
            "kustomization/coop-import",
            "kustomization/coop-apps",
            "kustomization/coop-ingress",
            "kustomization/coop-backup",
            "kustomization/coop-root",
        ]
        resume_indexes: list[int] = []
        for resource in expected:
            kind, name = resource.split("/", 1)
            resume_indexes.append(
                next(
                    index
                    for index, call in enumerate(rendered)
                    if f"patch {kind} {name}" in call and '"suspend":false' in call
                )
            )
        self.assertEqual(resume_indexes, sorted(resume_indexes))
        for resource, resume_index in zip(expected, resume_indexes, strict=True):
            kind, name = resource.split("/", 1)
            if resource == "kustomization/coop-root":
                self.assertTrue(
                    any(
                        index > resume_index and f"get {kind} {name} -o json" in call
                        for index, call in enumerate(rendered)
                    ),
                    resource,
                )
                self.assertFalse(
                    any("annotate" in call and f"{kind}/{name}" in call for call in rendered)
                )
                continue
            annotate_index = next(
                index
                for index, call in enumerate(rendered)
                if index > resume_index and "annotate" in call and f"{kind}/{name}" in call
            )
            self.assertTrue(
                any(
                    index > annotate_index and f"get {kind} {name} -o json" in call
                    for index, call in enumerate(rendered)
                ),
                resource,
            )
        self.assertTrue(any("create -f - -o jsonpath={.metadata.name}" in call for call in rendered))
        self.assertTrue(any("delete pod coop-restore-generated" in call for call in rendered))
        self.assertTrue(
            any(
                call.startswith("curl ")
                and "-X DELETE" in call
                and "http://localhost/api/v1/namespaces/coop-prod/configmaps/coop-postgres-restore-lock"
                in call
                and '"kind": "DeleteOptions"' in call
                and '"uid": "lock-uid-1"' in call
                and '"resourceVersion": "1"' in call
                for call in rendered
            )
        )
        self.assertTrue(
            any(
                "proxy" in call
                and any(part.startswith("--unix-socket=") for part in calls[index])
                and "--api-prefix=/" in calls[index]
                for index, call in enumerate(rendered)
            )
        )
        self.assertFalse(any("--preconditions=" in call for call in rendered))
        self.assertGreaterEqual(state["lock_reads"], 2)
        self.assertIsNone(state["lock"])
        for cronjob in (
            "coop-seed",
            "gmail-import-full",
            "gmail-import-bootstrap",
            "gmail-import-delta",
            "gmail-semantic-worker",
        ):
            self.assertTrue(state["suspended"].pop(f"cronjob/{cronjob}"))
        self.assertFalse(
            any(
                suspended
                for resource, suspended in state["suspended"].items()
                if not resource.startswith("cronjob/") and resource != "kustomization/coop-semantic"
            )
        )
        self.assertTrue(state["suspended"]["kustomization/coop-semantic"])
        self.assertEqual(state["replicas"], 2)

    def test_matching_generation_and_ready_with_stale_marker_does_not_resume_api(self) -> None:
        result, calls, state = self.run_restore(stale_reconcile=True)
        rendered = [self.operation(call) for call in calls]
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fresh reconciliation", result.stderr)
        self.assertFalse(any("--replicas=2" in call for call in rendered))
        self.assertNotIn("Restore completed", result.stdout)
        source = "gitrepository/coop-video-planning"
        self.assertEqual(state["observed_generation"][source], state["generation"][source])
        self.assertTrue(state["ready"][source])
        self.assertEqual(state["last_handled"][source], "old-request")
        self.assertTrue(all(state["suspended"].values()))
        self.assertEqual(state["replicas"], 0)

    def test_child_with_matching_generation_and_ready_still_requires_marker(self) -> None:
        child = "kustomization/coop-migration"
        result, calls, state = self.run_restore(stale_marker_resource=child)
        rendered = [self.operation(call) for call in calls]
        self.assertEqual(result.returncode, 10)
        self.assertIn(f"fresh reconciliation of {child}", result.stderr)
        self.assertEqual(state["observed_generation"][child], state["generation"][child])
        self.assertTrue(state["ready"][child])
        self.assertEqual(state["last_handled"][child], "old-request")
        self.assertFalse(any("--replicas=2" in call for call in rendered))
        self.assertTrue(all(state["suspended"].values()))
        self.assertEqual(state["replicas"], 0)

    def test_matching_last_handled_with_stale_observed_generation_does_not_resume_api(self) -> None:
        result, calls, state = self.run_restore(stale_observed_generation=True)
        rendered = [self.operation(call) for call in calls]
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fresh reconciliation", result.stderr)
        self.assertFalse(any("--replicas=2" in call for call in rendered))
        self.assertTrue(all(state["suspended"].values()))
        self.assertEqual(state["replicas"], 0)

    def test_self_managed_root_uses_fresh_generation_without_request_marker(self) -> None:
        result, calls, state = self.run_restore()
        rendered = [self.operation(call) for call in calls]
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            state["observed_generation"]["kustomization/coop-root"],
            state["generation"]["kustomization/coop-root"],
        )
        self.assertEqual(state["last_handled"]["kustomization/coop-root"], "old-request")
        self.assertFalse(
            any(
                "annotate" in call and "kustomization/coop-root" in call
                for call in rendered
            )
        )

    def test_self_managed_root_rejects_stale_observed_generation(self) -> None:
        result, calls, state = self.run_restore(root_stale_observed_generation=True)
        rendered = [self.operation(call) for call in calls]
        self.assertEqual(result.returncode, 10)
        self.assertIn("fresh reconciliation of kustomization/coop-root", result.stderr)
        self.assertFalse(
            any(
                "annotate" in call and "kustomization/coop-root" in call
                for call in rendered
            )
        )
        self.assertTrue(all(state["suspended"].values()))
        self.assertEqual(state["replicas"], 0)

    def test_self_managed_root_rejects_not_ready(self) -> None:
        result, _, state = self.run_restore(root_not_ready=True)
        self.assertEqual(result.returncode, 10)
        self.assertIn("fresh reconciliation of kustomization/coop-root", result.stderr)
        self.assertTrue(all(state["suspended"].values()))
        self.assertEqual(state["replicas"], 0)

    def test_restore_requires_backend_termination_to_succeed(self) -> None:
        script = RESTORE.read_text(encoding="utf-8")
        terminate = next(line for line in script.splitlines() if "pg_terminate_backend" in line)
        self.assertNotIn("|| true", terminate)

    def test_recurring_gmail_scheduler_comment_matches_dormant_delta_state(self) -> None:
        manifest = (
            ROOT / "infrastructure/import/gmail-recurring-cronjobs.yaml"
        ).read_text(encoding="utf-8")
        comment = manifest.splitlines()[0].lower()
        self.assertIn("both recurring collectors remain dormant", comment)


class RegistryBootstrapTests(unittest.TestCase):
    image = (
        "docker.io/library/registry:3.1.2@"
        "sha256:ddf754342cfc8acc51a56d5d0ab6af06826461864460636d8bd5c546dab2a7b8"
    )

    github_host_key = (
        "github.com ssh-ed25519 "
        "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl\n"
    )
    firewall_status = textwrap.dedent(
        """\
        Status: active
        Logging: on (low)
        Default: deny (incoming), allow (outgoing), deny (routed)
        New profiles: skip

        To                         Action      From
        --                         ------      ----
        22/tcp                     ALLOW IN    Anywhere                   # SSH management
        80/tcp                     ALLOW IN    192.168.2.0/24             # Coop HTTP LAN redirect
        443/tcp                    ALLOW IN    192.168.2.0/24             # Coop HTTPS LAN
        Anywhere                   ALLOW IN    10.42.0.0/16               # k3s pod CIDR
        Anywhere                   ALLOW IN    10.43.0.0/16               # k3s service CIDR
        22/tcp (v6)                ALLOW IN    Anywhere (v6)              # SSH management

        Anywhere                   ALLOW FWD   10.42.0.0/16               # k3s pod forwarding
        Anywhere                   ALLOW FWD   10.43.0.0/16               # k3s service forwarding
        """
    )

    def run_bootstrap(
        self,
        *,
        running: bool,
        mismatch: str | None = None,
        arguments: tuple[str, ...] = (),
        github_host_key: str | None = None,
        firewall_status: str | None = None,
        inspect_mode: str = "existing",
    ) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            registry_data = temp / "registry"
            k3s_config = temp / "config.yaml"
            registries = temp / "registries.yaml"
            config_dir = temp / "coop-config"
            config_dir.mkdir()
            (config_dir / "sops-age.key").write_text("AGE-SECRET-KEY-MOCK\n", encoding="utf-8")
            (config_dir / "flux-github-readonly").write_text("MOCK PRIVATE KEY\n", encoding="utf-8")
            known_hosts = temp / "github-known-hosts"
            known_hosts.write_text(
                self.github_host_key if github_host_key is None else github_host_key,
                encoding="utf-8",
            )
            k3s_config.write_text("bind-address: 192.168.2.94\n", encoding="utf-8")
            registries.write_text("mirrors:\n  x:\n    endpoint: [http://127.0.0.1:5000]\n", encoding="utf-8")
            log_file = temp / "commands.jsonl"
            inspect = {
                "Config": {
                    "Image": self.image,
                    "Env": [
                        "OTEL_TRACES_EXPORTER=none",
                        "REGISTRY_STORAGE_FILESYSTEM_ROOTDIRECTORY=/var/lib/registry",
                    ],
                    "Entrypoint": ["/entrypoint.sh"],
                    "Cmd": ["/etc/distribution/config.yml"],
                },
                "HostConfig": {
                    "PortBindings": {"5000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "5000"}]},
                    "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
                },
                "Mounts": [
                    {
                        "Type": "bind",
                        "Source": str(registry_data),
                        "Destination": "/var/lib/registry",
                        "RW": True,
                    }
                ],
                "State": {"Running": running},
            }
            if mismatch == "image":
                inspect["Config"]["Image"] = "docker.io/library/registry:3.1.1@sha256:bad"
            elif mismatch == "mount":
                inspect["Mounts"][0]["Source"] = str(temp / "wrong")
            elif mismatch == "datadir":
                inspect["Config"]["Env"][1] = "REGISTRY_STORAGE_FILESYSTEM_ROOTDIRECTORY=/wrong"
            elif mismatch == "env":
                inspect["Config"]["Env"].append("REGISTRY_HTTP_SECRET=unexpected")
            elif mismatch == "command":
                inspect["Config"]["Cmd"] = ["/wrong.yml"]
            elif mismatch == "port":
                inspect["HostConfig"]["PortBindings"]["5000/tcp"][0]["HostIp"] = "0.0.0.0"
            elif mismatch == "restart":
                inspect["HostConfig"]["RestartPolicy"]["Name"] = "always"
            inspect_file = temp / "inspect.json"
            inspect_file.write_text(json.dumps([inspect]), encoding="utf-8")

            common_logger = textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import json, os, sys
                with open(os.environ["MOCK_LOG"], "a") as stream:
                    stream.write(json.dumps([os.path.basename(sys.argv[0]), *sys.argv[1:]]) + "\\n")
                """
            )
            commands: dict[str, str] = {
                "docker": common_logger
                + textwrap.dedent(
                    """\
                    args = sys.argv[1:]
                    if args[:1] == ["version"]:
                        print("29.9.1")
                    elif args[:2] == ["container", "inspect"]:
                        mode = os.environ["MOCK_INSPECT_MODE"]
                        if mode == "missing":
                            print("Error: No such object: coop-registry", file=sys.stderr)
                            sys.exit(1)
                        if mode == "error":
                            print("Cannot connect to the Docker daemon", file=sys.stderr)
                            sys.exit(1)
                        print(open(os.environ["MOCK_INSPECT"]).read())
                    elif args[:1] == ["inspect"]:
                        print(open(os.environ["MOCK_INSPECT"]).read())
                    sys.exit(0)
                    """
                ),
                "k3s": common_logger
                + "\nif sys.argv[1:] == ['--version']: print('k3s version v1.36.5+k3s1 (mock)')\nsys.exit(0)\n",
                "kubectl": common_logger + "\nsys.exit(0)\n",
                "flux": common_logger
                + "\nif sys.argv[1:] == ['--version']: print('flux version 2.9.6')\nsys.exit(0)\n",
                "sops": common_logger
                + "\nif sys.argv[1:] == ['--version']: print('sops 3.13.3')\nsys.exit(0)\n",
                "age": common_logger
                + "\nif sys.argv[1:] == ['--version']: print('v1.3.2')\nsys.exit(0)\n",
                "curl": common_logger + "\nsys.exit(0)\n",
                "install": common_logger
                + "\nfrom pathlib import Path\nPath(sys.argv[-1]).mkdir(parents=True, exist_ok=True)\nsys.exit(0)\n",
                "ssh-keyscan": common_logger + "\nsys.exit(0)\n",
                "systemctl": common_logger + "\nsys.exit(0)\n",
                "ufw": common_logger
                + "\nprint(os.environ['MOCK_UFW_STATUS'], end='')\nsys.exit(0)\n",
            }
            for name, content in commands.items():
                executable = temp / name
                executable.write_text(content, encoding="utf-8")
                executable.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{temp}:{env['PATH']}",
                    "MOCK_LOG": str(log_file),
                    "MOCK_INSPECT": str(inspect_file),
                    "MOCK_INSPECT_MODE": inspect_mode,
                    "REGISTRY_DATA": str(registry_data),
                    "K3S_CONFIG_FILE": str(k3s_config),
                    "K3S_REGISTRIES_FILE": str(registries),
                    "COOP_CONFIG_DIR": str(config_dir),
                    "GITHUB_KNOWN_HOSTS_FILE": str(known_hosts),
                    "MOCK_UFW_STATUS": self.firewall_status if firewall_status is None else firewall_status,
                }
            )
            result = subprocess.run(
                [str(BOOTSTRAP), *arguments],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )
            calls = (
                [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
                if log_file.exists()
                else []
            )
            return result, calls

    def test_existing_mismatched_registry_aborts_without_replacement(self) -> None:
        result, calls = self.run_bootstrap(running=True, mismatch="image")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("configuration differs", result.stderr)
        docker_calls = [call[1:] for call in calls if call[0] == "docker"]
        self.assertFalse(any(call and call[0] in {"rm", "run", "start"} for call in docker_calls))

    def test_existing_registry_validates_mount_datadir_env_command_port_and_restart(self) -> None:
        for mismatch in ("image", "mount", "datadir", "env", "command", "port", "restart"):
            with self.subTest(mismatch=mismatch):
                result, calls = self.run_bootstrap(running=True, mismatch=mismatch)
                self.assertNotEqual(result.returncode, 0)
                docker_calls = [call[1:] for call in calls if call[0] == "docker"]
                self.assertFalse(
                    any(call and call[0] in {"rm", "run", "start", "restart"} for call in docker_calls)
                )

    def test_stopped_exact_registry_is_started_without_replacement(self) -> None:
        result, calls = self.run_bootstrap(running=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        docker_calls = [call[1:] for call in calls if call[0] == "docker"]
        self.assertIn(["start", "coop-registry"], docker_calls)
        self.assertFalse(any(call and call[0] in {"rm", "run"} for call in docker_calls))
        self.assertTrue(any(call[0] == "curl" for call in calls))

    def test_running_exact_registry_is_not_restarted(self) -> None:
        result, calls = self.run_bootstrap(running=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        docker_calls = [call[1:] for call in calls if call[0] == "docker"]
        self.assertFalse(any(call and call[0] in {"rm", "run", "start", "restart"} for call in docker_calls))

    def test_missing_registry_is_created(self) -> None:
        result, calls = self.run_bootstrap(running=False, inspect_mode="missing")
        self.assertEqual(result.returncode, 0, result.stderr)
        docker_calls = [call[1:] for call in calls if call[0] == "docker"]
        self.assertTrue(any(call and call[0] == "pull" for call in docker_calls))
        self.assertTrue(any(call and call[0] == "run" for call in docker_calls))

    def test_registry_inspect_error_aborts_without_create_or_start(self) -> None:
        result, calls = self.run_bootstrap(running=False, inspect_mode="error")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("inspect", result.stderr)
        docker_calls = [call[1:] for call in calls if call[0] == "docker"]
        self.assertFalse(any(call and call[0] in {"pull", "run", "start"} for call in docker_calls))

    def test_untrusted_github_host_key_aborts_before_cluster_mutation(self) -> None:
        result, calls = self.run_bootstrap(
            running=True,
            arguments=("--bootstrap-coop-root",),
            github_host_key="github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBadKey\n",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GitHub Ed25519 host key fingerprint", result.stderr)
        mutation_words = {"apply", "create", "delete", "patch", "replace"}
        cluster_calls = [call for call in calls if call[0] in {"k3s", "kubectl", "flux"}]
        self.assertFalse(any(mutation_words.intersection(call[1:]) for call in cluster_calls), cluster_calls)
        self.assertFalse(any(call[0] == "ssh-keyscan" for call in calls))

    def test_versioned_github_host_key_is_verified_before_cluster_mutation(self) -> None:
        result, calls = self.run_bootstrap(
            running=True,
            arguments=("--bootstrap-coop-root",),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(call[0] == "ssh-keyscan" for call in calls))
        self.assertTrue(
            any(
                call[0] == "k3s"
                and "create" in call
                and "coop-git-readonly" in call
                and any(part.startswith("--from-file=known_hosts=") for part in call)
                for call in calls
            )
        )

    def test_verify_firewall_is_read_only_and_accepts_the_documented_rules(self) -> None:
        result, calls = self.run_bootstrap(running=True, arguments=("--verify-firewall",))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [["ufw", "status", "verbose"]])
        self.assertIn("Firewall configuration verified read-only", result.stdout)

    def test_verify_firewall_fails_closed_on_an_extra_public_https_rule(self) -> None:
        unsafe = self.firewall_status + "443/tcp ALLOW IN Anywhere\n"
        result, calls = self.run_bootstrap(
            running=True,
            arguments=("--verify-firewall",),
            firewall_status=unsafe,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [["ufw", "status", "verbose"]])


class BackupTrapTests(unittest.TestCase):
    def backup_script(self) -> str:
        with BACKUP.open(encoding="utf-8") as stream:
            manifest = yaml.safe_load(stream)
        return manifest["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["args"][0]

    def run_backup(self, mode: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            backup_dir = temp / "backups"
            bin_dir = temp / "bin"
            backup_dir.mkdir()
            bin_dir.mkdir()
            script = self.backup_script().replace("/backups", str(backup_dir))
            commands = {
                "date": "#!/bin/sh\ncase \"$*\" in *%u*) echo 7;; *) echo 20260104T011700Z;; esac\n",
                "pg_dump": "#!/bin/sh\nfor x in \"$@\"; do case \"$x\" in --file=*) f=${x#--file=};; esac; done\nprintf dump >\"$f\"\n",
                "pg_isready": "#!/bin/sh\nexit 0\n",
                "pg_restore": "#!/bin/sh\nexit 0\n",
                "cp": textwrap.dedent(
                    f"""\
                    #!/usr/bin/env python3
                    import os, pathlib, signal, sys
                    pathlib.Path(sys.argv[2]).write_text("partial")
                    if {mode!r} == "signal":
                        os.kill(os.getppid(), signal.SIGTERM)
                        sys.exit(0)
                    sys.exit(23)
                    """
                ),
            }
            for name, content in commands.items():
                path = bin_dir / name
                path.write_text(content, encoding="utf-8")
                path.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}:{env['PATH']}"
            result = subprocess.run(
                ["/bin/sh", "-ec", script],
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            return result, sorted(path.name for path in backup_dir.iterdir())

    def test_cp_failure_removes_daily_and_weekly_temporary_files(self) -> None:
        result, files = self.run_backup("failure")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(name.endswith(".tmp") for name in files), files)

    def test_signal_removes_daily_and_weekly_temporary_files(self) -> None:
        result, files = self.run_backup("signal")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(name.endswith(".tmp") for name in files), files)


if __name__ == "__main__":
    unittest.main()
