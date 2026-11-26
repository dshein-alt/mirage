from contextlib import nullcontext
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import Mock
from urllib.error import HTTPError

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


def test_terminal_setup_selects_mode_port_models_and_tokens(tmp_path, monkeypatch):
    for name in ("MIRAGE_PORT", "MIRAGE_MODE", "MIRAGE_MODELS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MIRAGE_API_TOKEN", "")
    monkeypatch.setenv("MIRAGE_BROWSER_TOKEN", "")
    installer = make_installer(tmp_path, ["--standalone"])
    terminal = Mock()
    terminal.readline.side_effect = ["invalid\n", "2\n", "0\n", "9100\n", "unknown\n", "3, 4\n"]
    getpass = Mock(side_effect=["admin-token-fixture", "browser-token-fixture"])
    monkeypatch.setattr(installer_module.getpass, "getpass", getpass)

    installer.resolve_server_options(terminal)
    assert (installer.mode, installer.port) == ("authenticated", 9100)
    assert installer.selected_models(terminal) == "qwen-edit,moss"
    assert installer.credentials(terminal) == "admin-token-fixture"
    assert all(call.kwargs["stream"] is terminal for call in getpass.call_args_list)
    assert os.environ["MIRAGE_BROWSER_TOKEN"] == "browser-token-fixture"


def test_dialog_setup_prefills_saved_choices(tmp_path, monkeypatch):
    for name in ("MIRAGE_PORT", "MIRAGE_MODE", "MIRAGE_MODELS"):
        monkeypatch.delenv(name, raising=False)
    installer = make_installer(tmp_path, ["update"])
    installer.state = {"server": {"mode": "authenticated", "port": 9100}, "models": "qwen-edit"}
    terminal = Mock()
    prompt = Mock(side_effect=["local", "9200", '"qwen-edit" "moss"'])
    monkeypatch.setattr(installer_module, "dialog_prompt", prompt)

    installer.resolve_server_options(terminal, "dialog")
    assert (installer.mode, installer.port) == ("local", 9200)
    assert installer.selected_models(terminal, "dialog") == "qwen-edit,moss"
    choice, port, models = [call.args for call in prompt.call_args_list]
    assert choice[:5] == (terminal, "dialog", "--default-item", "authenticated", "--menu")
    assert port[2] == "--inputbox" and port[-1] == "9100"
    assert models[2] == "--checklist"
    assert models[models.index("qwen-edit") + 2] == "ON"


def test_explicit_setup_values_skip_prompts(tmp_path):
    installer = make_installer(tmp_path, ["--mode", "local", "--port", "9200", "--skip-models"])
    terminal = Mock()
    installer.resolve_server_options(terminal, "dialog")
    assert (installer.mode, installer.port, installer.selected_models(terminal, "dialog")) == ("local", 9200, "none")
    assert not terminal.mock_calls


def test_streamed_shell_installer_uses_python_without_host_tools(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "python3").symlink_to(sys.executable)
    remote = tmp_path / "install.py"
    remote.write_text(
        "import json, os, sys\n"
        "print(json.dumps([sys.argv[1:], os.environ.get('MIRAGE_MODELS')]))\n"
    )
    result = subprocess.run(
        [shutil.which("sh"), "-s", "--", "update", "--port", "9000"],
        input=(ROOT / "install.sh").read_text(), cwd=tmp_path, text=True,
        stdout=subprocess.PIPE, check=True,
        env={"PATH": str(tools), "MIRAGE_INSTALLER_URL": remote.as_uri()},
    )
    assert json.loads(result.stdout) == [
        ["--standalone", "update", "--port", "9000"],
        None,
    ]


def test_installer_downloads_selected_models_inside_service(tmp_path, monkeypatch):
    installer = make_installer(tmp_path)
    installer.port = 9000
    calls = []
    monkeypatch.setattr(installer, "run_engine", lambda *arguments: calls.append(arguments))

    installer.install_models("qwen-edit", {"id": "sha256:fixture"})

    assert calls == [(
        "exec", "-e", "MIRAGE_API_URL=http://127.0.0.1:9000",
        "mirage", "python", "-m", "mirage.cli", "models", "install", "qwen", "--wait", "--with-editing",
    )]


@pytest.mark.skipif(shutil.which("git") is None, reason="release fixture requires Git")
@pytest.mark.parametrize("engine", ["docker", "podman"])
@pytest.mark.parametrize("source", ["published", "tag", "head"])
def test_standalone_release_fixture_uses_persistent_versioned_source(tmp_path, monkeypatch, engine, source):
    repository = tmp_path / "release-repository"
    (repository / "stable-diffusion.cpp" / "ggml" / "src").mkdir(parents=True)
    (repository / "stable-diffusion.cpp" / "ggml" / "src" / "source.cpp").write_text("source")
    (repository / "pyproject.toml").write_text("[project]\nname='mirage'\n")
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "release"], check=True)
    released_commit = subprocess.check_output(["git", "-C", str(repository), "rev-parse", "HEAD"], text=True).strip()
    if source != "head":
        subprocess.run(["git", "-C", str(repository), "tag", "v1.0.0"], check=True)
    subprocess.run(["git", "-C", str(repository), "checkout", "-qb", "v1.0.0"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "--allow-empty", "-qm", "branch only"], check=True)
    head_commit = subprocess.check_output(["git", "-C", str(repository), "rev-parse", "HEAD"], text=True).strip()
    if source != "head":
        subprocess.run(["git", "-C", str(repository), "tag", "v1.9.0", released_commit], check=True)
        subprocess.run(["git", "-C", str(repository), "tag", "v1.10.0"], check=True)
    release = tmp_path / "release.json"
    release.write_text(json.dumps({"tag_name": "v1.0.0", "repository": str(repository)}))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "persistent"))
    arguments = ["--standalone", "--release-api", release.as_uri()]
    if source != "published":
        arguments += ["--repository", str(repository)]
        monkeypatch.setattr(installer_module, "urlopen", Mock(side_effect=HTTPError(release.as_uri(), 404, "missing", None, None)))
    installer = make_installer(tmp_path, arguments)
    installer.engine = engine
    calls = []

    def run_engine(*arguments, capture):
        assert capture
        assert arguments[:4] == ("run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}")
        assert ("--userns=keep-id" in arguments) == (engine == "podman")
        assert installer_module.GIT_IMAGE in arguments
        mount = arguments[arguments.index("-w") - 1]
        directory, target, access = mount.rsplit(":", 2)
        assert target == "/checkout"
        if "--entrypoint" in arguments:
            assert access == "rw"
            command = ["/bin/sh", *arguments[arguments.index(installer_module.GIT_IMAGE) + 1:]]
        else:
            assert access == "ro"
            command = ["git", "rev-parse", "HEAD"]
        calls.append(access)
        return subprocess.check_output(command, cwd=directory, text=True).strip()

    def unexpected_host_command(*args, **kwargs):
        pytest.fail("standalone source resolution bypassed the container engine")

    monkeypatch.setattr(installer, "run_engine", run_engine)
    monkeypatch.setattr(installer_module, "command", unexpected_host_command)

    reference = installer.resolve_source()
    tag = {"published": "v1.0.0", "tag": "v1.10.0", "head": None}[source]
    commit = released_commit if source == "published" else head_commit
    assert reference["kind"] == ("release" if tag else "head")
    assert reference["tag"] == tag
    assert reference["commit"] == commit
    assert installer.source == tmp_path / "persistent" / "mirage" / "sources" / f"{tag or 'head'}-{commit[:12]}"
    assert (installer.source / "stable-diffusion.cpp" / "ggml" / "src").is_dir()
    assert installer.resolve_source() == reference
    assert calls == ["rw", "rw", "ro"]
    assert list(installer.source.parent.iterdir()) == [installer.source]


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


def test_update_recovery_keeps_live_container_and_restores_settings_before_cutover(tmp_path, monkeypatch):
    installer, data, _ = recovery_fixture(tmp_path)
    installer.journal.update({
        "original_container": "mirage-original-test",
        "original_running": True,
        "actions": ["snapshot-config"],
        "pending_action": "initialize-config",
    })
    monkeypatch.setattr(installer, "container_exists", lambda name: name == "mirage")
    stop = Mock()
    engine = Mock()
    monkeypatch.setattr(installer, "stop_container", stop)
    monkeypatch.setattr(installer, "run_engine", engine)

    installer.recover()

    stop.assert_not_called()
    engine.assert_not_called()
    assert json.loads((data / "config" / "settings.json").read_text()) == {"original": True}


def test_update_recovery_restarts_original_after_interrupted_stop(tmp_path, monkeypatch):
    installer, _, _ = recovery_fixture(tmp_path)
    installer.journal.update({
        "original_container": "mirage-original-test",
        "original_running": True,
        "actions": ["snapshot-config", "initialize-config"],
        "pending_action": "stop-original",
    })
    monkeypatch.setattr(installer, "container_exists", lambda name: name == "mirage")
    monkeypatch.setattr(installer, "container_running", lambda name: False)
    engine = Mock()
    monkeypatch.setattr(installer, "run_engine", engine)
    monkeypatch.setattr(installer, "wait_health", Mock())

    installer.recover()

    engine.assert_called_once_with("start", "mirage")


def removal_fixture(tmp_path, monkeypatch, arguments):
    installer = make_installer(tmp_path, ["remove", *arguments])
    data = tmp_path / "data"
    data.mkdir()
    installer_module.connect_database(data).close()
    for group in ("models", "downloads", "outputs", "inputs", "thumbnails", "logs", "backups", "config", "runtimes"):
        path = data / group
        path.mkdir()
        (path / "fixture").touch()
    installer.state = {"engine": "podman", "data_dir": str(data), "app_image": {"id": "app-image"}}
    monkeypatch.setattr(installer, "run_engine", Mock())
    monkeypatch.setattr(installer, "stop_container", Mock())
    monkeypatch.setattr(installer, "container_running", lambda name: False)
    monkeypatch.setattr(installer, "image_exists", lambda image: False)
    return installer, data


@pytest.mark.parametrize("flags,keep_models,keep_user_data", [
    ([], True, True),
    (["--purge-models"], False, True),
    (["--purge-data"], True, False),
    (["--purge-models", "--purge-data"], False, False),
])
def test_unattended_removal_purges_only_selected_groups(tmp_path, monkeypatch, flags, keep_models, keep_user_data):
    installer, data = removal_fixture(tmp_path, monkeypatch, ["--yes", "--non-interactive", *flags])

    installer.run()

    assert installer.state["removed"] is True
    installer.stop_container.assert_called_once_with("mirage", remove=True)
    for name in ("models", "downloads"):
        assert (data / name / "fixture").exists() is keep_models
    for name in ("outputs", "inputs", "thumbnails", "logs", "backups"):
        assert (data / name / "fixture").exists() is keep_user_data
    assert (data / "mirage.sqlite3").exists() is keep_user_data
    assert (data / "config" / "fixture").is_file()
    assert (data / "runtimes" / "fixture").is_file()


def test_interactive_removal_confirms_model_and_user_data_separately(tmp_path, monkeypatch):
    installer, data = removal_fixture(tmp_path, monkeypatch, [])
    terminal = Mock()
    terminal.readline.side_effect = ["yes\n", "\n", "yes\n"]
    monkeypatch.setattr(installer_module, "installer_terminal", lambda _: nullcontext(terminal))

    installer.run()

    prompts = "".join(call.args[0] for call in terminal.write.call_args_list)
    assert f"model weights and partial downloads under {data}" in prompts
    assert f"database, job history, images, logs, and backups under {data}" in prompts
    assert (data / "models" / "fixture").is_file()
    assert not (data / "mirage.sqlite3").exists()


def test_removal_recovery_reopens_database_without_stopping_other_containers(tmp_path, monkeypatch):
    installer, data, _ = recovery_fixture(tmp_path)
    installer.journal.update({
        "operation": "remove",
        "original_container": "mirage-unused",
        "candidate_container": "mirage-candidate-unused",
    })
    stop = Mock()
    monkeypatch.setattr(installer, "stop_container", stop)

    installer.recover()

    stop.assert_not_called()
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
