#!/usr/bin/env python3
import argparse
from contextlib import contextmanager
import fcntl
import getpass
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid

APP_NAME = "mirage"
DATABASE_NAME = "mirage.sqlite3"
MODEL_DATA = ("models", "downloads")
USER_DATA = (
    "inputs", "outputs", "thumbnails", "logs", "backups",
    DATABASE_NAME, f"{DATABASE_NAME}-wal", f"{DATABASE_NAME}-shm",
    f"{DATABASE_NAME}-journal", f"{DATABASE_NAME}.recovery", ".active-job",
)
DEFAULT_RELEASE_API = "https://api.github.com/repos/dshein-alt/mirage/releases/latest"
DEFAULT_RELEASE_REPOSITORY = "https://github.com/dshein-alt/mirage.git"
GIT_IMAGE = "docker.io/alpine/git:v2.54.1"
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,64}\Z")
ACCESS_MODES = {
    "local": "This machine only (127.0.0.1)",
    "authenticated": "Network access with sign-in tokens",
}
MODEL_CHOICES = {
    "zimage": "Z-Image Turbo",
    "qwen": "Qwen-Image 2.1",
    "qwen-edit": "Qwen-Image 2.1 with editing",
    "moss": "MOSS-Image-SFT",
}


@contextmanager
def installer_terminal(non_interactive):
    if non_interactive:
        yield None
        return
    try:
        device = open("/dev/tty", "r+b", buffering=0)
    except OSError:
        yield None
        return
    with io.TextIOWrapper(device, write_through=True) as terminal:
        yield terminal


def read_terminal(terminal, prompt):
    print(prompt, end="", file=terminal, flush=True)
    answer = terminal.readline()
    if not answer:
        raise RuntimeError("installation cancelled")
    return answer.strip()


def dialog_prompt(terminal, dialog, *arguments):
    result = subprocess.run(
        [dialog, "--stdout", "--title", "Mirage setup", *arguments],
        stdin=terminal, stderr=terminal, stdout=subprocess.PIPE, text=True,
    )
    if result.returncode:
        raise RuntimeError("installation cancelled")
    return result.stdout.strip()


def model_selection(selection):
    models = re.split(r"[,\s]+", selection.strip()) if selection.strip() else []
    if "all" in models:
        return list(MODEL_CHOICES)
    if any(model not in MODEL_CHOICES and model != "none" for model in models):
        raise RuntimeError("unknown model selection")
    return models


def port_number(value):
    try:
        port = int(value)
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535") from error
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535")
    return port


def validate_tokens(admin, browser):
    for name, value in (("MIRAGE_API_TOKEN", admin), ("MIRAGE_BROWSER_TOKEN", browser)):
        if not TOKEN_PATTERN.fullmatch(value):
            raise RuntimeError(f"{name} must be a URL-safe 16-64 character string in authenticated mode")
    if admin == browser:
        raise RuntimeError("MIRAGE_API_TOKEN and MIRAGE_BROWSER_TOKEN must be different")


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
        self.local_source = None
        if not args.standalone:
            invoked = Path(args.source_dir).resolve() if args.source_dir else Path(__file__).resolve().parent
            if (invoked / "pyproject.toml").is_file():
                self.local_source = invoked
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

    def image_exists(self, image):
        return subprocess.run([self.engine, "image", "inspect", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0

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

    def git_container_arguments(self):
        arguments = ["run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}"]
        if self.engine == "podman":
            arguments += ["--userns=keep-id"]
        return arguments + ["-e", "HOME=/tmp", "-e", "GIT_TERMINAL_PROMPT=0"]

    def resolve_source(self):
        if self.local_source:
            if not (self.local_source / "stable-diffusion.cpp" / "ggml" / "src").is_dir():
                raise RuntimeError("source submodules are missing; run: git submodule update --init --recursive")
            self.source = self.local_source
            return {"kind": "local", "path": str(self.source)}
        repository = self.args.repository or os.environ.get("MIRAGE_RELEASE_REPOSITORY")
        release_api = self.args.release_api or os.environ.get("MIRAGE_RELEASE_API")
        if not release_api and repository in (None, DEFAULT_RELEASE_REPOSITORY):
            release_api = DEFAULT_RELEASE_API
        tag = ""
        if release_api:
            try:
                with urlopen(release_api, timeout=60) as response:
                    release = json.load(response)
            except HTTPError as error:
                if error.code != 404:
                    raise
            else:
                tag = release.get("tag_name")
                if not isinstance(tag, str) or not tag:
                    raise RuntimeError("release tag is invalid")
                repository = repository or release.get("repository")
        repository = repository or DEFAULT_RELEASE_REPOSITORY
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")).resolve() / "mirage" / "sources"
        base.mkdir(parents=True, exist_ok=True)
        arguments = self.git_container_arguments()
        if Path(repository).is_absolute():
            arguments += ["-v", f"{repository}:{repository}:ro"]
        print("Fetching Mirage and its source dependencies in a container", flush=True)
        with tempfile.TemporaryDirectory(prefix=".checkout-", dir=base) as temporary:
            source_info = self.run_engine(
                *arguments, "-v", f"{temporary}:/checkout:rw", "-w", "/checkout",
                "--entrypoint", "/bin/sh", GIT_IMAGE, "-ec", """
                    git init -q repository
                    cd repository
                    git remote add origin "$2"
                    tag=$1
                    if [ -z "$tag" ]; then
                        tags=$(git ls-remote --tags --refs --sort=-version:refname origin)
                        tag=$(printf '%s\n' "$tags" | sed -n '1s|^[^[:blank:]]*[[:blank:]]refs/tags/||p')
                    fi
                    ref=HEAD
                    if [ -n "$tag" ]; then
                        ref="refs/tags/$tag"
                        git check-ref-format "$ref"
                    fi
                    git fetch -q --depth 1 --no-tags origin "$ref"
                    git checkout -q --detach FETCH_HEAD
                    git submodule update --init --recursive --depth 1 >&2
                    git rev-parse HEAD
                    printf '%s\n' "$tag"
                """, "sh", tag, repository, capture=True,
            )
            commit, _, tag = source_info.partition("\n")
            checkout = Path(temporary) / "repository"
            if not re.fullmatch(r"[a-f0-9]{40}", commit) or not (checkout / "stable-diffusion.cpp" / "ggml" / "src").is_dir():
                raise RuntimeError("downloaded release or its recursive submodules are incomplete")
            label = re.sub(r"[^A-Za-z0-9._-]", "-", tag) if tag else "head"
            destination = base / f"{label}-{commit[:12]}"
            if destination.exists():
                actual = self.run_engine(
                    *self.git_container_arguments(), "--network=none", "-v", f"{destination}:/checkout:ro",
                    "-w", "/checkout", GIT_IMAGE, "rev-parse", "HEAD", capture=True,
                )
                if actual != commit or not (destination / "stable-diffusion.cpp" / "ggml" / "src").is_dir():
                    raise RuntimeError(f"existing release checkout is incomplete: {destination}")
            else:
                os.replace(checkout, destination)
        self.source = destination
        return {
            "kind": "release" if tag else "head",
            "tag": tag or None,
            "commit": commit,
            "repository": repository,
            "path": str(destination),
        }

    def resolve_server_options(self, terminal=None, dialog=None):
        server = self.state.get("server", {})
        port_override = self.args.port if self.args.port is not None else os.environ.get("MIRAGE_PORT")
        port = port_override if port_override is not None else server.get("port", 8765)
        try:
            self.port = port_number(port)
        except argparse.ArgumentTypeError as error:
            raise RuntimeError(str(error)) from error
        mode_override = self.args.mode or os.environ.get("MIRAGE_MODE")
        self.mode = mode_override or server.get("mode", "local")
        if self.mode not in ACCESS_MODES:
            raise RuntimeError("access mode must be local or authenticated")
        if terminal is None:
            return
        if not mode_override:
            if dialog:
                items = [part for item in ACCESS_MODES.items() for part in item]
                self.mode = dialog_prompt(
                    terminal, dialog, "--default-item", self.mode,
                    "--menu", "Choose Mirage access mode", "12", "72", "2", *items,
                )
            else:
                print("Access mode", file=terminal)
                for index, label in enumerate(ACCESS_MODES.values(), 1):
                    print(f"  {index}. {label}", file=terminal)
                while True:
                    mode = read_terminal(terminal, f"Access mode [{self.mode}]: ").lower()
                    mode = {"1": "local", "2": "authenticated"}.get(mode, mode) or self.mode
                    if mode in ACCESS_MODES:
                        self.mode = mode
                        break
                    print("Choose local or authenticated.", file=terminal)
        if port_override is None:
            prompt = "Web interface port (1-65535)"
            while True:
                value = (
                    dialog_prompt(terminal, dialog, "--inputbox", prompt, "9", "60", str(self.port))
                    if dialog else read_terminal(terminal, f"{prompt} [{self.port}]: ")
                )
                try:
                    self.port = port_number(value or self.port)
                    break
                except argparse.ArgumentTypeError as error:
                    if dialog:
                        dialog_prompt(terminal, dialog, "--msgbox", str(error), "8", "60")
                    else:
                        print(error, file=terminal)

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
        settings_path = self.data / "config" / "settings.json"
        settings = read_json(settings_path)
        settings["server"] = {"mode": self.mode, "port": self.port}
        atomic_json(settings_path, settings)

    def build(self, source_reference):
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
            build_options = {}
            if not self.local_source:
                from mirage.provenance import container_git

                build_options["git_command"] = container_git(self.source, self.engine, image=GIT_IMAGE)
            app = build_app(self.source, self.data, self.engine, **build_options)
            runtime = build_runtime(self.source, self.data, self.engine, **build_options)
        finally:
            sys.path.pop(0)
        manifest_path = self.data / "releases" / "app" / app["app_build_id"] / "manifest.json"
        app.update({
            "sd_cpp_commit": runtime["sd_cpp_commit"],
            "runtime_build": runtime["build_id"],
            "runtime_image": runtime["runtime_image"],
            "source_reference": source_reference,
        })
        atomic_json(manifest_path, app)
        return app["deployment_id"], app["app_image"], runtime

    def container_arguments(self, name, data, image, deployment, verify_only=False, maintenance=False, final=False):
        socket_path = self.engine_socket()
        server = read_json(Path(data) / "config" / "settings.json")["server"]
        port = server["port"]
        publish_host = "127.0.0.1" if server["mode"] == "local" or verify_only else "0.0.0.0"
        arguments = [
            "run", "-d", "--name", name, "--publish", f"{publish_host}:{port}:{port}",
            "--read-only", "--tmpfs", "/tmp", "--user", f"{os.getuid()}:{os.getgid()}",
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
            "-e", "MIRAGE_BIND_HOST=0.0.0.0", "-e", f"MIRAGE_MODE={server['mode']}", "-e", f"MIRAGE_PORT={port}",
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

    def credentials(self, terminal=None):
        if self.mode == "local":
            return ""
        admin = os.environ.get("MIRAGE_API_TOKEN", "")
        browser = os.environ.get("MIRAGE_BROWSER_TOKEN", "")
        if terminal is not None and (not admin or not browser):
            print("Use two different tokens of 16–64 ASCII letters, digits, underscores, or hyphens.", file=terminal)
            if not admin:
                admin = getpass.getpass("Mirage administrator token: ", stream=terminal)
            if not browser:
                browser = getpass.getpass("Mirage browser user token: ", stream=terminal)
        validate_tokens(admin, browser)
        os.environ["MIRAGE_API_TOKEN"] = admin
        os.environ["MIRAGE_BROWSER_TOKEN"] = browser
        return admin

    def http(self, port, path, method="GET", token=""):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        request = Request(f"http://127.0.0.1:{port}{path}", method=method, headers=headers)
        with urlopen(request, timeout=5) as response:
            return json.load(response)

    def wait_health(self, port, deployment, image, token="", allow_unauthorized=False):
        deadline = time.monotonic() + 60
        detail = None
        while time.monotonic() < deadline:
            try:
                health = self.http(port, "/api/v1/health", token=token)
                if health.get("deployment_id") != deployment or health.get("container_image") != image:
                    raise RuntimeError("candidate deployment or container image identity mismatch")
                return health
            except HTTPError as error:
                if allow_unauthorized and error.code == 401:
                    return {"authenticated": False}
                detail = error
                time.sleep(0.5)
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
        settings["server"] = {"mode": self.mode, "port": port}
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

    def selected_models(self, terminal=None, dialog=None):
        selection = self.option("models", "MIRAGE_MODELS", "models", "zimage")
        if self.args.skip_models:
            return "none"
        defaults = model_selection(selection)
        if terminal is None or self.args.models is not None or "MIRAGE_MODELS" in os.environ:
            return selection
        if dialog:
            items = []
            for model, label in MODEL_CHOICES.items():
                items += [model, label, "ON" if model in defaults else "OFF"]
            result = dialog_prompt(
                terminal, dialog, "--checklist", "Select models to install (Space toggles a model)",
                "18", "76", "4", *items,
            )
            return ",".join(result.replace('"', "").split()) or "none"
        print("Models", file=terminal)
        mapping = {}
        for index, (model, label) in enumerate(MODEL_CHOICES.items(), 1):
            print(f"  {index}. {label}", file=terminal)
            mapping[str(index)] = model
        while True:
            answer = read_terminal(terminal, f"Models to install (comma separated, all, or none) [{selection}]: ")
            if not answer:
                return selection
            choices = ",".join(mapping.get(item, item) for item in re.split(r"[,\s]+", answer.lower()))
            try:
                model_selection(choices)
            except RuntimeError as error:
                print(error, file=terminal)
            else:
                return choices

    def install_models(self, selection, image):
        models = model_selection(selection)
        if not models or models == ["none"]:
            return
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
            arguments = [
                "exec", "-e", f"MIRAGE_API_URL=http://127.0.0.1:{self.port}",
                "mirage", "python", "-m", "mirage.cli", "models", "install", model, "--wait",
            ]
            if model == "qwen" and editing:
                arguments += ["--with-editing"]
            self.run_engine(*arguments)

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
        self.resolve_options()
        with installer_terminal(self.args.non_interactive) as terminal:
            dialog = None
            if terminal is not None and os.environ.get("TERM") not in (None, "", "dumb"):
                dialog = shutil.which("dialog") or shutil.which("whiptail")
            self.resolve_server_options(terminal, dialog)
            selection = self.selected_models(terminal, dialog)
            token = self.credentials(terminal)
        source_reference = self.resolve_source()
        original_running = self.container_running("mirage")
        if operation == "update" and not self.container_exists("mirage"):
            raise RuntimeError("no installed Mirage container was found")
        if busy_jobs(self.data):
            raise RuntimeError("queued or running jobs prevent installation")
        self.new_journal(operation, original_running)
        try:
            backup = self.data / "backups" / self.operation_id
            self.intent("snapshot-config", backup_dir=str(backup))
            shutil.copytree(self.data / "config", backup / "config")
            self.completed("snapshot-config")
            self.intent("initialize-config")
            self.initialize_config()
            self.completed("initialize-config")
            deployment, image, runtime = self.build(source_reference)
            if not (self.data / "mirage.sqlite3").is_file():
                self.intent("initialize-database")
                self.offline_migrate(image, deployment)
                self.completed("initialize-database")
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
            self.intent("backup", backup_dir=str(backup))
            backup_sqlite(self.data / "mirage.sqlite3", backup / "mirage.sqlite3")
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
            if selection != "none" and not self.args.skip_models:
                self.install_models(selection, image)
            if self.args.no_start:
                self.stop_container("mirage")
            self.journal_path.unlink(missing_ok=True)
            self.journal = None
            exposure = "loopback only" if self.mode == "local" else "all interfaces"
            print(f"Mirage is ready: http://127.0.0.1:{port}\nAccess mode: {self.mode} ({exposure})\nCommand: {self.launcher}\nPersistent data: {self.data}")
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
        if journal.get("committed") or journal.get("operation") == "remove":
            if (self.data / DATABASE_NAME).is_file():
                leave_maintenance(self.data, self.operation_id)
            self.journal_path.unlink(missing_ok=True)
            self.journal = None
            return
        for name in (journal.get("verifier_container"), journal.get("candidate_container")):
            if name:
                self.stop_container(name, remove=True)
        actions = {*journal.get("actions", []), journal.get("pending_action")}
        backup = Path(journal["backup_dir"]) if journal.get("backup_dir") else None
        restore_database = "migrate" in actions and "backup" in journal.get("actions", [])
        if backup and restore_database and (backup / DATABASE_NAME).is_file():
            restored = self.data / "mirage.sqlite3.recovery"
            shutil.copy2(backup / DATABASE_NAME, restored)
            os.replace(restored, self.data / "mirage.sqlite3")
            for suffix in ("-wal", "-shm"):
                (self.data / f"mirage.sqlite3{suffix}").unlink(missing_ok=True)
        restore_config = restore_database or {"snapshot-config", "initialize-config"} <= actions
        if backup and restore_config and (backup / "config").is_dir():
            config = self.data / "config"
            if config.exists():
                shutil.rmtree(config)
            shutil.copytree(backup / "config", config)
        if "activate-runtime" in actions:
            current = self.data / "runtimes" / "current"
            current.unlink(missing_ok=True)
            if journal.get("original_runtime_link"):
                current.symlink_to(journal["original_runtime_link"])
        candidate = journal.get("candidate_container_id")
        if candidate and self.container_exists("mirage"):
            current_id = self.run_engine("inspect", "--format", "{{.Id}}", "mirage", capture=True)
            if current_id == candidate:
                self.stop_container("mirage", remove=True)
        original = journal.get("original_container")
        if original and self.container_exists(original):
            self.run_engine("rename", original, "mirage")
        if journal.get("original_running") and "stop-original" in actions and not self.container_running("mirage"):
            self.run_engine("start", "mirage")
            original_state = journal.get("original_state", {})
            settings = read_json(self.data / "config" / "settings.json", {})
            self.wait_health(
                settings.get("server", {}).get("port", 8765),
                original_state.get("deployment_id"),
                original_state.get("app_image", {}).get("id"),
                os.environ.get("MIRAGE_API_TOKEN", ""),
                allow_unauthorized=True,
            )
        if (self.data / DATABASE_NAME).is_file():
            leave_maintenance(self.data, self.operation_id)
        if journal.get("original_state"):
            atomic_json(self.state_path, journal["original_state"])
        else:
            self.state_path.unlink(missing_ok=True)
        self.journal_path.unlink(missing_ok=True)
        self.journal = None

    def confirm_removal(self):
        requested = (self.args.purge_models, self.args.purge_data)
        if self.args.yes:
            return requested
        with installer_terminal(self.args.non_interactive) as terminal:
            if terminal is None:
                raise RuntimeError("remove requires --yes when no terminal is available")
            answer = read_terminal(terminal, "Remove the Mirage application containers and launcher? [y/N] ")
            if answer.lower() not in ("y", "yes"):
                raise RuntimeError("removal cancelled")
            questions = (
                f"Permanently delete downloaded model weights and partial downloads under {self.data}? [y/N] ",
                f"Permanently delete the Mirage database, job history, images, logs, and backups under {self.data}? [y/N] ",
            )
            return tuple(
                (selected or not any(requested)) and read_terminal(terminal, question).lower() in ("y", "yes")
                for selected, question in zip(requested, questions)
            )

    def remove(self):
        data = Path(self.state.get("data_dir", ""))
        if not data.is_absolute() or data.resolve() != data:
            raise RuntimeError("the recorded Mirage data directory is missing or has moved")
        self.data = data
        self.engine = self.state.get("engine") or ("podman" if shutil.which("podman") else "docker")
        self.launcher = Path(self.state.get("launcher", Path.home() / ".local" / "bin" / "mirage"))
        purge_models, purge_data = self.confirm_removal()
        if (purge_models or purge_data) and self.data in (Path(self.data.anchor), Path.home().resolve()):
            raise RuntimeError("purging requires a Mirage data directory outside the home or filesystem root itself")
        if busy_jobs(self.data):
            raise RuntimeError("queued or running jobs prevent removal")
        if not self.state.get("removed"):
            self.remove_application()
        for purge, paths, description in (
            (purge_models, MODEL_DATA, "Downloaded models"),
            (purge_data, USER_DATA, "Database, job history, images, logs, and backups"),
        ):
            if purge:
                for name in paths:
                    path = self.data / name
                    if not path.is_symlink() and path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink(missing_ok=True)
            print(f"{description} {'deleted' if purge else 'kept'} under {self.data}")

    def remove_application(self):
        self.run_engine("info", capture=True)
        self.new_journal("remove", self.container_running("mirage"))
        try:
            if (self.data / DATABASE_NAME).is_file():
                self.intent("remove-maintenance")
                enter_maintenance(self.data, self.operation_id)
                wait_for_writes(self.data)
                self.completed("remove-maintenance")
            for name in ("mirage", (self.state.get("previous") or {}).get("container")):
                if name:
                    self.stop_container(name, remove=True)
            images = {self.state.get("app_image", {}).get("id"), ((self.state.get("previous") or {}).get("app_image") or {}).get("id")}
            for image in images:
                if image and self.image_exists(image):
                    self.run_engine("rmi", image)
            if self.launcher.is_symlink() and Path(os.readlink(self.launcher)) == Path(self.state.get("launcher_target", "")):
                self.launcher.unlink()
            self.journal["committed"] = True
            self.save_journal()
            if (self.data / DATABASE_NAME).is_file():
                leave_maintenance(self.data, self.operation_id)
            self.state.update({"removed": True, "container": None, "previous": None})
            atomic_json(self.state_path, self.state)
            self.journal_path.unlink(missing_ok=True)
            self.journal = None
            print("Mirage application removed")
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
            if operation != "remove" and (self.args.purge_models or self.args.purge_data):
                raise RuntimeError("--purge-models and --purge-data require the remove operation")
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
    result.add_argument("--port", type=port_number)
    result.add_argument("--mode", choices=("local", "authenticated"))
    result.add_argument("--models")
    result.add_argument("--import-models")
    result.add_argument("--runtime", dest="engine", choices=("podman", "docker"))
    result.add_argument("--skip-build", action="store_true")
    result.add_argument("--skip-models", action="store_true")
    result.add_argument("--no-start", action="store_true")
    result.add_argument("--non-interactive", action="store_true")
    result.add_argument("--allow-no-gpu", action="store_true")
    result.add_argument("--yes", action="store_true")
    result.add_argument("--purge-models", action="store_true", help="remove downloaded model weights and partial downloads")
    result.add_argument("--purge-data", action="store_true", help="remove the database, job history, images, logs, and backups")
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
