import argparse
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .errors import WorkbenchError


class Client:
    def __init__(self):
        self.base = os.environ.get("MIRAGE_API_URL", "http://127.0.0.1:8765").rstrip("/") + "/api/v1"
        self.token = os.environ.get("MIRAGE_API_TOKEN")

    def request(self, method, path, body=None, raw=False, expect_json=True):
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            if raw:
                data = body
                headers["Content-Type"] = "application/octet-stream"
            else:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=300) as response:
                payload = response.read()
                if not expect_json:
                    return payload
                return json.loads(payload) if payload else None
        except HTTPError as error:
            try:
                detail = json.loads(error.read())["error"]
                raise WorkbenchError(detail["code"], detail["message"], error.code) from error
            except (ValueError, KeyError):
                raise WorkbenchError("http_error", f"HTTP {error.code}", error.code) from error
        except URLError as error:
            raise WorkbenchError("service_unavailable", f"Cannot reach Mirage at {self.base}: {error.reason}. Run: mirage serve", 503) from error

    def upload(self, path):
        content = Path(path).read_bytes()
        return self.request("POST", "/uploads?" + urlencode({"filename": Path(path).name}), content, raw=True)


def emit(value, as_json=False):
    if as_json or isinstance(value, (dict, list)):
        print(json.dumps(value, ensure_ascii=False, indent=2))
    else:
        print(value)


def wait(client, job_id, as_json=False):
    last = None
    while True:
        job = client.request("GET", f"/jobs/{job_id}")
        state = (job["status"], job["stage"], round(job["progress"] * 100))
        if state != last and not as_json:
            print(f"{job['id']} {job['status']} {job['stage']} {state[2]}%", file=sys.stderr, flush=True)
            last = state
        if job["status"] in ("completed", "failed", "cancelled", "interrupted"):
            emit(job, as_json)
            if job["status"] != "completed":
                raise WorkbenchError("job_unsuccessful", job.get("error") or job["status"], 1)
            return job
        time.sleep(0.5)


def build_parser():
    parser = argparse.ArgumentParser(prog="mirage", description="Local stable-diffusion.cpp workbench")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    commands = parser.add_subparsers(dest="command", required=True)

    generate = commands.add_parser("generate", help="Queue image generation")
    generate.add_argument("-p", "--prompt", required=True)
    generate.add_argument("-m", "--model", choices=("moss", "qwen", "zimage"))
    generate.add_argument("-q", "--quality")
    generate.add_argument("-r", "--resolution")
    generate.add_argument("--ref", action="append", default=[], metavar="PATH")
    generate.add_argument("--seed", type=int)
    generate.add_argument("--steps", type=int)
    generate.add_argument("--cfg-scale", type=float)
    generate.add_argument("--sampler")
    generate.add_argument("--scheduler")
    mode = generate.add_mutually_exclusive_group()
    mode.add_argument("--wait", action="store_true")
    mode.add_argument("--detach", action="store_true")

    for name in ("jobs", "gallery", "models", "config", "runtime"):
        commands.add_parser(name)
    # Subparsers are attached explicitly to keep help and validation consistent.
    jobs = parser._subparsers._group_actions[0].choices["jobs"]
    jobs_commands = jobs.add_subparsers(dest="action", required=True)
    jobs_commands.add_parser("list")
    for action in ("show", "cancel", "retry", "logs"):
        sub = jobs_commands.add_parser(action)
        sub.add_argument("id")
    jobs_wait = jobs_commands.add_parser("wait")
    jobs_wait.add_argument("id")

    gallery = parser._subparsers._group_actions[0].choices["gallery"]
    gallery_commands = gallery.add_subparsers(dest="action", required=True)
    gallery_list = gallery_commands.add_parser("list")
    gallery_list.add_argument("--search")
    gallery_list.add_argument("--model", choices=("moss", "qwen", "zimage"))
    gallery_list.add_argument("--resolution")
    gallery_list.add_argument("--quality")
    gallery_list.add_argument("--width", type=int)
    gallery_list.add_argument("--height", type=int)
    gallery_export = gallery_commands.add_parser("export")
    gallery_export.add_argument("id")
    gallery_export.add_argument("destination", type=Path)
    gallery_commands.add_parser("reindex")

    models = parser._subparsers._group_actions[0].choices["models"]
    models_commands = models.add_subparsers(dest="action", required=True)
    models_commands.add_parser("available")
    models_commands.add_parser("list")
    for action in ("install", "verify", "remove"):
        sub = models_commands.add_parser(action)
        sub.add_argument("model", choices=("moss", "qwen", "zimage"))
        if action != "remove":
            sub.add_argument("--with-editing", action="store_true")
        if action == "install":
            sub.add_argument("--wait", action="store_true")

    config = parser._subparsers._group_actions[0].choices["config"]
    config_commands = config.add_subparsers(dest="action", required=True)
    for action in ("show", "validate", "reload", "restore-defaults"):
        config_commands.add_parser(action)

    runtime = parser._subparsers._group_actions[0].choices["runtime"]
    runtime_commands = runtime.add_subparsers(dest="action", required=True)
    runtime_commands.add_parser("status")
    verify = runtime_commands.add_parser("verify")
    verify.add_argument("--devices", action="store_true")
    activate = runtime_commands.add_parser("activate")
    activate.add_argument("revision")
    runtime_commands.add_parser("rollback")
    return parser


def run(args):
    client = Client()
    if args.command == "generate":
        references = [client.upload(path)["id"] for path in args.ref]
        overrides = {key: getattr(args, key) for key in ("seed", "steps", "sampler", "scheduler") if getattr(args, key) is not None}
        if args.cfg_scale is not None:
            overrides["cfg_scale"] = args.cfg_scale
        request = {"prompt": args.prompt, "reference_ids": references, "overrides": overrides}
        for key in ("model", "quality", "resolution"):
            if getattr(args, key) is not None:
                request[key] = getattr(args, key)
        job = client.request("POST", "/jobs", request)
        return wait(client, job["id"], args.json) if args.wait else emit(job, args.json)
    if args.command == "jobs":
        if args.action == "list":
            return emit(client.request("GET", "/jobs"), args.json)
        if args.action == "show":
            return emit(client.request("GET", f"/jobs/{args.id}"), args.json)
        if args.action == "cancel":
            return emit(client.request("POST", f"/jobs/{args.id}/cancel"), args.json)
        if args.action == "retry":
            return emit(client.request("POST", f"/jobs/{args.id}/retry"), args.json)
        if args.action == "logs":
            print(client.request("GET", f"/jobs/{args.id}/logs")["log"], end="")
            return
        if args.action == "wait":
            return wait(client, args.id, args.json)
    if args.command == "gallery":
        if args.action == "list":
            query = urlencode({key: value for key, value in (("search", args.search), ("model", args.model), ("resolution", args.resolution), ("quality", args.quality), ("width", args.width), ("height", args.height)) if value})
            return emit(client.request("GET", "/images" + (f"?{query}" if query else "")), args.json)
        if args.action == "reindex":
            return emit(client.request("POST", "/gallery/reindex"), args.json)
        if args.action == "export":
            image = client.request("GET", f"/images/{args.id}")
            content = client.request("GET", f"/images/{args.id}/file", expect_json=False)
            destination = args.destination / image["filename"] if args.destination.is_dir() else args.destination
            with destination.open("xb") as stream:
                stream.write(content)
            print(destination)
            return
    if args.command == "models":
        if args.action in ("available", "list"):
            return emit(client.request("GET", "/models"), args.json)
        if args.action == "install":
            job = client.request("POST", f"/models/{args.model}/install", {"with_editing": args.with_editing})
            return wait(client, job["id"], args.json) if args.wait else emit(job, args.json)
        if args.action == "verify":
            job = client.request("POST", f"/models/{args.model}/verify", {"with_editing": args.with_editing})
            return wait(client, job["id"], args.json)
        if args.action == "remove":
            return emit(client.request("POST", f"/models/{args.model}/remove"), args.json)
    if args.command == "config":
        paths = {"show": ("GET", ""), "validate": ("POST", "/reload"), "reload": ("POST", "/reload"), "restore-defaults": ("POST", "/restore-defaults")}
        method, path = paths[args.action]
        return emit(client.request(method, "/config/generation" + path), args.json)
    if args.command == "runtime":
        if args.action == "status":
            return emit(client.request("GET", "/runtime"), args.json)
        if args.action == "verify":
            return emit(client.request("POST", f"/runtime/verify?devices={str(args.devices).lower()}"), args.json)
        if args.action == "activate":
            return emit(client.request("POST", f"/runtime/activate/{args.revision}"), args.json)
        if args.action == "rollback":
            return emit(client.request("POST", "/runtime/rollback"), args.json)


def main():
    args = build_parser().parse_args()
    try:
        run(args)
        return 0
    except WorkbenchError as error:
        print(f"mirage: {error.code}: {error.message}", file=sys.stderr)
        return 3 if error.status < 500 else 4
    except (OSError, ValueError) as error:
        print(f"mirage: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("mirage: client stopped; queued/running jobs were not cancelled", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
