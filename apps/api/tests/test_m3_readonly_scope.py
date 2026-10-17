from __future__ import annotations

import ast
import re
import shutil
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
ALLOWED_INGESTION_MODULES = {"__init__.py", "gmail.py"}
ALLOWED_IMPORT_RESOURCES = {
    "gmail-collector-db.secret.sops.yaml",
    "gmail-egress-proxy.yaml",
    "gmail-full-pilot-admission.yaml",
    "gmail-full-pilot-rbac.yaml",
    "gmail-import-cronjobs.yaml",
    "gmail-recurring-cronjobs.yaml",
    "gmail-oauth.secret.sops.yaml",
    "network-policy.yaml",
}
ALLOWED_API_ROUTES = {
    ("GET", "/api/v1/health/live"),
    ("GET", "/api/v1/health/ready"),
    ("POST", "/api/v1/organizations"),
    ("POST", "/api/v1/contacts"),
    ("POST", "/api/v1/coops"),
    ("GET", "/api/v1/coops"),
    ("GET", "/api/v1/coops/{coop_id}"),
    ("PATCH", "/api/v1/coops/{coop_id}"),
    ("POST", "/api/v1/coops/{coop_id}/transitions"),
    ("POST", "/api/v1/coops/{coop_id}/reopen"),
    ("GET", "/api/v1/coops/{coop_id}/audit-events"),
}
FORBIDDEN_OPERATION = re.compile(r"(?i)\b(?:apply|dry[- ]?run)\b")
DELTA_OPERATION = re.compile(r"(?i)\b(?:delta|history(?:-sync)?)\b")
FUTURE_OR_ABSENT = re.compile(
    r"(?i)\b(?:not shipped|not available|future|later|planned|"
    r"kein(?:e|en)?|nicht|ohne|später|zukünftig|blockiert|blocked|"
    r"dormant|suspended|suspendiert|unarmed)\b"
)
AFFIRMATIVE_SHIPPED_CLAIM = re.compile(
    r"(?i)\b(?:ships?|shipped|available|enabled|supports?|liefert|geliefert|"
    r"aktiviert|verfügbar|führt|runs?|scheduled|schedule|term(?:in|iniert))\b"
)
PRODUCTION_MUTATION_NAME = re.compile(
    r"(?i)(?:^|_)(?:apply|mutat(?:e|ion)|dry_?run|delta|history_?sync)(?:_|$)"
)
INGESTION_CONTEXT = re.compile(
    r"(?i)gmail|ingestion|evidence|manifest|app\.ingestion|\bimports?\b|\bimporter\b"
)
PRODUCTION_CONTENT_CONTEXT = re.compile(r"(?i)gmail|ingestion|evidence|manifest|app\.ingestion")
PRODUCTION_PATH_CONTEXT = re.compile(
    r"(?i)(?:^|[/_.-])(?:gmail|ingestion|evidence|manifest|import)(?:[/_.-]|$)"
)
FORBIDDEN_COMMAND = re.compile(
    r"(?i)(?:python\s+-m\s+app\.ingestion\.[\w.]+\s+"
    r"(?:apply|dry-run)\b|"
    r"/api/v\d+/(?:ingestion|imports?)/[^\s`\"']*"
    r"(?:apply|dry-run|delta|history))"
)


def _kustomization_resources(path: Path) -> set[str]:
    resources: set[str] = set()
    in_resources = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line == "resources:":
            in_resources = True
            continue
        if in_resources and line.startswith("  - "):
            resources.add(line.removeprefix("  - ").strip())
            continue
        if in_resources and line and not line.startswith(" "):
            break
    return resources


def _yaml_documents(path: Path) -> list[str]:
    return re.split(r"(?m)^---\s*$", path.read_text(encoding="utf-8"))


def _kind(document: str) -> str | None:
    match = re.search(r"(?m)^kind:\s*([^\s#]+)", document)
    return match.group(1) if match else None


def _metadata_name(document: str) -> str | None:
    match = re.search(r"(?ms)^metadata:\s*\n(?:(?:  .*)?\n)*?  name:\s*([^\s#]+)", document)
    return match.group(1) if match else None


def _python_route_inventory(path: Path) -> tuple[set[tuple[str, str]], list[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    routes: set[tuple[str, str]] = set()
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            if any(module.startswith("app.ingestion") for module in modules):
                violations.append(f"API/service imports ingestion runtime: {path}")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"include_router", "add_api_route"}
        ):
            violations.append(f"dynamic API route registration is outside inventory: {path}")
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            method = decorator.func.attr.upper()
            if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"} or not decorator.args:
                continue
            route = decorator.args[0]
            if isinstance(route, ast.Constant) and isinstance(route.value, str):
                routes.add((method, route.value))
            else:
                violations.append(
                    f"non-literal API route is outside inventory: {path}:{node.lineno}"
                )
    return routes, violations


def _python_ingestion_mutation_surfaces(path: Path, root: Path) -> list[str]:
    relative = path.relative_to(root).as_posix()
    if relative == "apps/api/app/ingestion/gmail.py":
        return []
    source = path.read_text(encoding="utf-8")
    if (
        PRODUCTION_PATH_CONTEXT.search(relative) is None
        and PRODUCTION_CONTENT_CONTEXT.search(source) is None
    ):
        return []
    tree = ast.parse(source, filename=str(path))
    named_surfaces: set[tuple[int, str]] = {(1, path.stem)}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            named_surfaces.add((node.lineno, node.name))
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            named_surfaces.add((node.lineno, node.id))
        elif isinstance(node, ast.alias):
            named_surfaces.add((getattr(node, "lineno", 1), node.asname or node.name))
    return [
        f"Gmail ingestion mutation surface outside allowlist: {path}:{line_number}:{name}"
        for line_number, name in sorted(named_surfaces)
        if PRODUCTION_MUTATION_NAME.search(name)
    ]


def _operational_files(root: Path) -> list[Path]:
    files: set[Path] = set()
    for relative in ("docs", "scripts", "infrastructure", "clusters", ".github"):
        base = root / relative
        if base.exists():
            files.update(path for path in base.rglob("*") if path.is_file())
    readme = root / "README.md"
    if readme.exists():
        files.add(readme)
    for relative in ("apps/web/app", "apps/web/features", "apps/web/lib"):
        base = root / relative
        if base.exists():
            files.update(path for path in base.rglob("*") if path.is_file())
    return sorted(files)


def _semantic_surface_violations(path: Path, root: Path) -> list[str]:
    if path.suffix not in {".py", ".md", ".sh", ".yml", ".yaml", ".ts", ".tsx", ".js", ".jsx"}:
        return []
    relative = path.relative_to(root).as_posix()
    path_context = INGESTION_CONTEXT.search(relative) is not None
    violations: list[str] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not path_context and INGESTION_CONTEXT.search(line) is None:
            continue
        forbidden_operation = FORBIDDEN_OPERATION.search(line) is not None
        delta_operation = DELTA_OPERATION.search(line) is not None
        if not forbidden_operation and not delta_operation:
            continue
        if forbidden_operation and FORBIDDEN_COMMAND.search(line):
            violations.append(f"shipped ingestion mutation command: {path}:{line_number}")
        elif AFFIRMATIVE_SHIPPED_CLAIM.search(line) and FUTURE_OR_ABSENT.search(line) is None:
            violations.append(f"affirmative shipped ingestion claim: {path}:{line_number}")
        elif path.suffix in {".ts", ".tsx", ".js", ".jsx"} and re.search(
            r"(?i)\b(?:function|const|let|var|class)\s+[\w$]*(?:apply|mutat|dry.?run|delta)[\w$]*",
            line,
        ):
            violations.append(f"web ingestion mutation surface: {path}:{line_number}")
    return violations


def readonly_scope_violations(root: Path) -> list[str]:
    violations: list[str] = []
    ingestion_dir = root / "apps/api/app/ingestion"
    modules = {
        path.relative_to(ingestion_dir).as_posix()
        for path in ingestion_dir.rglob("*.py")
        if "__pycache__" not in path.parts
    }
    if modules != ALLOWED_INGESTION_MODULES:
        violations.append(f"production ingestion modules differ: {sorted(modules)}")
    if (ingestion_dir / "__main__.py").exists():
        violations.append("ingestion package __main__.py is forbidden")

    project = tomllib.loads((root / "apps/api/pyproject.toml").read_text(encoding="utf-8"))
    for table_name in ("scripts", "gui-scripts"):
        scripts = project.get("project", {}).get(table_name, {})
        for name, target in scripts.items():
            if "ingest" in name.casefold() or str(target).startswith("app.ingestion"):
                violations.append(f"ingestion console entrypoint is forbidden: {name}")

    app_dir = root / "apps/api/app"
    routes: set[tuple[str, str]] = set()
    for module in app_dir.rglob("*.py"):
        if "__pycache__" in module.parts:
            continue
        module_routes, module_violations = _python_route_inventory(module)
        routes.update(module_routes)
        violations.extend(module_violations)
        violations.extend(_python_ingestion_mutation_surfaces(module, root))
    if routes != ALLOWED_API_ROUTES:
        violations.append(f"API route inventory differs: {sorted(routes)}")

    import_dir = root / "infrastructure/import"
    resources = _kustomization_resources(import_dir / "kustomization.yaml")
    if resources != ALLOWED_IMPORT_RESOURCES:
        violations.append(f"import kustomization resources differ: {sorted(resources)}")
    workloads: list[tuple[str, str | None, str]] = []
    for manifest in import_dir.glob("*.yaml"):
        if manifest.name.endswith("secret.example.yaml"):
            continue
        for document in _yaml_documents(manifest):
            kind = _kind(document)
            if kind in {"Job", "CronJob"}:
                workloads.append((kind, _metadata_name(document), document))
    expected_workloads = {
        "gmail-import-full": ("full", True, "false"),
        "gmail-import-bootstrap": ("bootstrap", True, "false"),
        "gmail-import-delta": ("delta", False, "true"),
    }
    actual_workloads = {
        name: document for kind, name, document in workloads if kind == "CronJob" and name
    }
    if set(actual_workloads) != set(expected_workloads):
        violations.append(f"import workload inventory differs: {sorted(actual_workloads)}")
    for name, (command, suspended, armed) in expected_workloads.items():
        cronjob = actual_workloads.get(name, "")
        expected_suspend = str(suspended).lower()
        if re.search(rf"(?m)^  suspend:\s*{expected_suspend}\s*$", cronjob) is None:
            violations.append(f"{name} suspend state must be {expected_suspend}")
        if f'args: ["{command}"]' not in cronjob:
            violations.append(f"{name} must expose only the {command} command")
        if f'{{name: GMAIL_IMPORT_ARMED, value: "{armed}"}}' not in cronjob:
            violations.append(f"{name} armed state must be {armed}")

    allowed_gmail_workloads = {
        ("infrastructure/import/gmail-import-cronjobs.yaml", "CronJob", "gmail-import-full"),
        (
            "infrastructure/import/gmail-recurring-cronjobs.yaml",
            "CronJob",
            "gmail-import-bootstrap",
        ),
        ("infrastructure/import/gmail-recurring-cronjobs.yaml", "CronJob", "gmail-import-delta"),
    }
    for path in _operational_files(root):
        violations.extend(_semantic_surface_violations(path, root))
        if path.suffix not in {".yml", ".yaml"}:
            continue
        for document in _yaml_documents(path):
            kind = _kind(document)
            if kind not in {"Job", "CronJob"} or INGESTION_CONTEXT.search(document) is None:
                continue
            workload = (path.relative_to(root).as_posix(), kind, _metadata_name(document))
            if workload not in allowed_gmail_workloads:
                violations.append(f"unexpected repository Gmail/import workload: {workload}")
    return violations


def _copy_contract_surface(destination: Path) -> Path:
    for relative in (
        "apps/api/app",
        "apps/api/tests",
        "apps/web/app",
        "apps/web/features",
        "apps/web/lib",
        "infrastructure",
        "clusters",
        ".github",
        "docs",
        "scripts",
    ):
        source = REPOSITORY_ROOT / relative
        if not source.exists():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)
    for relative in ("apps/api/pyproject.toml", "README.md"):
        source = REPOSITORY_ROOT / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return destination


def _add_renamed_module(root: Path) -> None:
    (root / "apps/api/app/ingestion/apply_v2.py").write_text("def mutate():\n    pass\n")


def _add_service_apply_surface(root: Path) -> None:
    path = root / "apps/api/app/services/gmail_apply_v2.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("def apply_reviewed_manifest():\n    pass\n", encoding="utf-8")


def _add_console_entrypoint(root: Path) -> None:
    path = root / "apps/api/pyproject.toml"
    path.write_text(
        path.read_text(encoding="utf-8")
        + '\n[project.scripts]\ngmail-import-v2 = "app.ingestion.gmail:main"\n',
        encoding="utf-8",
    )


def _add_api_apply_route(root: Path) -> None:
    path = root / "apps/api/app/main.py"
    path.write_text(
        path.read_text(encoding="utf-8")
        + '\n@app.post("/api/v1/ingestion/apply-v2")\ndef apply_v2():\n    return {}\n',
        encoding="utf-8",
    )


def _add_extra_import_cronjob(root: Path) -> None:
    path = root / "infrastructure/import/gmail-import-cronjobs.yaml"
    path.write_text(
        path.read_text(encoding="utf-8")
        + "\n---\napiVersion: batch/v1\nkind: CronJob\nmetadata:\n  name: gmail-import-v2\n"
        "spec:\n  suspend: true\n",
        encoding="utf-8",
    )


def _add_shipped_delta_claim(root: Path) -> None:
    path = root / "docs/gmail-import-operations.md"
    path.write_text(
        path.read_text(encoding="utf-8") + "\nGmail delta sync is enabled and scheduled.\n",
        encoding="utf-8",
    )


def _add_infrastructure_shipped_delta_claim(root: Path) -> None:
    path = root / "infrastructure/import/README.md"
    path.write_text(
        path.read_text(encoding="utf-8") + "\nGmail delta sync is enabled and scheduled.\n",
        encoding="utf-8",
    )


def _add_mutation_command(root: Path) -> None:
    path = root / "scripts/gmail-import-apply.sh"
    path.write_text("python -m app.ingestion.gmail apply\n", encoding="utf-8")


@pytest.mark.no_db
def test_option_a_readonly_scope_inventory_is_exact() -> None:
    assert readonly_scope_violations(REPOSITORY_ROOT) == []


@pytest.mark.no_db
@pytest.mark.parametrize(
    "counterexample",
    [
        _add_renamed_module,
        _add_service_apply_surface,
        _add_console_entrypoint,
        _add_api_apply_route,
        _add_extra_import_cronjob,
        _add_shipped_delta_claim,
        _add_infrastructure_shipped_delta_claim,
        _add_mutation_command,
    ],
    ids=[
        "renamed-apply-module",
        "service-outside-ingestion",
        "console-entrypoint",
        "api-apply-route",
        "extra-cronjob",
        "shipped-delta-claim",
        "infrastructure-readme-shipped-delta-claim",
        "mutation-command",
    ],
)
def test_scope_guard_rejects_renamed_or_additional_shipped_surfaces(
    tmp_path: Path, counterexample: Callable[[Path], None]
) -> None:
    repository = _copy_contract_surface(tmp_path / "repository")
    assert readonly_scope_violations(repository) == []

    counterexample(repository)

    assert readonly_scope_violations(repository) != []
