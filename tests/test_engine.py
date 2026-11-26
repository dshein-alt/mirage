import os
import select
import shlex
import shutil
import subprocess
import time

import pytest

from mirage.engine import EngineClient, LINE_OUTPUT_ENTRYPOINT, nvidia_device_requests


@pytest.mark.skipif(not shutil.which("perl") or not shutil.which("bash"), reason="requires the runtime's shell utilities")
def test_progress_frames_are_flushed_before_next_step_and_preserve_failure(tmp_path):
    renderer = tmp_path / "sd-cli"
    renderer.write_text(r'''#!/bin/sh
printf '\r  |=>   | 1/40 - 39.07s/it\033['
sleep 0.05
printf 'K'
read -r next_step
printf '\r  |==>  | 2/40 - 39.15s/it\033[K\n'
printf 'renderer failed\n' >&2
exit 17
''')
    renderer.chmod(0o755)
    *shell, script, name = LINE_OUTPUT_ENTRYPOINT
    entrypoint = [*shell, script.replace("/usr/local/bin/sd-cli", shlex.quote(str(renderer)), 1), name]
    with subprocess.Popen(entrypoint, stdin=subprocess.PIPE, stdout=subprocess.PIPE) as process:
        try:
            received = b""
            deadline = time.monotonic() + 5
            while b"\n" not in received:
                ready, _, _ = select.select([process.stdout], [], [], max(0, deadline - time.monotonic()))
                assert ready, "the first progress frame was buffered"
                block = os.read(process.stdout.fileno(), 4096)
                assert block, "the renderer closed before its first progress frame"
                received += block
            assert received == b"  |=>   | 1/40 - 39.07s/it\n"
        finally:
            process.stdin.close()
        assert process.stdout.read() == b"  |==>  | 2/40 - 39.15s/it\nrenderer failed\n"
        assert process.wait(timeout=5) == 17


def test_podman_containers_use_host_identity_and_keep_id(monkeypatch):
    monkeypatch.setenv("MIRAGE_HOST_UID", "1000")
    monkeypatch.setenv("MIRAGE_HOST_GID", "1001")
    monkeypatch.setenv("MIRAGE_HOST_GROUPS", "1001,992")
    client = EngineClient(engine="podman")
    captured = {}

    def request(method, path, body=None, **kwargs):
        captured.update(body)
        return {"Id": "container-id"}

    client.request = request
    assert client.create("job", "image", [], []) == "container-id"
    assert captured["User"] == "1000:1001"
    assert captured["HostConfig"]["UsernsMode"] == "keep-id"
    assert captured["HostConfig"]["GroupAdd"] == ["1001", "992"]


@pytest.mark.parametrize("engine,expected", [
    ("docker", [{"Driver": "nvidia", "Count": -1, "Capabilities": [["gpu"]]}]),
    ("podman", [{"Driver": "cdi", "DeviceIDs": ["nvidia.com/gpu=all"]}]),
])
def test_nvidia_device_requests(monkeypatch, engine, expected):
    client = EngineClient(engine=engine)
    captured = {}
    client.request = lambda method, path, body=None, **kwargs: captured.update(body) or {"Id": "container-id"}

    assert client.create("job", "image", [], [], device_requests=nvidia_device_requests(engine)) == "container-id"

    assert captured["HostConfig"]["DeviceRequests"] == expected
    if engine == "podman":
        assert captured["HostConfig"]["SecurityOpt"] == ["label=disable"]


def test_offline_podman_migration_uses_keep_id(tmp_path):
    from test_installer import make_installer

    installer = make_installer(tmp_path)
    installer.engine = "podman"
    installer.data = tmp_path / "data"
    arguments = []
    installer.run_engine = lambda *args: arguments.extend(args)
    installer.offline_migrate({"id": "image-id"}, "deployment-id")
    assert "--userns=keep-id" in arguments
    assert f"{installer.data}:/data:rw" in arguments
    assert "MIRAGE_MAINTENANCE=0" in arguments


def test_migration_and_service_start_leave_maintenance_with_the_installer(paths, tmp_path, monkeypatch):
    from mirage.service import Application
    from test_installer import installer_module, make_installer

    installer = make_installer(tmp_path)
    installer.engine = "podman"
    installer.data = paths.data
    monkeypatch.setattr(installer, "engine_socket", lambda: "/tmp/podman.sock")

    def initialize(*arguments):
        with monkeypatch.context() as environment:
            for argument in arguments:
                if argument.startswith(("MIRAGE_MAINTENANCE=", "MIRAGE_DEPLOYMENT_ID=")):
                    name, value = argument.split("=", 1)
                    environment.setenv(name, value)
            service = Application(paths)
            try:
                return service.database.maintenance_status()
            finally:
                service.close()

    monkeypatch.setattr(installer, "run_engine", initialize)
    image = {"id": "image-id"}
    deployment = "app-fixture"
    assert not paths.database.exists()
    installer.offline_migrate(image, deployment)
    installer_module.enter_maintenance(paths.data, installer.operation_id)
    installer.offline_migrate(image, deployment)

    arguments = installer.container_arguments("mirage", paths.data, image["id"], deployment, final=True)
    assert initialize(*arguments)["owner"] == installer.operation_id
    installer_module.leave_maintenance(paths.data, installer.operation_id)
    assert not initialize(*arguments)["active"]
