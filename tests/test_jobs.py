from copy import deepcopy
from io import BytesIO, StringIO
import threading
import time

from PIL import Image
import pytest

from conftest import install_sparse, wait_for_job
from mirage.errors import WorkbenchError


def test_fake_generation_publishes_gallery_and_sidecar(app, registry):
    install_sparse(registry, "zimage")
    app.queue.start()
    job = app.submit_generation({"prompt": "A red fox", "model": "zimage", "resolution": "sd", "quality": "fast"})
    finished = wait_for_job(app, job["id"])
    assert finished["status"] == "completed", finished["error"]
    image = app.database.get_image(finished["output_id"])
    assert (app.paths.outputs / image["filename"]).is_file()
    assert (app.paths.outputs / image["filename"]).with_suffix(".json").is_file()
    assert app.media.thumbnail_path(image["id"]).is_file()
    assert image["metadata"]["seed"] == job["resolved"]["seed"]
    assert image["metadata"]["elapsed_seconds"] >= 0


def test_publication_recovers_after_database_failure(app, registry):
    install_sparse(registry, "zimage")
    job = app.submit_generation({"prompt": "Recover", "model": "zimage", "quality": "fast"})
    app.database.update_job(job["id"], status="running")
    job["duration_seconds"] = 1.25
    temporary = app.paths.outputs / ".recovery.png"
    Image.new("RGB", (64, 64), "green").save(temporary, "PNG")
    final = app.paths.outputs / job["resolved"]["output_name"]
    original_add_image = app.database.add_image
    def fail_once(*args, **kwargs):
        raise RuntimeError("simulated database failure")
    app.database.add_image = fail_once
    with pytest.raises(RuntimeError, match="simulated"):
        app.media.publish(job, temporary, final)
    assert final.is_file()
    assert list(app.paths.outputs.glob(".*.publication.json"))
    app.database.add_image = original_add_image
    recovered = app.media.reconcile_publications()
    assert len(recovered) == 1
    finished = app.database.get_job(job["id"])
    assert finished["status"] == "completed"
    assert finished["duration_seconds"] == 1.25
    assert app.media.thumbnail_path(finished["output_id"]).is_file()
    assert final.with_suffix(".json").is_file()
    assert not list(app.paths.outputs.glob(".*.publication.json"))


def test_queued_job_keeps_config_snapshot(app, registry):
    install_sparse(registry, "qwen")
    job = app.submit_generation({"prompt": "Original", "model": "qwen", "quality": "medium"})
    document = deepcopy(app.config.snapshot()["document"])
    document["models"]["qwen"]["quality"]["medium"]["steps"] = 40
    app.config.update(document)
    assert job["resolved"]["steps"] == 25
    assert app.database.get_job(job["id"])["resolved"]["steps"] == 25
    settings = deepcopy(app.settings)
    settings["backend_profiles"]["vulkan-balanced"]["backend"] = "te=cpu,diffusion=cpu,vae=cpu"
    app.update_settings(settings)
    new_job = app.submit_generation({"prompt": "New", "model": "qwen", "quality": "medium"})
    assert new_job["resolved"]["steps"] == 40
    assert new_job["resolved"]["backend_profile"]["backend"] == "te=cpu,diffusion=cpu,vae=cpu"
    assert job["resolved"]["backend_profile"]["backend"] == "te=cpu,clip_vision=cpu,diffusion=vulkan0,vae=vulkan0"
    assert app.runner.command(job, app.paths.outputs / "old.png") != app.runner.command(new_job, app.paths.outputs / "new.png")


def test_qwen_edit_uses_projector_and_four_bit_cache(app, registry):
    install_sparse(registry, "qwen", editing=True)
    buffer = BytesIO()
    Image.new("RGB", (64, 64), "red").save(buffer, "PNG")
    reference = app.media.upload(buffer.getvalue(), "car.png")
    job = app.submit_generation({"prompt": "Make it classic", "model": "qwen", "reference_ids": [reference["id"]]})
    command = app.runner.command(job, app.paths.outputs / "temporary.png")
    assert "--llm_vision" in command
    assert command[command.index("--backend") + 1] == "te=cpu,clip_vision=cpu,diffusion=vulkan0,vae=vulkan0"
    assert "--offload-to-cpu" in command
    assert command[command.index("--model-args") + 1] == "qwen_image_2_1_prefix_cache_type=q4_1"
    assert f"/inputs/{reference['filename']}" in command


def test_qwen_text_generation_keeps_default_cache_with_editing_installed(app, registry):
    install_sparse(registry, "qwen", editing=True)
    job = app.submit_generation({"prompt": "A classic car", "model": "qwen"})
    command = app.runner.command(job, app.paths.outputs / "temporary.png")
    assert "--llm_vision" not in command
    assert "--model-args" not in command


def test_cuda_job_requests_nvidia_gpu_without_dri_devices(app, registry, monkeypatch):
    install_sparse(registry, "zimage")
    monkeypatch.delenv("MIRAGE_DRI_DEVICES", raising=False)
    monkeypatch.delenv("MIRAGE_ALLOW_NO_GPU", raising=False)
    job = app.submit_generation({"prompt": "CUDA", "model": "zimage"})
    job["resolved"]["runtime_backend"] = "cuda"
    release_bin = app.paths.data / "runtimes" / "releases" / job["resolved"]["runtime_build"] / "bin"
    release_bin.mkdir(parents=True)
    (release_bin / "sd-cli").touch()
    captured = {}

    class NvidiaEngine:
        engine = "docker"

        def require_runtime_image(self, image):
            pass

        def create(self, *args, **kwargs):
            captured.update(kwargs)
            captured["devices"] = args[4]
            return "nvidia-container"

        def start(self, container):
            raise RuntimeError("stop after container creation")

        def remove(self, container, force=False):
            pass

    app.runner.engine = NvidiaEngine()
    with pytest.raises(RuntimeError, match="stop after"):
        app.runner.run_container(job, [], StringIO(), threading.Event())

    assert captured["devices"] == []
    assert captured["device_requests"] == [{"Driver": "nvidia", "Count": -1, "Capabilities": [["gpu"]]}]


def test_reference_rejected_for_non_edit_model(app, registry):
    install_sparse(registry, "zimage")
    buffer = BytesIO()
    Image.new("RGB", (64, 64)).save(buffer, "PNG")
    reference = app.media.upload(buffer.getvalue())
    with pytest.raises(WorkbenchError, match="does not support"):
        app.submit_generation({"prompt": "No", "model": "zimage", "reference_ids": [reference["id"]]})


def test_retry_uses_new_output_and_preserves_original(app, registry):
    install_sparse(registry, "zimage")
    app.queue.start()
    original = app.submit_generation({"prompt": "Retry", "model": "zimage", "quality": "fast"})
    original = wait_for_job(app, original["id"])
    retry = app.queue.retry(original["id"])
    assert retry["resolved"]["output_name"] != original["resolved"]["output_name"]
    assert retry["retry_of"] == original["id"]
    retry = wait_for_job(app, retry["id"])
    assert retry["status"] == "completed"
    assert (app.paths.outputs / original["resolved"]["output_name"]).is_file()


def test_cancellation_does_not_publish_image(app, registry):
    install_sparse(registry, "zimage")
    app.queue.start()
    job = app.submit_generation({"prompt": "Cancel", "model": "zimage", "overrides": {"steps": 250}})
    deadline = time.monotonic() + 3
    while app.database.get_job(job["id"])["status"] != "running" and time.monotonic() < deadline:
        time.sleep(0.01)
    app.queue.cancel(job["id"])
    finished = wait_for_job(app, job["id"])
    assert finished["status"] == "cancelled"
    assert not (app.paths.outputs / job["resolved"]["output_name"]).exists()


@pytest.mark.parametrize("cancelled,expected_code", [(True, "job_cancelled"), (False, "engine_error")])
def test_container_wait_after_removal_respects_cancellation(app, registry, monkeypatch, cancelled, expected_code):
    install_sparse(registry, "zimage")
    monkeypatch.setenv("MIRAGE_ALLOW_NO_GPU", "1")
    job = app.submit_generation({"prompt": "Cancel running container", "model": "zimage"})
    release_bin = app.paths.data / "runtimes" / "releases" / job["resolved"]["runtime_build"] / "bin"
    release_bin.mkdir(parents=True)
    (release_bin / "sd-cli").touch()

    class RemovedContainerEngine:
        def require_runtime_image(self, image):
            pass

        def create(self, *args, **kwargs):
            return "removed-container"

        def start(self, container):
            pass

        def logs(self, container):
            return BytesIO(b""), StringIO()

        def wait(self, container):
            raise WorkbenchError("engine_error", "Container engine POST /containers/removed-container/wait?condition=not-running: no such container", 500)

        def remove(self, container, force=False):
            pass

    app.runner.engine = RemovedContainerEngine()
    cancel = threading.Event()
    if cancelled:
        cancel.set()
    with pytest.raises(WorkbenchError) as error:
        app.runner.run_container(job, [], StringIO(), cancel)
    assert error.value.code == expected_code


def test_queued_cancellation_removes_reservation(app, registry):
    install_sparse(registry, "zimage")
    job = app.submit_generation({"prompt": "Cancel queued", "model": "zimage"})
    app.queue.cancel(job["id"])
    assert app.database.get_job(job["id"])["status"] == "cancelled"
    assert not (app.paths.outputs / job["resolved"]["output_name"]).exists()


def test_restart_marks_running_interrupted(app, registry):
    install_sparse(registry, "zimage")
    job = app.submit_generation({"prompt": "Restart", "model": "zimage"})
    app.database.update_job(job["id"], status="running")
    assert app.database.reconcile() == 1
    assert app.database.get_job(job["id"])["status"] == "interrupted"


def test_trash_then_permanent_delete(app, registry):
    install_sparse(registry, "zimage")
    app.queue.start()
    job = app.submit_generation({"prompt": "Delete", "model": "zimage", "quality": "fast"})
    job = wait_for_job(app, job["id"])
    output = app.paths.outputs / job["resolved"]["output_name"]
    app.database.trash_image(job["output_id"])
    app.database.trash_image(job["output_id"], permanent=True)
    output.unlink(missing_ok=True)
    assert not output.exists()
