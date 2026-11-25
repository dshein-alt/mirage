from pathlib import Path
from unittest.mock import patch

from mirage.provenance import BUILD_INPUTS, source_identity


COMMIT = "79dc7ca9074d79419a46380cdb95a4524db9b74c"


def source_fixture(tmp_path):
    source = tmp_path / "stable-diffusion.cpp"
    (source / "ggml" / "src").mkdir(parents=True)
    (source / "ggml" / "src" / "nested.cpp").write_text("one")
    for name in BUILD_INPUTS:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)


def identity(tmp_path, status="", environment=None):
    with patch("mirage.provenance.git", side_effect=[COMMIT, status]):
        return source_identity(tmp_path, environment)


def test_build_id_accounts_for_nested_source_and_build_inputs(tmp_path):
    source_fixture(tmp_path)
    original = identity(tmp_path)
    assert original["sd_cpp_commit"] == COMMIT
    assert original["dirty"] is False

    (tmp_path / "stable-diffusion.cpp" / "ggml" / "src" / "nested.cpp").write_text("two")
    nested_change = identity(tmp_path, " M stable-diffusion.cpp")
    assert nested_change["build_id"] != original["build_id"]
    assert nested_change["dirty"] is True

    (tmp_path / BUILD_INPUTS[0]).write_text("new builder input")
    assert identity(tmp_path)["build_id"] != nested_change["build_id"]
    assert identity(tmp_path, environment={"ubuntu:24.04": "sha256:other"})["build_id"] != identity(tmp_path)["build_id"]


def test_job_snapshot_contains_runtime_identity(app, registry):
    from conftest import install_sparse

    install_sparse(registry, "zimage")
    job = app.submit_generation({"prompt": "Identity", "model": "zimage"})
    assert job["resolved"]["sd_cpp_commit"] == job["resolved"]["runtime_revision"]
    assert job["runtime_build"] == job["resolved"]["runtime_build"]
    assert job["runtime_image_id"] == job["resolved"]["runtime_image_id"]
    assert job["resolved"]["runtime_revision"] == job["resolved"]["sd_cpp_commit"]
