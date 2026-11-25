import json
from pathlib import Path
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

from .errors import WorkbenchError


def utcnow():
    return datetime.now(timezone.utc).isoformat()


SCHEMA = """
CREATE TABLE IF NOT EXISTS migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    model TEXT,
    request_json TEXT NOT NULL,
    resolved_json TEXT,
    progress REAL NOT NULL DEFAULT 0,
    stage TEXT NOT NULL DEFAULT 'waiting',
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    retry_of TEXT REFERENCES jobs(id),
    runtime_revision TEXT,
    output_id TEXT,
    error TEXT,
    log_path TEXT,
    container_id TEXT,
    duration_seconds REAL
);
CREATE INDEX IF NOT EXISTS jobs_queue_idx ON jobs(kind, status, created_at);
CREATE TABLE IF NOT EXISTS inputs (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL UNIQUE,
    origin TEXT,
    sha256 TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    bytes INTEGER NOT NULL,
    mime TEXT NOT NULL,
    created_at TEXT NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS job_inputs (
    job_id TEXT NOT NULL REFERENCES jobs(id),
    input_id TEXT NOT NULL REFERENCES inputs(id),
    PRIMARY KEY(job_id, input_id)
);
CREATE TABLE IF NOT EXISTS images (
    id TEXT PRIMARY KEY,
    job_id TEXT REFERENCES jobs(id),
    filename TEXT NOT NULL UNIQUE,
    prompt TEXT NOT NULL,
    model TEXT NOT NULL,
    resolution TEXT NOT NULL,
    quality TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS images_created_idx ON images(created_at DESC);
CREATE TABLE IF NOT EXISTS image_inputs (
    image_id TEXT NOT NULL REFERENCES images(id),
    input_id TEXT NOT NULL REFERENCES inputs(id),
    PRIMARY KEY(image_id, input_id)
);
CREATE TABLE IF NOT EXISTS events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        with self.lock:
            self.connection.executescript(SCHEMA)
            self.connection.execute("INSERT OR IGNORE INTO migrations(version, applied_at) VALUES(1, ?)", (utcnow(),))

    def close(self):
        with self.lock:
            self.connection.close()

    @staticmethod
    def _job(row):
        if row is None:
            return None
        result = dict(row)
        result["request"] = json.loads(result.pop("request_json"))
        result["resolved"] = json.loads(result.pop("resolved_json")) if result["resolved_json"] else None
        result["cancel_requested"] = bool(result["cancel_requested"])
        result.pop("container_id", None)
        return result

    def create_job(self, kind, request, resolved=None, model=None, retry_of=None, runtime_revision=None, log_path=None):
        job_id = str(uuid.uuid4())
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self.connection.execute(
                    "INSERT INTO jobs(id, kind, status, created_at, model, request_json, resolved_json, retry_of, runtime_revision, log_path) VALUES(?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)",
                    (job_id, kind, utcnow(), model, json.dumps(request, ensure_ascii=False), json.dumps(resolved, ensure_ascii=False) if resolved else None, retry_of, runtime_revision, log_path),
                )
                if kind == "generation":
                    for input_id in request.get("reference_ids", []):
                        self.connection.execute("INSERT INTO job_inputs(job_id, input_id) VALUES(?, ?)", (job_id, input_id))
                self._event_locked(job_id, "job", {"id": job_id, "status": "queued"})
                self.connection.execute("COMMIT")
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise
        return self.get_job(job_id)

    def get_job(self, job_id):
        with self.lock:
            row = self.connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise WorkbenchError("job_not_found", f"Job not found: {job_id}", 404)
        return self._job(row)

    def get_job_with_container(self, job_id):
        with self.lock:
            row = self.connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise WorkbenchError("job_not_found", f"Job not found: {job_id}", 404)
        result = self._job(row)
        result["container_id"] = row["container_id"]
        return result

    def list_jobs(self, limit=100, status=None):
        with self.lock:
            if status:
                rows = self.connection.execute("SELECT * FROM jobs WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit)).fetchall()
            else:
                rows = self.connection.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._job(row) for row in rows]

    def next_job(self, kind):
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT id FROM jobs WHERE kind=? AND status='queued' AND cancel_requested=0 ORDER BY created_at LIMIT 1", (kind,)
            ).fetchone()
            if row is None:
                self.connection.execute("COMMIT")
                return None
            now = utcnow()
            self.connection.execute("UPDATE jobs SET status='running', started_at=?, stage='loading' WHERE id=?", (now, row["id"]))
            self._event_locked(row["id"], "job", {"id": row["id"], "status": "running", "stage": "loading"})
            self.connection.execute("COMMIT")
        return self.get_job(row["id"])

    def update_job(self, job_id, **fields):
        allowed = {"status", "progress", "stage", "error", "finished_at", "duration_seconds", "output_id", "container_id", "started_at"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown job fields: {unknown}")
        if not fields:
            return
        with self.lock:
            assignments = ",".join(f"{key}=?" for key in fields)
            self.connection.execute(f"UPDATE jobs SET {assignments} WHERE id=?", (*fields.values(), job_id))
            public = {key: value for key, value in fields.items() if key != "container_id"}
            public["id"] = job_id
            self._event_locked(job_id, "job", public)

    def request_cancel(self, job_id):
        job = self.get_job(job_id)
        if job["status"] not in ("queued", "running"):
            raise WorkbenchError("job_not_cancellable", "Only queued or running jobs can be cancelled", 409)
        with self.lock:
            if job["status"] == "queued":
                self.connection.execute("UPDATE jobs SET cancel_requested=1, status='cancelled', finished_at=?, error='Cancelled before start' WHERE id=?", (utcnow(), job_id))
            else:
                self.connection.execute("UPDATE jobs SET cancel_requested=1, stage='cancelling' WHERE id=?", (job_id,))
            self._event_locked(job_id, "job", {"id": job_id, "status": "cancelled" if job["status"] == "queued" else "cancelling"})
        return self.get_job(job_id)

    def set_container(self, job_id, container_id):
        self.update_job(job_id, container_id=container_id)

    def reconcile(self):
        with self.lock:
            rows = self.connection.execute("SELECT id FROM jobs WHERE status='running'").fetchall()
            for row in rows:
                self.connection.execute(
                    "UPDATE jobs SET status='interrupted', finished_at=?, error='Service restarted while the job was running; retry explicitly', container_id=NULL WHERE id=?",
                    (utcnow(), row["id"]),
                )
                self._event_locked(row["id"], "job", {"id": row["id"], "status": "interrupted"})
        return len(rows)

    def output_name_exists(self, filename):
        with self.lock:
            row = self.connection.execute("SELECT 1 FROM images WHERE filename=?", (filename,)).fetchone()
        return row is not None

    def active_model_jobs(self, model_id):
        with self.lock:
            return [self._job(row) for row in self.connection.execute(
                "SELECT * FROM jobs WHERE model=? AND status IN ('queued', 'running')", (model_id,)
            ).fetchall()]

    def create_input(self, filename, origin, sha256, width, height, size, mime):
        input_id = str(uuid.uuid4())
        with self.lock:
            self.connection.execute(
                "INSERT INTO inputs(id, filename, origin, sha256, width, height, bytes, mime, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (input_id, filename, origin, sha256, width, height, size, mime, utcnow()),
            )
        return self.get_input(input_id)

    def get_input(self, input_id):
        with self.lock:
            row = self.connection.execute("SELECT * FROM inputs WHERE id=? AND deleted=0", (input_id,)).fetchone()
        if row is None:
            raise WorkbenchError("input_not_found", f"Reference image not found: {input_id}", 404)
        return dict(row)

    def add_image(self, job, path, sha256, image_id=None, metadata=None):
        resolved = job["resolved"]
        request = job["request"]
        image_id = image_id or str(uuid.uuid4())
        metadata = metadata or {**resolved, "prompt": request["prompt"], "image": path.name, "job_id": job["id"], "timestamp": utcnow(), "elapsed_seconds": job.get("duration_seconds")}
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self.connection.execute(
                    "INSERT INTO images(id, job_id, filename, prompt, model, resolution, quality, width, height, created_at, metadata_json, sha256) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (image_id, job["id"], path.name, request["prompt"], resolved["model"], resolved["resolution"], resolved["quality"], resolved["width"], resolved["height"], utcnow(), json.dumps(metadata, ensure_ascii=False), sha256),
                )
                for input_id in request.get("reference_ids", []):
                    self.connection.execute("INSERT INTO image_inputs(image_id, input_id) VALUES(?, ?)", (image_id, input_id))
                self.connection.execute("UPDATE jobs SET output_id=? WHERE id=?", (image_id, job["id"]))
                self.connection.execute("COMMIT")
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise
        return self.get_image(image_id)

    def find_image(self, filename):
        with self.lock:
            row = self.connection.execute("SELECT * FROM images WHERE filename=?", (filename,)).fetchone()
        return self._image(row) if row else None

    def get_image(self, image_id, include_deleted=False):
        query = "SELECT * FROM images WHERE id=?" + ("" if include_deleted else " AND deleted=0")
        with self.lock:
            row = self.connection.execute(query, (image_id,)).fetchone()
        if row is None:
            raise WorkbenchError("image_not_found", f"Image not found: {image_id}", 404)
        return self._image(row)

    @staticmethod
    def _image(row):
        result = dict(row)
        result["metadata"] = json.loads(result.pop("metadata_json"))
        result["deleted"] = bool(result["deleted"])
        return result

    def list_images(self, search=None, model=None, resolution=None, quality=None, limit=200):
        clauses = ["deleted=0"]
        values = []
        if search:
            clauses.append("prompt LIKE ?")
            values.append(f"%{search}%")
        for field, value in (("model", model), ("resolution", resolution), ("quality", quality)):
            if value:
                clauses.append(f"{field}=?")
                values.append(value)
        values.append(limit)
        with self.lock:
            rows = self.connection.execute(
                f"SELECT * FROM images WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ?", values
            ).fetchall()
        return [self._image(row) for row in rows]

    def trash_image(self, image_id, permanent=False):
        image = self.get_image(image_id, include_deleted=True)
        with self.lock:
            if permanent:
                if not image["deleted"]:
                    raise WorkbenchError("image_not_trashed", "Move the image to trash before permanent deletion", 409)
                referenced = self.connection.execute("SELECT COUNT(*) FROM image_inputs WHERE image_id=?", (image_id,)).fetchone()[0]
                self.connection.execute("DELETE FROM image_inputs WHERE image_id=?", (image_id,))
                self.connection.execute("DELETE FROM images WHERE id=?", (image_id,))
            else:
                self.connection.execute("UPDATE images SET deleted=1 WHERE id=?", (image_id,))
        return image

    def input_in_use(self, input_id):
        with self.lock:
            active = self.connection.execute(
                "SELECT COUNT(*) FROM job_inputs ji JOIN jobs j ON j.id=ji.job_id WHERE ji.input_id=? AND j.status IN ('queued','running')", (input_id,)
            ).fetchone()[0]
            images = self.connection.execute("SELECT COUNT(*) FROM image_inputs WHERE input_id=?", (input_id,)).fetchone()[0]
        return bool(active or images)

    def _event_locked(self, job_id, event_type, payload):
        self.connection.execute(
            "INSERT INTO events(job_id, type, payload_json, created_at) VALUES(?, ?, ?, ?)",
            (job_id, event_type, json.dumps(payload, ensure_ascii=False), utcnow()),
        )

    def events_after(self, sequence=0, limit=100):
        with self.lock:
            rows = self.connection.execute("SELECT * FROM events WHERE sequence>? ORDER BY sequence LIMIT ?", (sequence, limit)).fetchall()
        return [{"sequence": row["sequence"], "job_id": row["job_id"], "type": row["type"], "payload": json.loads(row["payload_json"]), "created_at": row["created_at"]} for row in rows]

    def backup(self, destination):
        destination = Path(destination)
        with self.lock:
            target = sqlite3.connect(destination)
            try:
                self.connection.backup(target)
            finally:
                target.close()
