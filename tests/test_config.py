from copy import deepcopy

import pytest

from sd_workbench.config import ConfigService
from sd_workbench.errors import WorkbenchError
from sd_workbench.output import reserve_output
from sd_workbench.util import read_json


def test_shipped_defaults_and_resolution(paths, registry):
    service = ConfigService(paths, registry)
    resolved = service.resolve("qwen", "medium", "fast")
    assert resolved["steps"] == 15
    assert resolved["width"] == resolved["height"] == 512
    assert resolved["cfg_scale"] == 6.0
    assert service.resolve("zimage")["quality"] == "high"


def test_invalid_reload_keeps_last_known_good(paths, registry):
    service = ConfigService(paths, registry)
    before = service.snapshot()
    document = deepcopy(before["document"])
    document["models"]["qwen"]["quality"]["medium"]["steps"] = 0
    paths.generation_config.write_text(__import__("json").dumps(document))
    with pytest.raises(WorkbenchError, match="steps"):
        service.reload()
    assert service.snapshot() == before
    assert service.resolve("qwen", quality_id="medium")["steps"] == 25


def test_duplicate_json_key_is_rejected(paths, registry):
    service = ConfigService(paths, registry)
    paths.generation_config.write_text('{"schema_version":1,"schema_version":1}')
    with pytest.raises(WorkbenchError, match="duplicate"):
        service.reload()
    assert service.snapshot()["document"]["schema_version"] == 1


@pytest.mark.parametrize("field,value", [("width", 255), ("height", 0), ("steps", 251), ("cfg_scale", -1)])
def test_invalid_overrides(paths, registry, field, value):
    service = ConfigService(paths, registry)
    with pytest.raises(WorkbenchError):
        service.resolve("moss", overrides={field: value})


def test_atomic_update_changes_revision(paths, registry):
    service = ConfigService(paths, registry)
    before = service.snapshot()
    document = deepcopy(before["document"])
    document["models"]["qwen"]["quality"]["medium"]["steps"] = 27
    after = service.update(document)
    assert after["revision"] != before["revision"]
    assert service.resolve("qwen", quality_id="medium")["steps"] == 27
    assert read_json(paths.generation_config)["models"]["qwen"]["quality"]["medium"]["steps"] == 27


def test_settings_reject_arbitrary_flags(paths, registry):
    service = ConfigService(paths, registry)
    document = service.settings()
    document["models"]["zimage"]["extra_args"] = ["--unsafe-user-flag"]
    with pytest.raises(ValueError, match="unsupported extra argument"):
        service.update_settings(document)
    assert service.settings()["models"]["zimage"]["extra_args"] == ["--diffusion-fa"]


def test_output_names_never_overwrite(paths):
    first = reserve_output(paths.outputs, "z-image-turbo", "large", "high")
    second = reserve_output(paths.outputs, "z-image-turbo", "large", "high")
    assert first.name.endswith("-0001.png")
    assert second.name.endswith("-0002.png")
    third = reserve_output(paths.outputs, "z-image-turbo", "large", "high")
    assert third.name.endswith("-0003.png")
