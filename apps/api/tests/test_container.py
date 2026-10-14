from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]


def test_api_container_runs_as_non_root_with_read_only_root_compatible_runtime() -> None:
    dockerfile = (API_ROOT / "Dockerfile").read_text()

    assert "USER app" in dockerfile
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile
    assert "PYTHONUNBUFFERED=1" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "/api/v1/health/ready" in dockerfile
    assert "uv run" not in dockerfile
    assert "--no-dev" in dockerfile


def test_docker_context_excludes_local_state_and_secrets() -> None:
    dockerignore = (API_ROOT / ".dockerignore").read_text().splitlines()

    assert ".venv" in dockerignore
    assert ".env" in dockerignore
    assert "tests" in dockerignore
    assert "__pycache__" in dockerignore
