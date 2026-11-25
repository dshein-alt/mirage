import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("mirage_installer", ROOT / "install.py")
installer_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer_module)


def make_installer(tmp_path, arguments=None):
    args = installer_module.parser().parse_args([*(arguments or []), "--state-file", str(tmp_path / "state" / "installation.json")])
    return installer_module.Installer(args)


def test_default_operation_and_option_precedence(tmp_path, monkeypatch):
    installer = make_installer(tmp_path)
    assert installer.args.operation == "install"
    installer.state = {"data_dir": "/persisted", "prefix": "/old-prefix", "engine": "podman", "models": "moss", "server": {"mode": "authenticated", "port": 9000}}
    monkeypatch.setenv("MIRAGE_DATA_DIR", "/environment")
    monkeypatch.setenv("MIRAGE_PREFIX", "/env-prefix")
    monkeypatch.setenv("MIRAGE_MODELS", "qwen")
    assert installer.option("data_dir", "MIRAGE_DATA_DIR", "data_dir", "/default") == "/environment"
    assert installer.option("prefix", "MIRAGE_PREFIX", "prefix", "/default") == "/env-prefix"
    assert installer.option("engine", "MIRAGE_CONTAINER_ENGINE", "engine", "docker") == "podman"
    installer.args.data_dir = "/explicit"
    assert installer.option("data_dir", "MIRAGE_DATA_DIR", "data_dir", "/default") == "/explicit"


def test_server_option_precedence(tmp_path, monkeypatch):
    installer = make_installer(tmp_path)
    installer.state = {"server": {"mode": "authenticated", "port": 9000}}
    installer.resolve_server_options()
    assert (installer.mode, installer.port) == ("authenticated", 9000)
    monkeypatch.setenv("MIRAGE_MODE", "local")
    monkeypatch.setenv("MIRAGE_PORT", "9100")
    installer.resolve_server_options()
    assert (installer.mode, installer.port) == ("local", 9100)
    explicit = make_installer(tmp_path, ["--mode", "authenticated", "--port", "9200"])
    explicit.resolve_server_options()
    assert (explicit.mode, explicit.port) == ("authenticated", 9200)


@pytest.mark.skipif(shutil.which("git") is None, reason="release fixture requires Git")
def test_standalone_release_fixture_uses_persistent_versioned_source(tmp_path, monkeypatch):
    repository = tmp_path / "release-repository"
    (repository / "stable-diffusion.cpp" / "ggml" / "src").mkdir(parents=True)
    (repository / "stable-diffusion.cpp" / "ggml" / "src" / "source.cpp").write_text("source")
    (repository / "pyproject.toml").write_text("[project]\nname='mirage'\n")
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "release"], check=True)
    subprocess.run(["git", "-C", str(repository), "tag", "v1.0.0"], check=True)
    commit = subprocess.run(["git", "-C", str(repository), "rev-parse", "HEAD"], check=True, text=True, capture_output=True).stdout.strip()
    release = tmp_path / "release.json"
    release.write_text(json.dumps({"tag_name": "v1.0.0", "repository": str(repository)}))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "persistent"))
    installer = make_installer(tmp_path, ["--standalone", "--release-api", release.as_uri()])

    reference = installer.resolve_source()
    assert reference["kind"] == "release"
    assert reference["commit"] == commit
    assert installer.source == tmp_path / "persistent" / "mirage" / "sources" / f"v1.0.0-{commit[:12]}"
    assert (installer.source / "stable-diffusion.cpp" / "ggml" / "src").is_dir()


def recovery_fixture(tmp_path, committed=False):
    installer = make_installer(tmp_path)
    data = tmp_path / "data"
    (data / "config").mkdir(parents=True)
    (data / "runtimes" / "releases").mkdir(parents=True)
    (data / "config" / "settings.json").write_text('{"server":{"port":8765}}')
    installer_module.connect_database(data).close()
    backup = tmp_path / "backup"
    (backup / "config").mkdir(parents=True)
    (backup / "config" / "settings.json").write_text('{"original":true}')
    installer_module.backup_sqlite(data / "mirage.sqlite3", backup / "mirage.sqlite3")
    (data / "config" / "settings.json").write_text('{"migrated":true}')
    (data / "runtimes" / "current").symlink_to("releases/new")
    installer.engine = "podman"
    installer.journal = {
        "operation_id": "recovery-test",
        "operation": "update",
        "data_dir": str(data),
        "original_state": {},
        "original_running": False,
        "original_runtime_link": "releases/old",
        "candidate_container": None,
        "verifier_container": None,
        "original_container": None,
        "committed": committed,
        "pending_action": None,
        "actions": ["backup", "migrate", "activate-runtime"],
        "backup_dir": str(backup),
    }
    installer_module.enter_maintenance(data, "recovery-test")
    return installer, data, backup


def test_recovery_before_commit_restores_backup_and_activation(tmp_path):
    installer, data, backup = recovery_fixture(tmp_path)
    installer.recover()
    assert json.loads((data / "config" / "settings.json").read_text()) == {"original": True}
    assert os.readlink(data / "runtimes" / "current") == "releases/old"
    connection = installer_module.connect_database(data)
    assert connection.execute("SELECT active FROM maintenance").fetchone()[0] == 0
    connection.close()
    assert not installer.journal_path.exists()


def test_recovery_after_commit_preserves_migrated_database(tmp_path):
    installer, data, backup = recovery_fixture(tmp_path, committed=True)
    installer.recover()
    assert json.loads((data / "config" / "settings.json").read_text()) == {"migrated": True}
    assert os.readlink(data / "runtimes" / "current") == "releases/new"
    connection = installer_module.connect_database(data)
    assert connection.execute("SELECT active FROM maintenance").fetchone()[0] == 0
    connection.close()


def test_verification_copy_reopens_admission_without_changing_live_database(tmp_path):
    installer = make_installer(tmp_path)
    data = tmp_path / "live"
    (data / "config").mkdir(parents=True)
    (data / "runtimes" / "releases" / "build-test" / "bin").mkdir(parents=True)
    (data / "config" / "settings.json").write_text(json.dumps({"server": {"mode": "local", "port": 8765}}))
    installer.data = data
    installer.mode = "local"
    installer_module.connect_database(data).close()
    installer_module.enter_maintenance(data, installer.operation_id)
    connection = installer_module.connect_database(data)
    connection.execute("INSERT INTO admitted_writes(token, started_at) VALUES('finished-write', 'now')")
    connection.close()

    verification, port = installer.verification_data("build-test")
    live = installer_module.connect_database(data)
    copied = installer_module.connect_database(verification)
    assert live.execute("SELECT active FROM maintenance").fetchone()[0] == 1
    assert copied.execute("SELECT active FROM maintenance").fetchone()[0] == 0
    assert copied.execute("SELECT COUNT(*) FROM admitted_writes").fetchone()[0] == 0
    assert port != 8765
    assert json.loads((data / "config" / "settings.json").read_text())["server"]["port"] == 8765
    live.close()
    copied.close()


def test_container_publishes_local_port_without_host_networking(tmp_path, monkeypatch):
    installer = make_installer(tmp_path)
    installer.engine = "podman"
    monkeypatch.setattr(installer, "engine_socket", lambda: "/tmp/podman.sock")
    data = tmp_path / "data"
    (data / "config").mkdir(parents=True)
    settings = data / "config" / "settings.json"
    settings.write_text(json.dumps({"server": {"mode": "local", "port": 9000}}))

    arguments = installer.container_arguments("mirage", data, {"id": "image-id"}, "deployment")
    assert "--network" not in arguments
    assert arguments[arguments.index("--publish") + 1] == "127.0.0.1:9000:9000"
    assert "MIRAGE_BIND_HOST=0.0.0.0" in arguments

    settings.write_text(json.dumps({"server": {"mode": "authenticated", "port": 9100}}))
    arguments = installer.container_arguments("mirage", data, {"id": "image-id"}, "deployment")
    assert arguments[arguments.index("--publish") + 1] == "0.0.0.0:9100:9100"
    arguments = installer.container_arguments("verifier", data, {"id": "image-id"}, "deployment", verify_only=True)
    assert arguments[arguments.index("--publish") + 1] == "127.0.0.1:9100:9100"


def test_authenticated_install_requires_both_valid_distinct_tokens(tmp_path, monkeypatch):
    installer = make_installer(tmp_path, ["--mode", "authenticated", "--non-interactive"])
    installer.mode = "authenticated"
    monkeypatch.delenv("MIRAGE_API_TOKEN", raising=False)
    monkeypatch.delenv("MIRAGE_BROWSER_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="MIRAGE_API_TOKEN"):
        installer.credentials()

    monkeypatch.setenv("MIRAGE_API_TOKEN", "admin-secret_12345")
    monkeypatch.setenv("MIRAGE_BROWSER_TOKEN", "admin-secret_12345")
    with pytest.raises(RuntimeError, match="different"):
        installer.credentials()

    monkeypatch.setenv("MIRAGE_BROWSER_TOKEN", "browser-secret_12345")
    assert installer.credentials() == "admin-secret_12345"
    assert os.environ["MIRAGE_BROWSER_TOKEN"] == "browser-secret_12345"


def test_local_install_does_not_require_tokens(tmp_path, monkeypatch):
    installer = make_installer(tmp_path)
    installer.mode = "local"
    monkeypatch.delenv("MIRAGE_API_TOKEN", raising=False)
    monkeypatch.delenv("MIRAGE_BROWSER_TOKEN", raising=False)
    assert installer.credentials() == ""


def test_first_party_service_paths_do_not_use_host_networking():
    launcher = (ROOT / "bin" / "mirage").read_text()
    installer = (ROOT / "install.py").read_text()
    compose = (ROOT / "compose.yaml").read_text()
    authenticated = (ROOT / "compose.authenticated.yaml").read_text()
    assert "--network host" not in launcher
    assert '"--network", "host"' not in installer
    assert '--network "container:$NAME"' in launcher
    assert "network_mode: host" not in compose + authenticated
    assert "MIRAGE_PUBLISH_HOST" not in compose + authenticated
    assert '127.0.0.1:${MIRAGE_PORT:-8765}:${MIRAGE_PORT:-8765}' in compose
    assert '0.0.0.0:${MIRAGE_PORT:-8765}:${MIRAGE_PORT:-8765}' in authenticated


def test_initialize_config_persists_access_mode_and_port(tmp_path):
    installer = make_installer(tmp_path, ["--mode", "authenticated", "--port", "9000"])
    installer.source = ROOT
    installer.data = tmp_path / "data"
    (installer.data / "config").mkdir(parents=True)
    installer.mode = "authenticated"
    installer.port = 9000
    installer.initialize_config()
    settings = json.loads((installer.data / "config" / "settings.json").read_text())
    assert settings["server"] == {"mode": "authenticated", "port": 9000}
    assert settings["queue"]["concurrency"] == 1


def test_health_check_uses_admin_token(tmp_path, monkeypatch):
    installer = make_installer(tmp_path)
    calls = []

    def response(port, path, method="GET", token=""):
        calls.append(token)
        return {"deployment_id": "deployment", "container_image": "image-id"}

    monkeypatch.setattr(installer, "http", response)
    assert installer.wait_health(8765, "deployment", "image-id", "admin-secret_12345")["deployment_id"] == "deployment"
    assert calls == ["admin-secret_12345"]


def test_recovery_health_check_accepts_rotated_credential_response(tmp_path, monkeypatch):
    from urllib.error import HTTPError

    installer = make_installer(tmp_path)

    def unauthorized(port, path, method="GET", token=""):
        raise HTTPError(path, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(installer, "http", unauthorized)
    assert installer.wait_health(8765, "old-deployment", "old-image", "new-token", allow_unauthorized=True) == {"authenticated": False}
