from copy import deepcopy
import json

from PIL import Image
import pytest

from conftest import install_sparse
from mirage.config import ConfigService, RESOLUTIONS
from mirage.errors import WorkbenchError
from mirage.util import file_sha256, read_json


def test_exact_size_presets(paths, registry):
    service = ConfigService(paths, registry)
    assert service.snapshot()["document"]["defaults"]["resolution"] == "hd"
    assert service.snapshot()["document"]["resolutions"] == RESOLUTIONS
    assert service.resolve("qwen", "fhd")["width"] == 1920
    assert service.resolve("qwen", "fhd")["height"] == 1080
    document = deepcopy(service.snapshot()["document"])
    document["resolutions"]["hd"] = {"width": 1024, "height": 768}
    with pytest.raises(ValueError, match="exactly"):
        service.update(document)


def test_legacy_migration_preserves_sampling_and_warns_on_fallback(paths, registry):
    initial = ConfigService(paths, registry)
    legacy = {
        "schema_version": 1,
        "defaults": {"model": "qwen", "resolution": "portrait", "quality": "medium"},
        "resolutions": {"portrait": {"width": 768, "height": 1152}},
        "models": deepcopy(initial.snapshot()["document"]["models"]),
    }
    legacy["models"]["qwen"]["quality"]["medium"]["steps"] = 31
    legacy["models"]["qwen"]["cfg_scale"] = 5.5
    paths.generation_config.write_text(json.dumps(legacy))

    migrated = ConfigService(paths, registry)
    snapshot = migrated.snapshot()
    assert snapshot["document"]["defaults"]["resolution"] == "hd"
    assert snapshot["document"]["models"]["qwen"]["quality"]["medium"]["steps"] == 31
    assert snapshot["document"]["models"]["qwen"]["cfg_scale"] == 5.5
    assert any("unsupported" in warning for warning in snapshot["warnings"])
    backups = list(paths.generation_config.parent.glob("generation-presets.pre-mirage-*.json"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text()) == legacy


def test_fhd_publication_crops_before_hash_and_metadata(app, registry):
    install_sparse(registry, "zimage")
    job = app.submit_generation({"prompt": "FHD", "model": "zimage", "resolution": "fhd", "quality": "fast"})
    app.database.update_job(job["id"], status="running")
    temporary = app.paths.outputs / ".fhd-aligned.png"
    Image.new("RGB", (1920, 1088), "green").save(temporary, "PNG")
    final = app.paths.outputs / job["resolved"]["output_name"]

    record = app.media.publish(job, temporary, final)
    with Image.open(final) as image:
        assert image.size == (1920, 1080)
    assert (record["width"], record["height"]) == (1920, 1080)
    assert record["dimensions_verified"] is True
    assert record["sha256"] == file_sha256(final)
    sidecar = read_json(final.with_suffix(".json"))
    assert (sidecar["width"], sidecar["height"]) == (1920, 1080)
    assert sidecar["sha256"] == record["sha256"] == file_sha256(final)


def test_fhd_publication_rejects_undersized_result(app, registry):
    install_sparse(registry, "zimage")
    job = app.submit_generation({"prompt": "Too small", "model": "zimage", "resolution": "fhd"})
    temporary = app.paths.outputs / ".fhd-small.png"
    Image.new("RGB", (1920, 1072), "green").save(temporary, "PNG")
    with pytest.raises(WorkbenchError, match="smaller"):
        app.media.publish(job, temporary, app.paths.outputs / job["resolved"]["output_name"])


def test_historical_dimensions_backfill_and_filters(app, registry):
    install_sparse(registry, "zimage")
    job = app.submit_generation({"prompt": "Legacy", "model": "zimage"})
    temporary = app.paths.outputs / ".legacy.png"
    Image.new("RGB", (768, 1152), "purple").save(temporary, "PNG")
    final = app.paths.outputs / job["resolved"]["output_name"]
    record = app.media.publish(job, temporary, final)
    original_sidecar = final.with_suffix(".json").read_bytes()
    app.database.connection.execute(
        "UPDATE images SET width=1024, height=1024, dimensions_verified=0 WHERE id=?", (record["id"],)
    )

    assert app.media.backfill_dimensions() == [record["id"]]
    assert final.with_suffix(".json").read_bytes() == original_sidecar
    assert app.database.image_dimensions() == [{"width": 768, "height": 1152}]
    assert [image["id"] for image in app.database.list_images(width=768, height=1152)] == [record["id"]]
    assert app.database.list_images(width=768, height=1024) == []


def test_new_requests_reject_dimension_overrides(app, registry):
    install_sparse(registry, "zimage")
    for request in ({"width": 512}, {"height": 512}, {"overrides": {"width": 512}}):
        with pytest.raises(WorkbenchError) as error:
            app.submit_generation({"prompt": "Override", "model": "zimage", **request})
        assert error.value.code == "dimension_override"
