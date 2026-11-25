from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
import os
import secrets
import uuid

from PIL import Image, UnidentifiedImageError

from .errors import WorkbenchError
from .util import atomic_write_json, file_sha256, read_json

FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp", "BMP": "image/bmp"}
Image.MAX_IMAGE_PIXELS = 8192 * 8192


class MediaService:
    def __init__(self, paths, database, max_upload_bytes=20971520):
        self.paths = paths
        self.database = database
        self.max_upload_bytes = max_upload_bytes

    def upload(self, content, origin="upload"):
        if not content:
            raise WorkbenchError("upload_empty", "Reference image is empty")
        if len(content) > self.max_upload_bytes:
            raise WorkbenchError("upload_too_large", f"Reference image exceeds {self.max_upload_bytes} bytes", 413)
        try:
            with Image.open(BytesIO(content)) as source:
                if source.format not in FORMATS:
                    raise WorkbenchError("upload_type", "Only PNG, JPEG, WebP and BMP images are supported")
                if not 32 <= source.width <= 8192 or not 32 <= source.height <= 8192:
                    raise WorkbenchError("upload_dimensions", "Image dimensions must be between 32 and 8192 pixels")
                width, height = source.size
                source.load()
                image = source.convert("RGBA") if "A" in source.getbands() else source.convert("RGB")
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
            raise WorkbenchError("upload_invalid", "Uploaded content is not a valid supported image") from error
        filename = f"{secrets.token_hex(16)}.png"
        destination = self.paths.inputs / filename
        image.save(destination, format="PNG")
        return self.database.create_input(filename, Path(origin).name[:255], file_sha256(destination), width, height, destination.stat().st_size, "image/png")

    def input_path(self, input_id):
        record = self.database.get_input(input_id)
        path = (self.paths.inputs / record["filename"]).resolve()
        if path.parent != self.paths.inputs.resolve() or not path.is_file():
            raise WorkbenchError("input_missing", "Reference image file is missing", 500)
        return path

    @staticmethod
    def _fsync(path):
        with path.open("rb") as stream:
            os.fsync(stream.fileno())

    def publish(self, job, temporary, final):
        try:
            with Image.open(temporary) as image:
                image.verify()
            with Image.open(temporary) as image:
                if image.format != "PNG":
                    raise WorkbenchError("output_invalid", "sd-cli did not produce a PNG", 500)
                thumbnail_image = image.convert("RGB")
                thumbnail_image.thumbnail((384, 384))
        except (UnidentifiedImageError, OSError) as error:
            raise WorkbenchError("output_invalid", "sd-cli produced an unreadable output image", 500) from error
        image_id = str(uuid.uuid4())
        metadata = {
            **job["resolved"],
            "prompt": job["request"]["prompt"],
            "image": final.name,
            "job_id": job["id"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": job.get("duration_seconds"),
        }
        thumbnail = self.paths.thumbnails / f"{final.stem}.jpg"
        thumbnail_temp = self.paths.thumbnails / f".{final.stem}.{image_id}.tmp.jpg"
        sidecar = final.with_suffix(".json")
        sidecar_temp = self.paths.outputs / f".{final.stem}.{image_id}.tmp.json"
        state_path = self.paths.outputs / f".{final.name}.publication.json"
        state = {
            "schema_version": 1,
            "job_id": job["id"],
            "image_id": image_id,
            "final": final.name,
            "temporary": temporary.name,
            "thumbnail": thumbnail.name,
            "thumbnail_temp": thumbnail_temp.name,
            "sidecar_temp": sidecar_temp.name,
            "metadata": metadata,
            "sha256": file_sha256(temporary),
            "duration_seconds": job.get("duration_seconds"),
        }
        thumbnail_image.save(thumbnail_temp, "JPEG", quality=88)
        self._fsync(thumbnail_temp)
        atomic_write_json(sidecar_temp, metadata)
        atomic_write_json(state_path, state)
        return self.complete_publication(state_path)

    def complete_publication(self, state_path):
        state = read_json(state_path)
        names = [state[key] for key in ("final", "temporary", "thumbnail", "thumbnail_temp", "sidecar_temp")]
        if any(Path(name).name != name for name in names):
            raise WorkbenchError("publication_invalid", "Unsafe publication state path", 500)
        final = self.paths.outputs / state["final"]
        temporary = self.paths.outputs / state["temporary"]
        thumbnail = self.paths.thumbnails / state["thumbnail"]
        thumbnail_temp = self.paths.thumbnails / state["thumbnail_temp"]
        sidecar_temp = self.paths.outputs / state["sidecar_temp"]
        if temporary.exists():
            if final.exists() and final.stat().st_size:
                raise WorkbenchError("publication_conflict", f"Both temporary and published PNG exist for {final.name}", 409)
            os.replace(temporary, final)
        if thumbnail_temp.exists():
            os.replace(thumbnail_temp, thumbnail)
        if sidecar_temp.exists():
            os.replace(sidecar_temp, final.with_suffix(".json"))
        for path in (final, thumbnail, final.with_suffix(".json")):
            if not path.is_file() or not path.stat().st_size:
                raise WorkbenchError("publication_incomplete", f"Cannot recover incomplete publication file: {path.name}", 500)
        record = self.database.find_image(final.name)
        if record is None:
            job = self.database.get_job(state["job_id"])
            record = self.database.add_image(
                job, final, state["sha256"], image_id=state["image_id"], metadata=state["metadata"],
            )
        state_path.unlink(missing_ok=True)
        return record

    def reconcile_publications(self):
        recovered = []
        for state_path in sorted(self.paths.outputs.glob(".*.publication.json")):
            record = self.complete_publication(state_path)
            recovered.append(record["id"])
            job = self.database.get_job(record["job_id"])
            if job["status"] == "running":
                self.database.update_job(
                    job["id"], status="completed", progress=1.0, stage="completed",
                    finished_at=datetime.now(timezone.utc).isoformat(),
                    duration_seconds=record["metadata"].get("elapsed_seconds"), output_id=record["id"],
                )
        return recovered

    def image_path(self, image_id):
        record = self.database.get_image(image_id)
        path = (self.paths.outputs / record["filename"]).resolve()
        if path.parent != self.paths.outputs.resolve() or not path.is_file():
            raise WorkbenchError("image_missing", "Image file is missing", 404)
        return path

    def thumbnail_path(self, image_id):
        record = self.database.get_image(image_id)
        path = (self.paths.thumbnails / f"{Path(record['filename']).stem}.jpg").resolve()
        if path.parent != self.paths.thumbnails.resolve() or not path.is_file():
            raise WorkbenchError("thumbnail_missing", "Thumbnail is missing", 404)
        return path

    def reindex(self):
        imported = []
        for path in self.paths.outputs.glob("*.png"):
            if path.name.startswith(".") or not path.stat().st_size:
                continue
            try:
                with Image.open(path) as image:
                    width, height = image.size
                sidecar = path.with_suffix(".json")
                metadata = read_json(sidecar) if sidecar.is_file() else {}
                job = {
                    "id": None,
                    "request": {"prompt": metadata.get("prompt", "")},
                    "resolved": {
                        "model": metadata.get("model", "unknown"),
                        "resolution": metadata.get("resolution", "custom"),
                        "quality": metadata.get("quality", "custom"),
                        "width": width,
                        "height": height,
                    },
                    "duration_seconds": metadata.get("elapsed_seconds"),
                }
                if not self.database.find_image(path.name):
                    record = self.database.add_image(job, path, file_sha256(path))
                    imported.append(record["id"])
            except (OSError, ValueError, UnidentifiedImageError):
                continue
        return imported
