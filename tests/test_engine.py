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
