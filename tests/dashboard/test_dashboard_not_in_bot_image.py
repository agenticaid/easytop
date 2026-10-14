"""The dashboard's libraries are an optional extra: the bot's image must not grow (Spec 12 "Stack and code")."""

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HEAVY = ("streamlit", "google-cloud-bigquery", "pandas", "db-dtypes")


def test_the_dashboard_libraries_are_only_in_the_dashboard_extra():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    base = " ".join(project["dependencies"]).lower()
    extra = " ".join(project["optional-dependencies"]["dashboard"]).lower()
    for name in HEAVY:
        assert name not in base, f"{name} must not be a base dependency"
        assert name in extra, f"{name} belongs to the dashboard extra"


def test_the_dockerfile_installs_no_extras():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "--extra" not in dockerfile and "--all-extras" not in dockerfile
    assert "dashboard" not in dockerfile
    assert "uv sync --frozen --no-dev" in dockerfile
