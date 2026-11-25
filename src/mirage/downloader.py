import os
from pathlib import Path
import shutil
import socket
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .errors import WorkbenchError
from .manifest import ModelRegistry
from .util import file_sha256


class DownloadManager:
    def __init__(self, registry: ModelRegistry, downloads_dir: Path, token=None):
        self.registry = registry
        self.downloads_dir = downloads_dir
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.token = token or os.environ.get("HF_TOKEN")

    def install(self, model_id, editing=False, progress=None, cancel=None):
        model = self.registry.get(model_id)
        components = [
            component for component in model["components"].values()
            if component["required"] or (editing and "edit" in component.get("required_for", []))
        ]
        missing_bytes = 0
        for component in components:
            destination = self.registry.path(component)
            partial = self.partial_path(model_id, component)
            if destination.is_file() and destination.stat().st_size == component["bytes"]:
                continue
            missing_bytes += component["bytes"] - (partial.stat().st_size if partial.is_file() else 0)
        free = shutil.disk_usage(self.downloads_dir).free
        if missing_bytes > free:
            raise WorkbenchError("disk_full", f"Need {missing_bytes} bytes for {model_id}, only {free} available", 507)
        total = sum(component["bytes"] for component in components)
        completed = 0
        for index, component in enumerate(components):
            if cancel and cancel.is_set():
                raise WorkbenchError("download_cancelled", "Model installation cancelled", 409)
            destination = self.registry.path(component)
            if destination.is_file():
                if destination.stat().st_size == component["bytes"]:
                    completed += component["bytes"]
                    if progress:
                        progress(completed, total, component["local_path"], "verified")
                    continue
                partial = self.partial_path(model_id, component)
                partial.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, partial)
            self.download(model_id, component, completed, total, progress, cancel)
            completed += component["bytes"]
            if progress:
                progress(completed, total, component["local_path"], "verified")
        self.registry.verify(model_id, editing=editing, checksum=True)
        return self.registry.status(model_id)

    def partial_path(self, model_id, component):
        return self.downloads_dir / model_id / f"{Path(component['local_path']).name}.partial"

    def download(self, model_id, component, completed, total, progress, cancel):
        partial = self.partial_path(model_id, component)
        partial.parent.mkdir(parents=True, exist_ok=True)
        destination = self.registry.path(component)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if partial.exists() and partial.stat().st_size > component["bytes"]:
            partial.unlink()
        for attempt in range(5):
            if cancel and cancel.is_set():
                raise WorkbenchError("download_cancelled", "Model installation cancelled", 409)
            offset = partial.stat().st_size if partial.exists() else 0
            headers = {"User-Agent": "mirage/1.0", "Accept-Encoding": "identity"}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            if offset:
                headers["Range"] = f"bytes={offset}-"
            request = Request(self.registry.url(component), headers=headers)
            try:
                with urlopen(request, timeout=60) as response:
                    if offset and response.status == 200:
                        offset = 0
                    if offset:
                        content_range = response.headers.get("Content-Range", "")
                        if response.status != 206 or not content_range.startswith(f"bytes {offset}-"):
                            raise WorkbenchError("resume_rejected", "Server returned an invalid range response; restart this model install")
                    mode = "ab" if offset else "wb"
                    received = offset
                    with partial.open(mode) as stream:
                        while True:
                            if cancel and cancel.is_set():
                                raise WorkbenchError("download_cancelled", "Model installation cancelled", 409)
                            block = response.read(1024 * 1024)
                            if not block:
                                break
                            stream.write(block)
                            received += len(block)
                            if received > component["bytes"]:
                                raise WorkbenchError("size_mismatch", f"Download exceeded expected size for {component['local_path']}")
                            if progress:
                                progress(completed + received, total, component["local_path"], "downloading")
                        stream.flush()
                        os.fsync(stream.fileno())
                if received != component["bytes"]:
                    raise WorkbenchError("download_incomplete", f"Incomplete download for {component['local_path']}: {received}/{component['bytes']}")
                if progress:
                    progress(completed + received, total, component["local_path"], "hashing")
                if file_sha256(partial) != component["sha256"]:
                    partial.unlink(missing_ok=True)
                    raise WorkbenchError("checksum_mismatch", f"Checksum mismatch for {component['local_path']}; corrupt partial was removed")
                os.replace(partial, destination)
                return
            except WorkbenchError as error:
                if error.code in {"download_incomplete", "size_mismatch"} and attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                raise
            except (HTTPError, URLError, TimeoutError, socket.timeout, OSError) as error:
                if isinstance(error, HTTPError) and error.code == 416 and partial.exists():
                    partial.unlink()
                if attempt == 4:
                    raise WorkbenchError("download_failed", f"Unable to download {component['local_path']}: {getattr(error, 'reason', error)}", 502) from error
                time.sleep(2 ** attempt)

    def remove(self, model_id, active_jobs=None):
        model = self.registry.get(model_id)
        if active_jobs:
            raise WorkbenchError("model_in_use", "Model has queued or running jobs; cancel them before removal", 409)
        for component in model["components"].values():
            path = self.registry.path(component)
            if path.is_file():
                path.unlink()
        model_dir = self.models_dir / model_id
        try:
            model_dir.rmdir()
        except OSError:
            pass

    @property
    def models_dir(self):
        return self.registry.models_dir
