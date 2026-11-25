import os
from pathlib import Path
import time

import pytest

from mirage.manifest import ModelRegistry
from mirage.paths import Paths
from mirage.service import Application

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def paths(tmp_path):
    return Paths(tmp_path / "data", ROOT / "config", ROOT / "web")


@pytest.fixture
def registry(paths):
    return ModelRegistry(paths.model_manifest, paths.data / "models")


@pytest.fixture
def app(paths, monkeypatch):
    monkeypatch.setenv("MIRAGE_FAKE_SD", "1")
    application = Application(paths)
    yield application
    application.close()


def install_sparse(registry, model_id, editing=False):
    model = registry.get(model_id)
    for component in model["components"].values():
        if component["required"] or (editing and "edit" in component.get("required_for", [])):
            path = registry.path(component)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as stream:
                stream.truncate(component["bytes"])


def wait_for_job(application, job_id, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = application.database.get_job(job_id)
        if job["status"] in ("completed", "failed", "cancelled", "interrupted"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"job did not finish: {application.database.get_job(job_id)}")
