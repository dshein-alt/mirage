import http.client
import io
import json
import os
from pathlib import Path
import socket
import tarfile
from urllib.parse import quote, urlencode

from .errors import WorkbenchError


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path, timeout=60):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class EngineClient:
    def __init__(self, socket_path=None, engine=None, api_prefix="/v1.41"):
        self.socket_path = socket_path or os.environ.get("SDWB_ENGINE_SOCKET", "/run/engine.sock")
        self.engine = engine or os.environ.get("SDWB_CONTAINER_ENGINE", "podman")
        self.api_prefix = api_prefix

    def request(self, method, path, body=None, headers=None, timeout=60, stream=False):
        connection = UnixHTTPConnection(self.socket_path, timeout=timeout)
        request_headers = {"Host": "localhost", **(headers or {})}
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
            request_headers["Content-Type"] = "application/json"
        if isinstance(body, str):
            body = body.encode()
        try:
            connection.request(method, self.api_prefix + path, body=body, headers=request_headers)
            response = connection.getresponse()
            if response.status >= 400:
                detail = response.read(65536).decode("utf-8", "replace")
                connection.close()
                try:
                    detail = json.loads(detail).get("message", detail)
                except (ValueError, AttributeError):
                    pass
                raise WorkbenchError("engine_error", f"Container engine {method} {path}: {detail}", 500)
            if stream:
                return response, connection
            payload = response.read()
            content_type = response.headers.get("Content-Type", "")
            connection.close()
            if "json" in content_type and payload:
                return json.loads(payload)
            return payload
        except (OSError, http.client.HTTPException) as error:
            connection.close()
            raise WorkbenchError("engine_unavailable", f"Cannot communicate with container engine at {self.socket_path}: {error}", 503) from error

    def ping(self):
        return self.request("GET", "/_ping") in (b"OK", "OK")

    def image_exists(self, image):
        try:
            self.request("GET", f"/images/{quote(image, safe='')}/json")
            return True
        except WorkbenchError as error:
            if error.code == "engine_error":
                return False
            raise

    def ensure_runtime_image(self, image, dockerfile):
        if self.image_exists(image):
            return
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            content = Path(dockerfile).read_bytes()
            info = tarfile.TarInfo("Dockerfile.runtime")
            info.size = len(content)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(content))
        query = urlencode({"dockerfile": "Dockerfile.runtime", "t": image, "pull": "true"})
        payload = self.request(
            "POST", f"/build?{query}", buffer.getvalue(),
            headers={"Content-Type": "application/x-tar"}, timeout=600,
        )
        if b'"error"' in payload:
            raise WorkbenchError("runtime_image_build_failed", payload.decode("utf-8", "replace")[-4000:], 500)

    def create(self, name, image, command, binds, devices=(), environment=None, workdir="/output"):
        host_config = {
            "Binds": binds,
            "AutoRemove": False,
            "NetworkMode": "none",
            "Devices": [
                {"PathOnHost": str(device), "PathInContainer": str(device), "CgroupPermissions": "rwm"}
                for device in devices
            ],
        }
        if self.engine == "podman":
            host_config["GroupAdd"] = [value for value in os.environ.get("SDWB_HOST_GROUPS", "").split(",") if value]
        body = {
            "Image": image,
            "Entrypoint": ["/usr/local/bin/sd-cli"],
            "Cmd": command,
            "Env": [f"{key}={value}" for key, value in (environment or {}).items()],
            "WorkingDir": workdir,
            "User": f"{os.environ.get('SDWB_HOST_UID', '')}:{os.environ.get('SDWB_HOST_GID', '')}" if self.engine == "docker" and os.environ.get("SDWB_HOST_UID") else "",
            "Tty": True,
            "OpenStdin": False,
            "HostConfig": host_config,
        }
        result = self.request("POST", f"/containers/create?{urlencode({'name': name})}", body, timeout=120)
        return result["Id"]

    def start(self, container_id):
        self.request("POST", f"/containers/{container_id}/start")

    def logs(self, container_id):
        return self.request("GET", f"/containers/{container_id}/logs?stdout=1&stderr=1&follow=1", timeout=None, stream=True)

    def wait(self, container_id):
        result = self.request("POST", f"/containers/{container_id}/wait?condition=not-running", timeout=None)
        return int(result["StatusCode"])

    def remove(self, container_id, force=False):
        try:
            self.request("DELETE", f"/containers/{container_id}?force={'1' if force else '0'}&v=1", timeout=120)
        except WorkbenchError as error:
            if error.code != "engine_error":
                raise

    def inspect(self, container_id):
        return self.request("GET", f"/containers/{container_id}/json")
