#!/usr/bin/env python3
import argparse
import fcntl
import getpass
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen
import uuid

APP_NAME = "mirage"
DATABASE_NAME = "mirage.sqlite3"
DEFAULT_RELEASE_API = None


def atomic_json(path, document):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def command(arguments, capture=False):
    result = subprocess.run(
        [str(argument) for argument in arguments], text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "")[-4000:] if capture else ""
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(map(str, arguments))}\n{detail}")
    return (result.stdout or "").strip() if capture else ""


def connect_database(data):
    connection = sqlite3.connect(Path(data) / DATABASE_NAME, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS maintenance (
            id INTEGER PRIMARY KEY CHECK(id=1), active INTEGER NOT NULL DEFAULT 0,
            owner TEXT, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS admitted_writes (token TEXT PRIMARY KEY, started_at TEXT NOT NULL);
        INSERT OR IGNORE INTO maintenance(id, active, updated_at) VALUES(1, 0, '');
    """)
    return connection


def active_job_count(connection):
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone()
    if not exists:
        return 0
    return connection.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running')").fetchone()[0]


def busy_jobs(data):
    database = Path(data) / DATABASE_NAME
    if not database.is_file():
        return 0
    with sqlite3.connect(database, timeout=30) as connection:
        return active_job_count(connection)


def enter_maintenance(data, owner):
    connection = connect_database(data)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT active, owner FROM maintenance WHERE id=1").fetchone()
        if row["active"] and row["owner"] != owner:
            raise RuntimeError("another maintenance operation is active")
        busy = active_job_count(connection)
        if busy:
            raise RuntimeError("queued or running jobs prevent installation maintenance")
        connection.execute("UPDATE maintenance SET active=1, owner=?, updated_at=datetime('now') WHERE id=1", (owner,))
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def leave_maintenance(data, owner=None):
    connection = connect_database(data)
    try:
        if owner:
            row = connection.execute("SELECT owner FROM maintenance WHERE id=1").fetchone()
            if row and row["owner"] not in (None, owner):
                raise RuntimeError("another maintenance operation is active")
        connection.execute("UPDATE maintenance SET active=0, owner=NULL, updated_at=datetime('now') WHERE id=1")
    finally:
        connection.close()


def wait_for_writes(data, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        connection = connect_database(data)
        try:
            count = connection.execute("SELECT COUNT(*) FROM admitted_writes").fetchone()[0]
        finally:
            connection.close()
        if not count:
            return
        time.sleep(0.1)
    raise RuntimeError("timed out waiting for admitted writes to drain")


def backup_sqlite(source, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(source, timeout=30)
    target_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()


class Installer:
    def __init__(self, args):
        self.args = args
        invoked = Path(args.source_dir).resolve() if args.source_dir else Path(__file__).resolve().parent
        self.local_source = invoked if (invoked / "pyproject.toml").is_file() and not args.standalone else None
        state_default = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "mirage" / "installation.json"
        self.state_path = Path(args.state_file or os.environ.get("MIRAGE_STATE_FILE") or state_default).resolve()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state = read_json(self.state_path, {}) or {}
        self.journal_path = self.state_path.with_name("recovery-journal.json")
        self.journal = None
        self.source = None
        self.data = None
        self.engine = None
        self.prefix = None
        self.launcher = None
        self.operation_id = uuid.uuid4().hex[:12]
        self.candidate = f"mirage-candidate-{self.operation_id}"
        self.verifier = f"mirage-verifier-{self.operation_id}"
        self.original = f"mirage-original-{self.operation_id}"
        self.lock_stream = None

    def option(self, name, environment, persisted, default=None):
        explicit = getattr(self.args, name, None)
        if explicit is not None:
            return explicit
        if os.environ.get(environment) is not None:
            return os.environ[environment]
        if self.state.get(persisted) is not None:
            return self.state[persisted]
        return default

    def lock(self):
        self.lock_stream = (self.state_path.parent / "installation.lock").open("a+")
        try:
            fcntl.flock(self.lock_stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("another Mirage installer operation is active") from error

    def unlock(self):
        if self.lock_stream:
            self.lock_stream.close()
            self.lock_stream = None

    def save_journal(self):
        if self.journal is not None:
            atomic_json(self.journal_path, self.journal)

    def intent(self, action, **fields):
        self.journal["pending_action"] = action
        self.journal.update(fields)
        self.save_journal()

    def completed(self, action):
        self.journal.setdefault("actions", []).append(action)
        self.journal["pending_action"] = None
        self.save_journal()

    def run_engine(self, *arguments, capture=False):
        return command([self.engine, *arguments], capture=capture)

    def container_exists(self, name):
        return subprocess.run([self.engine, "container", "inspect", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0

    def container_running(self, name):
        if not self.container_exists(name):
            return False
        return self.run_engine("inspect", "--format", "{{.State.Running}}", name, capture=True) == "true"

    def stop_container(self, name, remove=False):
        if not self.container_exists(name):
            return
        if self.container_running(name):
            self.run_engine("stop", name)
        if remove:
            self.run_engine("rm", name)

    def engine_socket(self):
        if os.environ.get("MIRAGE_ENGINE_SOCKET"):
            return os.environ["MIRAGE_ENGINE_SOCKET"]
        if self.engine == "docker":
            path = "/var/run/docker.sock"
        else:
            path = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "podman" / "podman.sock"
            if not path.exists() and shutil.which("systemctl"):
                subprocess.run(["systemctl", "--user", "enable", "--now", "podman.socket"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            path = str(path)
        if not os.path.exists(path):
            raise RuntimeError(f"container API socket not found: {path}")
        return path

    def resolve_source(self):
        if self.local_source:
            if not (self.local_source / "stable-diffusion.cpp" / "ggml" / "src").is_dir():
                raise RuntimeError("source submodules are missing; run: git submodule update --init --recursive")
            self.source = self.local_source
            return {"kind": "local", "path": str(self.source)}
        release_api = self.args.release_api or os.environ.get("MIRAGE_RELEASE_API") or DEFAULT_RELEASE_API
        if not release_api:
            raise RuntimeError("standalone installation requires --release-api or MIRAGE_RELEASE_API until the canonical release host is configured")
        with urlopen(release_api, timeout=60) as response:
            release = json.load(response)
        tag = release["tag_name"]
        if not tag or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for character in tag):
            raise RuntimeError("release tag is invalid")
        repository = self.args.repository or os.environ.get("MIRAGE_RELEASE_REPOSITORY") or release.get("repository")
        if not repository:
            raise RuntimeError("the release response does not specify a source repository")
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "mirage" / "sources"
        base.mkdir(parents=True, exist_ok=True)
        temporary = base / f".{tag}.{os.getpid()}.tmp"
        if temporary.exists():
            shutil.rmtree(temporary)
        command(["git", "clone", "--depth", "1", "--branch", tag, "--recurse-submodules", "--shallow-submodules", repository, temporary])
        commit = command(["git", "-C", temporary, "rev-parse", "HEAD"], capture=True)
        destination = base / f"{tag}-{commit[:12]}"
        if destination.exists():
            shutil.rmtree(temporary)
        else:
            os.replace(temporary, destination)
        actual = command(["git", "-C", destination, "rev-parse", "HEAD"], capture=True)
        if actual != commit or not (destination / "stable-diffusion.cpp" / "ggml" / "src").is_dir():
            raise RuntimeError("downloaded release or its recursive submodules are incomplete")
        self.source = destination
        return {"kind": "release", "tag": tag, "commit": commit, "repository": repository, "path": str(destination)}

    def resolve_options(self):
        standalone_default = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "mirage" / "data"
        data_default = (self.local_source / "data") if self.local_source else standalone_default
        self.data = Path(self.option("data_dir", "MIRAGE_DATA_DIR", "data_dir", data_default)).resolve()
        self.prefix = Path(self.option("prefix", "MIRAGE_PREFIX", "prefix", Path.home() / ".local" / "bin")).resolve()
        self.launcher = self.prefix / "mirage"
        engine = self.option("engine", "MIRAGE_CONTAINER_ENGINE", "engine")
        if not engine:
            engine = "podman" if shutil.which("podman") else "docker" if shutil.which("docker") else None
        if engine not in ("podman", "docker") or not shutil.which(engine):
            raise RuntimeError("Podman or Docker is required")
        self.engine = engine
        self.data.mkdir(parents=True, exist_ok=True)
        for name in ("config", "models", "downloads", "inputs", "outputs", "thumbnails", "logs", "runtimes", "backups"):
            (self.data / name).mkdir(parents=True, exist_ok=True)
        if not self.args.allow_no_gpu and not list(Path("/dev/dri").glob("renderD*")):
            raise RuntimeError("no DRI render device found; use --allow-no-gpu for a CPU/custom backend")
        if shutil.disk_usage(self.data).free < 8_000_000_000:
            raise RuntimeError("at least 8 GB free space is required")

    def initialize_config(self):
        for name in ("generation-presets", "settings"):
            destination = self.data / "config" / f"{name}.json"
            if not destination.exists():
                shutil.copy2(self.source / "config" / f"{name}.default.json", destination)

    def build(self):
        if self.args.skip_build:
            app_image = self.state.get("app_image")
            runtime_build = self.state.get("active_runtime_build")
            if not app_image or not runtime_build:
                raise RuntimeError("--skip-build requires a previous immutable installation")
            runtime = read_json(self.data / "runtimes" / "releases" / runtime_build / "manifest.json")
            if not runtime:
                raise RuntimeError("the recorded runtime release is missing")
            return self.state["deployment_id"], app_image, runtime
        sys.path.insert(0, str(self.source / "src"))
        try:
            from mirage.app_build import build_app
            from mirage.runtime_build import build_runtime
            app = build_app(self.source, self.data, self.engine)
            runtime = build_runtime(self.source, self.data, self.engine, activate=False)
        finally:
            sys.path.pop(0)
        manifest_path = self.data / "releases" / "app" / app["app_build_id"] / "manifest.json"
        app.update({
            "sd_cpp_commit": runtime["sd_cpp_commit"],
            "runtime_build": runtime["build_id"],
            "runtime_image": runtime["runtime_image"],
            "source_reference": self.state.get("source_reference"),
        })
        atomic_json(manifest_path, app)
        return app["deployment_id"], app["app_image"], runtime

    def container_arguments(self, name, data, image, deployment, verify_only=False, maintenance=False, final=False):
        socket_path = self.engine_socket()
        arguments = [
            "run", "-d", "--name", name, "--network", "host", "--read-only", "--tmpfs", "/tmp",
            "--user", f"{os.getuid()}:{os.getgid()}",
        ]
        if self.engine == "podman":
            arguments += ["--userns=keep-id"]
        if final:
            arguments += ["--restart", "unless-stopped"]
        devices = [str(path) for pattern in ("card*", "renderD*") for path in sorted(Path("/dev/dri").glob(pattern))]
        arguments += [
            "-v", f"{data}:/data:rw", "-v", f"{socket_path}:/run/engine.sock:rw",
            "-e", "MIRAGE_DATA_DIR=/data", "-e", f"MIRAGE_HOST_DATA_DIR={data}",
            "-e", "MIRAGE_ENGINE_SOCKET=/run/engine.sock", "-e", f"MIRAGE_CONTAINER_ENGINE={self.engine}",
            "-e", f"MIRAGE_HOST_UID={os.getuid()}", "-e", f"MIRAGE_HOST_GID={os.getgid()}",
            "-e", f"MIRAGE_HOST_GROUPS={','.join(str(group) for group in os.getgroups())}",
            "-e", "HF_TOKEN", "-e", "MIRAGE_API_TOKEN", "-e", "MIRAGE_BROWSER_TOKEN",
            "-e", f"MIRAGE_DEPLOYMENT_ID={deployment}", "-e", f"MIRAGE_CONTAINER_IMAGE_ID={image}",
            "-e", f"MIRAGE_FAKE_SD={os.environ.get('MIRAGE_FAKE_SD', '0')}",
            "-e", f"MIRAGE_ALLOW_NO_GPU={os.environ.get('MIRAGE_ALLOW_NO_GPU', '0')}",
            "-e", f"MIRAGE_DRI_DEVICES={','.join(devices)}",
            "-e", f"MIRAGE_VERIFY_ONLY={'1' if verify_only else '0'}",
            "-e", f"MIRAGE_MAINTENANCE={'1' if maintenance else '0'}",
        ]
        if self.engine == "docker":
            arguments += ["--group-add", str(Path(socket_path).stat().st_gid)]
        return arguments + [image]

    def start_container(self, name, data, image, deployment, verify_only=False, maintenance=False, final=False):
        if self.container_exists(name):
            self.stop_container(name, remove=True)
        container = self.run_engine(*self.container_arguments(name, data, image, deployment, verify_only, maintenance, final), capture=True)
        return container

    def credentials(self):
        token = os.environ.get("MIRAGE_API_TOKEN", "")
        settings = read_json(self.data / "config" / "settings.json", {}) or {}
        host = settings.get("server", {}).get("host", "127.0.0.1")
        if host not in ("127.0.0.1", "localhost", "::1") and not token:
            if self.args.non_interactive:
                raise RuntimeError("MIRAGE_API_TOKEN is required for an authenticated installation")
            with open("/dev/tty", "r+") as terminal:
                token = getpass.getpass("Mirage administrator token: ", stream=terminal)
        return token

    def http(self, port, path, method="GET", token=""):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        request = Request(f"http://127.0.0.1:{port}{path}", method=method, headers=headers)
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def wait_health(self, port, deployment, image, token=""):
        deadline = time.monotonic() + 60
        detail = None
        while time.monotonic() < deadline:
            try:
                health = self.http(port, "/api/v1/health")
                if health.get("deployment_id") != deployment or health.get("container_image") != image:
                    raise RuntimeError("candidate deployment or container image identity mismatch")
                return health
            except Exception as error:
                detail = error
                time.sleep(0.5)
        raise RuntimeError(f"candidate did not become healthy: {detail}")

    def verification_data(self, runtime_build):
        root = self.state_path.parent / f"verification-{self.operation_id}"
        if root.exists():
            shutil.rmtree(root)
        (root / "config").mkdir(parents=True)
        shutil.copytree(self.data / "config", root / "config", dirs_exist_ok=True)
        backup_sqlite(self.data / DATABASE_NAME, root / DATABASE_NAME)
        leave_maintenance(root)
        with sqlite3.connect(root / "mirage.sqlite3") as connection:
            connection.execute("DELETE FROM admitted_writes")
        release = root / "runtimes" / "releases" / runtime_build
        release.parent.mkdir(parents=True)
        shutil.copytree(self.data / "runtimes" / "releases" / runtime_build, release, copy_function=os.link)
        (root / "runtimes" / "current").symlink_to(Path("releases") / runtime_build)
        for name in ("models", "inputs", "outputs", "thumbnails", "logs", "downloads"):
            (root / name).mkdir()
        settings = read_json(root / "config" / "settings.json")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        settings["server"] = {"host": "127.0.0.1", "port": port}
        atomic_json(root / "config" / "settings.json", settings)
        return root, port

    def verify_candidate(self, name, data, port, deployment, image, runtime, token):
        self.start_container(name, data, image["id"], deployment, verify_only=True)
        try:
            self.wait_health(port, deployment, image["id"], token)
            status = self.http(port, "/api/v1/runtime", token=token)
            if status.get("build_id") != runtime["build_id"] or status.get("runtime_image", {}).get("id") != runtime["runtime_image"]["id"]:
                raise RuntimeError("candidate runtime build or immutable image identity mismatch")
            verified = self.http(port, "/api/v1/runtime/verify", method="POST", token=token)
            if verified.get("build_id") != runtime["build_id"] or verified.get("runtime_image", {}).get("id") != runtime["runtime_image"]["id"]:
                raise RuntimeError("runtime verification returned a different identity")
        finally:
            self.stop_container(name, remove=True)

    def offline_migrate(self, image, deployment):
        code = "from mirage.service import Application; application = Application(start_queue=False); application.close()"
        arguments = ["run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}"]
        if self.engine == "podman":
            arguments += ["--userns=keep-id"]
        arguments += [
            "-v", f"{self.data}:/data:rw", "-e", "MIRAGE_DATA_DIR=/data",
            "-e", "MIRAGE_MAINTENANCE=1", "-e", f"MIRAGE_DEPLOYMENT_ID={deployment}",
            image["id"], "python", "-c", code,
        ]
        self.run_engine(*arguments)

    def activate_runtime(self, build_id):
        current = self.data / "runtimes" / "current"
        temporary = self.data / "runtimes" / "current.new"
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(Path("releases") / build_id)
        os.replace(temporary, current)

    def install_launcher(self):
        self.prefix.mkdir(parents=True, exist_ok=True)
        target = self.source / "bin" / "mirage"
        temporary = self.prefix / f".mirage.{os.getpid()}.tmp"
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(target)
        os.replace(temporary, self.launcher)

    def selected_models(self):
        selection = self.option("models", "MIRAGE_MODELS", "models", "zimage")
        if self.args.skip_models:
            selection = "none"
        if self.args.non_interactive:
            return selection
        try:
            terminal = open("/dev/tty", "r+")
        except OSError:
            return selection
        with terminal:
            dialog = shutil.which("dialog") or shutil.which("whiptail")
            if dialog:
                items = []
                for model, label in (("zimage", "Z-Image Turbo"), ("qwen", "Qwen-Image 2.1"), ("qwen-edit", "Qwen editing projector"), ("moss", "MOSS-Image-SFT")):
                    state = "ON" if model in selection.split(",") else "OFF"
                    items += [model, label, state]
                result = subprocess.run([dialog, "--stdout", "--title", "Mirage models", "--checklist", "Select models to install", "18", "76", "4", *items], text=True, stdout=subprocess.PIPE, stderr=terminal)
                if result.returncode:
                    raise RuntimeError("installation cancelled")
                return ",".join(result.stdout.replace('"', "").split()) or "none"
            terminal.write("Models: 1) Z-Image  2) Qwen  3) Qwen editing  4) MOSS; all; none\n")
            terminal.write("Numbers to install [1]: ")
            answer = terminal.readline()
        mapping = {"1": "zimage", "2": "qwen", "3": "qwen-edit", "4": "moss"}
        return ",".join(mapping.get(item.strip(), item.strip()) for item in answer.split(",")) if answer.strip() else "zimage"

    def install_models(self, selection, image):
        models = [item for item in selection.replace(" ", ",").split(",") if item]
        if "all" in models:
            models = ["zimage", "qwen", "qwen-edit", "moss"]
        if not models or models == ["none"]:
            return
        if any(model not in {"zimage", "qwen", "qwen-edit", "moss", "none"} for model in models):
            raise RuntimeError("unknown model selection")
        editing = "qwen-edit" in models
        selected = [model for model in ("zimage", "qwen", "moss") if model in models or (model == "qwen" and editing)]
        if self.args.import_models:
            source = Path(self.args.import_models).resolve()
            if not source.is_dir():
                raise RuntimeError("model import directory not found")
            arguments = [self.engine, "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}"]
            if self.engine == "podman":
                arguments += ["--userns=keep-id"]
            arguments += ["-v", f"{self.data}:/data:rw", "-v", f"{source}:/import:ro", image["id"], "python", "-m", "mirage.importer", "/import", *selected]
            if editing:
                arguments += ["--with-editing"]
            command(arguments)
        for model in selected:
            arguments = [self.launcher, "models", "install", model, "--wait"]
            if model == "qwen" and editing:
                arguments += ["--with-editing"]
            command(arguments)

    def new_journal(self, operation, original_running):
        self.journal = {
            "schema_version": 1,
            "operation": operation,
            "operation_id": self.operation_id,
            "data_dir": str(self.data),
            "original_state": self.state,
            "original_running": original_running,
            "original_runtime_link": os.readlink(self.data / "runtimes" / "current") if (self.data / "runtimes" / "current").is_symlink() else None,
            "candidate_container": self.candidate,
            "verifier_container": self.verifier,
            "original_container": self.original,
            "committed": False,
            "actions": [],
        }
        self.save_journal()

    def install_or_update(self, operation):
        source_reference = self.resolve_source()
        self.resolve_options()
        self.state["source_reference"] = source_reference
        selection = self.selected_models()
        original_running = self.container_running("mirage")
        if operation == "update" and not self.container_exists("mirage"):
            raise RuntimeError("no installed Mirage container was found")
        if busy_jobs(self.data):
            raise RuntimeError("queued or running jobs prevent installation")
        self.new_journal(operation, original_running)
        try:
            self.intent("initialize-config")
            self.initialize_config()
            self.completed("initialize-config")
            deployment, image, runtime = self.build()
            if not (self.data / "mirage.sqlite3").is_file():
                self.intent("initialize-database")
                self.offline_migrate(image, deployment)
                self.completed("initialize-database")
            token = self.credentials()
            self.intent("maintenance")
            enter_maintenance(self.data, self.operation_id)
            wait_for_writes(self.data)
            self.completed("maintenance")
            if original_running:
                self.intent("stop-original")
                self.stop_container("mirage")
                self.completed("stop-original")
            if self.container_exists("mirage"):
                self.intent("retain-original")
                self.run_engine("rename", "mirage", self.original)
                self.completed("retain-original")
            verification, port = self.verification_data(runtime["build_id"])
            self.intent("verify-candidate", verification_dir=str(verification))
            self.verify_candidate(self.verifier, verification, port, deployment, image, runtime, token)
            self.completed("verify-candidate")
            backup = self.data / "backups" / self.operation_id
            self.intent("backup", backup_dir=str(backup))
            backup_sqlite(self.data / "mirage.sqlite3", backup / "mirage.sqlite3")
            shutil.copytree(self.data / "config", backup / "config", dirs_exist_ok=True)
            self.completed("backup")
            self.intent("migrate")
            self.offline_migrate(image, deployment)
            self.completed("migrate")
            self.intent("activate-runtime")
            self.activate_runtime(runtime["build_id"])
            self.completed("activate-runtime")
            settings = read_json(self.data / "config" / "settings.json")
            port = settings["server"]["port"]
            self.intent("start-final")
            container_id = self.start_container(self.candidate, self.data, image["id"], deployment, maintenance=True, final=True)
            self.journal["candidate_container_id"] = container_id
            self.save_journal()
            self.wait_health(port, deployment, image["id"], token)
            status = self.http(port, "/api/v1/runtime", token=token)
            if status.get("build_id") != runtime["build_id"] or status.get("runtime_image", {}).get("id") != runtime["runtime_image"]["id"]:
                raise RuntimeError("final candidate runtime identity mismatch")
            self.completed("start-final")
            self.intent("commit")
            self.run_engine("rename", self.candidate, "mirage")
            previous = None
            if self.container_exists(self.original):
                previous = {
                    "container": self.original,
                    "container_id": self.run_engine("inspect", "--format", "{{.Id}}", self.original, capture=True),
                    "app_image": self.state.get("app_image"),
                    "deployment_id": self.state.get("deployment_id"),
                    "runtime_build": self.state.get("active_runtime_build"),
                }
            new_state = {
                **self.state,
                "schema_version": 1,
                "data_dir": str(self.data),
                "engine": self.engine,
                "prefix": str(self.prefix),
                "launcher": str(self.launcher),
                "launcher_target": str(self.source / "bin" / "mirage"),
                "source_reference": source_reference,
                "deployment_id": deployment,
                "server": settings["server"],
                "app_image": image,
                "container": {"name": "mirage", "id": self.run_engine("inspect", "--format", "{{.Id}}", "mirage", capture=True)},
                "active_runtime_build": runtime["build_id"],
                "active_runtime_image": runtime["runtime_image"],
                "previous_runtime_build": self.state.get("active_runtime_build"),
                "previous": previous,
                "models": selection,
                "removed": False,
            }
            atomic_json(self.state_path, new_state)
            self.state = new_state
            self.journal["committed"] = True
            self.save_journal()
            self.completed("commit")
            self.intent("open-admission")
            leave_maintenance(self.data, self.operation_id)
            self.completed("open-admission")
            self.intent("launcher")
            self.install_launcher()
            self.completed("launcher")
            if self.args.no_start:
                self.stop_container("mirage")
            if selection != "none" and not self.args.skip_models:
                if self.args.no_start:
                    command([self.launcher, "start"])
                self.install_models(selection, image)
                if self.args.no_start:
                    self.stop_container("mirage")
            self.journal_path.unlink(missing_ok=True)
            self.journal = None
            print(f"Mirage is ready: http://127.0.0.1:{port}\nCommand: {self.launcher}\nPersistent data: {self.data}")
        except BaseException:
            self.recover()
            raise
        finally:
            if self.journal and self.journal.get("verification_dir"):
                shutil.rmtree(self.journal["verification_dir"], ignore_errors=True)

    def recover(self):
        journal = self.journal or read_json(self.journal_path)
        if not journal:
            return
        self.journal = journal
        self.operation_id = journal["operation_id"]
        self.data = Path(journal["data_dir"])
        if journal.get("committed"):
            leave_maintenance(self.data, self.operation_id)
            self.journal_path.unlink(missing_ok=True)
            self.journal = None
            return
        for name in (journal.get("verifier_container"), journal.get("candidate_container")):
            if name:
                self.stop_container(name, remove=True)
        backup = journal.get("backup_dir")
        migration_started = "migrate" in journal.get("actions", []) or journal.get("pending_action") == "migrate"
        if migration_started and "backup" in journal.get("actions", []) and backup and Path(backup).is_dir():
            restored = self.data / "mirage.sqlite3.recovery"
            shutil.copy2(Path(backup) / "mirage.sqlite3", restored)
            os.replace(restored, self.data / "mirage.sqlite3")
            for suffix in ("-wal", "-shm"):
                (self.data / f"mirage.sqlite3{suffix}").unlink(missing_ok=True)
            shutil.rmtree(self.data / "config")
            shutil.copytree(Path(backup) / "config", self.data / "config")
        if "activate-runtime" in journal.get("actions", []) or journal.get("pending_action") == "activate-runtime":
            current = self.data / "runtimes" / "current"
            current.unlink(missing_ok=True)
            if journal.get("original_runtime_link"):
                current.symlink_to(journal["original_runtime_link"])
        if journal.get("original_container") and self.container_exists("mirage"):
            self.stop_container("mirage", remove=True)
        original = journal.get("original_container")
        if original and self.container_exists(original):
            self.run_engine("rename", original, "mirage")
            if journal.get("original_running"):
                self.run_engine("start", "mirage")
                original_state = journal.get("original_state", {})
                settings = read_json(self.data / "config" / "settings.json", {})
                self.wait_health(
                    settings.get("server", {}).get("port", 8765),
                    original_state.get("deployment_id"),
                    original_state.get("app_image", {}).get("id"),
                    os.environ.get("MIRAGE_API_TOKEN", ""),
                )
        leave_maintenance(self.data, self.operation_id)
        if journal.get("original_state"):
            atomic_json(self.state_path, journal["original_state"])
        else:
            self.state_path.unlink(missing_ok=True)
        self.journal_path.unlink(missing_ok=True)
        self.journal = None

    def remove(self):
        if not self.args.yes:
            if self.args.non_interactive:
                raise RuntimeError("remove requires --yes in non-interactive mode")
            with open("/dev/tty", "r+") as terminal:
                terminal.write("Remove the Mirage application stack and launcher? User data is preserved. [y/N] ")
                if terminal.readline().strip().lower() not in ("y", "yes"):
                    raise RuntimeError("removal cancelled")
        self.data = Path(self.state.get("data_dir", "")).resolve()
        self.engine = self.state.get("engine") or ("podman" if shutil.which("podman") else "docker")
        self.launcher = Path(self.state.get("launcher", Path.home() / ".local" / "bin" / "mirage"))
        if busy_jobs(self.data):
            raise RuntimeError("queued or running jobs prevent removal")
        self.new_journal("remove", self.container_running("mirage"))
        try:
            self.intent("remove-maintenance")
            enter_maintenance(self.data, self.operation_id)
            wait_for_writes(self.data)
            self.completed("remove-maintenance")
            for name in ("mirage", (self.state.get("previous") or {}).get("container")):
                if name:
                    self.stop_container(name, remove=True)
            images = {self.state.get("app_image", {}).get("id"), ((self.state.get("previous") or {}).get("app_image") or {}).get("id")}
            for image in images:
                if image:
                    self.run_engine("rmi", image)
            if self.launcher.is_symlink() and Path(os.readlink(self.launcher)) == Path(self.state.get("launcher_target", "")):
                self.launcher.unlink()
            self.journal["committed"] = True
            self.save_journal()
            leave_maintenance(self.data, self.operation_id)
            self.state.update({"removed": True, "container": None, "previous": None})
            atomic_json(self.state_path, self.state)
            self.journal_path.unlink(missing_ok=True)
            self.journal = None
            print(f"Mirage application removed; user data preserved at {self.data}")
        except BaseException:
            self.recover()
            raise

    def run(self):
        self.lock()
        try:
            if self.journal_path.exists():
                self.engine = self.state.get("engine") or ("podman" if shutil.which("podman") else "docker")
                self.recover()
            operation = self.args.operation
            if operation == "remove":
                if not self.state:
                    raise RuntimeError("no Mirage installation state was found")
                self.remove()
            else:
                self.install_or_update(operation)
        finally:
            self.unlock()


def parser():
    result = argparse.ArgumentParser(description="Install, update, or remove Mirage using Python 3 and a container engine")
    result.add_argument("operation", nargs="?", default="install", choices=("install", "update", "remove"))
    result.add_argument("--data-dir")
    result.add_argument("--prefix")
    result.add_argument("--models")
    result.add_argument("--import-models")
    result.add_argument("--runtime", dest="engine", choices=("podman", "docker"))
    result.add_argument("--skip-build", action="store_true")
    result.add_argument("--skip-models", action="store_true")
    result.add_argument("--no-start", action="store_true")
    result.add_argument("--non-interactive", action="store_true")
    result.add_argument("--allow-no-gpu", action="store_true")
    result.add_argument("--yes", action="store_true")
    result.add_argument("--standalone", action="store_true")
    result.add_argument("--source-dir")
    result.add_argument("--release-api")
    result.add_argument("--repository")
    result.add_argument("--state-file")
    return result


def main():
    args = parser().parse_args()
    try:
        Installer(args).run()
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"mirage: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("mirage: interrupted; rerun the installer to inspect and finish recovery", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
