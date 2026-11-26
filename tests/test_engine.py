from mirage.engine import EngineClient


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
