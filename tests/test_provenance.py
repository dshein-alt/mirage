import json
from pathlib import Path
from unittest.mock import patch

from mirage import app_build
from mirage.provenance import BUILD_INPUTS, container_git, source_identity


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


def test_container_git_mounts_the_checkout_for_submodule_provenance(tmp_path):
    source_fixture(tmp_path)
    with patch("mirage.provenance.git_output", side_effect=[COMMIT, ""]) as run:
        manifest = source_identity(tmp_path, git_command=container_git(tmp_path, "podman"))

    assert manifest["sd_cpp_commit"] == COMMIT
    first, second = [call.args[0] for call in run.call_args_list]
    assert first[:3] == ["podman", "run", "--rm"]
    assert "--userns=keep-id" in first
    assert f"{tmp_path}:/checkout:ro" in first
    assert first[-4:] == ["-C", "/checkout/stable-diffusion.cpp", "rev-parse", "HEAD"]
    assert second[second.index("-C") + 1:][:2] == ["/checkout", "status"]


def test_app_rebuilds_after_uninstall_removed_its_cached_image(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    data = tmp_path / "data"
    identity = app_build.app_identity(root, {reference: reference for reference in app_build.BASE_IMAGES})
    manifest_path = data / "releases" / "app" / identity["app_build_id"] / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps({"app_image": {"id": "removed-image"}}))
    commands = []
    monkeypatch.setattr(app_build, "run", lambda arguments: commands.append(arguments))

    def image_id(engine, reference):
        if reference == "removed-image":
            raise RuntimeError("image removed")
        return reference

    monkeypatch.setattr(app_build, "image_id", image_id)

    manifest = app_build.build_app(root, data, "podman")

    assert any(command[:2] == ["podman", "build"] for command in commands)
    assert manifest["app_image"]["id"] != "removed-image"
    assert json.loads(manifest_path.read_text()) == manifest


def test_job_snapshot_contains_runtime_identity(app, registry):
    from conftest import install_sparse

    install_sparse(registry, "zimage")
    job = app.submit_generation({"prompt": "Identity", "model": "zimage"})
    assert job["resolved"]["sd_cpp_commit"] == job["resolved"]["runtime_revision"]
    assert job["runtime_build"] == job["resolved"]["runtime_build"]
    assert job["runtime_image_id"] == job["resolved"]["runtime_image_id"]
    assert job["resolved"]["runtime_revision"] == job["resolved"]["sd_cpp_commit"]
