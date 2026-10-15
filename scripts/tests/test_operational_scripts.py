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
        stale_observed_generation: bool = False,
        initially_suspended: str | None = None,
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
            }
            state_file.write_text(
                json.dumps(
                    {
                        "suspended": suspended,
                        "replicas": 2,
                        "generation": {item: 1 for item in self.flux_objects},
                        "observed_generation": {item: 1 for item in self.flux_objects},
                        "last_handled": {item: "old-request" for item in self.flux_objects},
                        "ready": {item: True for item in self.flux_objects},
                    }
                ),
                encoding="utf-8",
            )
            kubectl = temp / "kubectl"
            kubectl.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json, os, sys
                    from pathlib import Path

                    args = sys.argv[1:]
                    state_path = Path(os.environ["MOCK_STATE"])
                    log_path = Path(os.environ["MOCK_LOG"])
                    state = json.loads(state_path.read_text())
                    with log_path.open("a") as stream:
                        stream.write(json.dumps(args) + "\\n")

                    def save():
                        state_path.write_text(json.dumps(state))

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
                            print(json.dumps({
                                "metadata": {"generation": state["generation"][resource]},
                                "status": {
                                    "observedGeneration": state["observed_generation"][resource],
                                    "lastHandledReconcileAt": state["last_handled"][resource],
                                    "conditions": [{"type": "Ready", "status": "True" if state["ready"][resource] else "False"}],
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
                        new_suspend = json.loads(payload)["spec"]["suspend"]
                        if kind != "cronjob" and state["suspended"][resource] != new_suspend:
                            state["generation"][resource] += 1
                        state["suspended"][resource] = new_suspend
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
                    elif "create" in args and "-f" in args and "-" in args:
                        sys.stdin.read()
                        print("coop-restore-generated", end="")
                    elif "annotate" in args:
                        resource = next(word for word in args if "/" in word and not word.startswith("reconcile."))
                        requested = next(word for word in args if word.startswith("reconcile.fluxcd.io/requestedAt=")).split("=", 1)[1]
                        if os.environ.get("MOCK_STALE_RECONCILE") != "1":
                            state["last_handled"][resource] = requested
                            if os.environ.get("MOCK_STALE_OBSERVED_GENERATION") != "1":
                                state["observed_generation"][resource] = state["generation"][resource]
                            state["ready"][resource] = True
                            save()
                    elif "scale" in args:
                        replica_arg = next(item for item in args if item.startswith("--replicas="))
                        state["replicas"] = int(replica_arg.split("=", 1)[1])
                        save()
                    elif "exec" in args and os.environ.get("MOCK_FAIL_EXEC") == "1":
                        sys.exit(41)
                    sys.exit(0)
                    """
                ),
                encoding="utf-8",
            )
            kubectl.chmod(0o755)
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
                    "MOCK_STALE_OBSERVED_GENERATION": "1" if stale_observed_generation else "0",
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
                timeout=15,
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

    def test_success_resumes_and_reconciles_in_dependency_order(self) -> None:
        result, calls, state = self.run_restore()
        self.assertEqual(result.returncode, 0, result.stderr)
        rendered = [self.operation(call) for call in calls]
        self.assertEqual(sum(" get jobs " in f" {call} " for call in rendered), 2)
        expected = [
            "gitrepository/coop-video-planning",
            "kustomization/coop-database",
            "kustomization/coop-migration",
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
        self.assertTrue(any("delete configmap coop-postgres-restore-lock" in call for call in rendered))
        self.assertTrue(state["suspended"].pop("cronjob/coop-seed"))
        self.assertFalse(any(state["suspended"].values()))
        self.assertEqual(state["replicas"], 2)

    def test_stale_ready_and_last_handled_reconcile_do_not_resume_api(self) -> None:
        result, calls, state = self.run_restore(stale_reconcile=True)
        rendered = [self.operation(call) for call in calls]
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("fresh reconciliation", result.stderr)
        self.assertFalse(any("--replicas=2" in call for call in rendered))
        self.assertNotIn("Restore completed", result.stdout)
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

    def test_restore_requires_backend_termination_to_succeed(self) -> None:
        script = RESTORE.read_text(encoding="utf-8")
        terminate = next(line for line in script.splitlines() if "pg_terminate_backend" in line)
        self.assertNotIn("|| true", terminate)


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
