from copy import deepcopy

import pytest

from mirage.config import ConfigService
from mirage.errors import WorkbenchError
from mirage.output import reserve_output
from mirage.util import read_json


def test_database_filename_is_mirage(paths):
    assert paths.database.name == "mirage.sqlite3"


def test_shipped_defaults_and_resolution(paths, registry):
    service = ConfigService(paths, registry)
    resolved = service.resolve("qwen", "sd", "fast")
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
    assert service.snapshot()["document"]["schema_version"] == 2


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


def test_server_access_defaults(paths, registry):
    settings = ConfigService(paths, registry).settings()
    assert settings["server"] == {"mode": "local", "port": 8765}


@pytest.mark.parametrize("server", [
    {"mode": "lan", "port": 8765},
    {"mode": "local", "port": 0},
    {"mode": "local", "port": 65536},
    {"mode": "local", "port": True},
])
def test_invalid_server_access_settings(paths, registry, server):
    service = ConfigService(paths, registry)
    document = service.settings()
    document["server"] = server
    with pytest.raises(ValueError, match="server"):
        service.update_settings(document)


def test_server_access_changes_require_installer(app):
    document = deepcopy(app.settings)
    document["server"] = {"mode": "authenticated", "port": 9000}
    with pytest.raises(WorkbenchError, match="installer"):
        app.update_settings(document)


def test_access_environment_controls_listener_and_mode(monkeypatch):
    monkeypatch.setenv("MIRAGE_MODE", "authenticated")
    monkeypatch.setenv("MIRAGE_PORT", "9000")
    server = ConfigService.resolve_server({"mode": "local", "port": 8765})
    assert server == {"mode": "authenticated", "port": 9000}
    monkeypatch.setenv("MIRAGE_PORT", "invalid")
    with pytest.raises(ValueError, match="MIRAGE_PORT"):
        ConfigService.resolve_server(server)
