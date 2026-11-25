from contextlib import contextmanager
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
    runtime_build TEXT,
    runtime_image_id TEXT,
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
    dimensions_verified INTEGER NOT NULL DEFAULT 1,
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
CREATE TABLE IF NOT EXISTS maintenance (
    id INTEGER PRIMARY KEY CHECK(id=1),
    active INTEGER NOT NULL DEFAULT 0,
    owner TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admitted_writes (
    token TEXT PRIMARY KEY,
    started_at TEXT NOT NULL
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
            columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(images)")}
            if "dimensions_verified" not in columns:
                self.connection.execute("ALTER TABLE images ADD COLUMN dimensions_verified INTEGER NOT NULL DEFAULT 0")
            job_columns = {row["name"] for row in self.connection.execute("PRAGMA table_info(jobs)")}
            for column in ("runtime_build", "runtime_image_id"):
                if column not in job_columns:
                    self.connection.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")
            self.connection.execute("INSERT OR IGNORE INTO migrations(version, applied_at) VALUES(1, ?)", (utcnow(),))
            self.connection.execute("INSERT OR IGNORE INTO migrations(version, applied_at) VALUES(2, ?)", (utcnow(),))
            self.connection.execute("INSERT OR IGNORE INTO migrations(version, applied_at) VALUES(3, ?)", (utcnow(),))
            self.connection.execute("INSERT OR IGNORE INTO migrations(version, applied_at) VALUES(4, ?)", (utcnow(),))
            self.connection.execute("INSERT OR IGNORE INTO maintenance(id, active, updated_at) VALUES(1, 0, ?)", (utcnow(),))
            self.connection.execute("DELETE FROM admitted_writes")

    def close(self):
        with self.lock:
            self.connection.close()

    def _assert_open_locked(self):
        row = self.connection.execute("SELECT active FROM maintenance WHERE id=1").fetchone()
        if row and row["active"]:
            raise WorkbenchError("maintenance", "Mirage is undergoing maintenance; shared-state changes are temporarily closed", 409)

    @contextmanager
    def write_admission(self):
        token = uuid.uuid4().hex
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_open_locked()
                self.connection.execute("INSERT INTO admitted_writes(token, started_at) VALUES(?, ?)", (token, utcnow()))
                self.connection.execute("COMMIT")
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise
        try:
            yield token
        finally:
            with self.lock:
                self.connection.execute("DELETE FROM admitted_writes WHERE token=?", (token,))

    def active_write_count(self):
        with self.lock:
            return self.connection.execute("SELECT COUNT(*) FROM admitted_writes").fetchone()[0]

    def enter_maintenance(self, owner):
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                row = self.connection.execute("SELECT active, owner FROM maintenance WHERE id=1").fetchone()
                if row and row["active"]:
                    if row["owner"] == owner:
                        self.connection.execute("COMMIT")
                        return self.maintenance_status()
                    raise WorkbenchError("maintenance_active", "Another maintenance operation is active", 409)
                busy = self.connection.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running')").fetchone()[0]
                if busy:
                    raise WorkbenchError("installation_busy", "Cannot maintain an installation with queued or running jobs", 409)
                self.connection.execute("UPDATE maintenance SET active=1, owner=?, updated_at=? WHERE id=1", (owner, utcnow()))
                self.connection.execute("COMMIT")
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise
        return self.maintenance_status()

    def ensure_maintenance(self, owner):
        with self.lock:
            row = self.connection.execute("SELECT active FROM maintenance WHERE id=1").fetchone()
            if not row or not row["active"]:
                self.connection.execute("UPDATE maintenance SET active=1, owner=?, updated_at=? WHERE id=1", (owner, utcnow()))

    def leave_maintenance(self, owner=None):
        with self.lock:
            if owner is not None:
                row = self.connection.execute("SELECT owner FROM maintenance WHERE id=1").fetchone()
                if row and row["owner"] not in (None, owner):
                    raise WorkbenchError("maintenance_active", "Another maintenance operation is active", 409)
            self.connection.execute("UPDATE maintenance SET active=0, owner=NULL, updated_at=? WHERE id=1", (utcnow(),))

    def maintenance_status(self):
        with self.lock:
            row = self.connection.execute("SELECT active, owner, updated_at FROM maintenance WHERE id=1").fetchone()
            writes = self.connection.execute("SELECT COUNT(*) FROM admitted_writes").fetchone()[0]
        return {"active": bool(row and row["active"]), "owner": row["owner"] if row else None, "updated_at": row["updated_at"] if row else None, "admitted_writes": writes}

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

    def create_job(self, kind, request, resolved=None, model=None, retry_of=None, runtime_revision=None, runtime_build=None, runtime_image_id=None, log_path=None):
        job_id = str(uuid.uuid4())
        runtime_build = runtime_build or (resolved or {}).get("runtime_build")
        runtime_image_id = runtime_image_id or (resolved or {}).get("runtime_image_id")
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_open_locked()
                self.connection.execute(
                    "INSERT INTO jobs(id, kind, status, created_at, model, request_json, resolved_json, retry_of, runtime_revision, runtime_build, runtime_image_id, log_path) VALUES(?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (job_id, kind, utcnow(), model, json.dumps(request, ensure_ascii=False), json.dumps(resolved, ensure_ascii=False) if resolved else None, retry_of, runtime_revision, runtime_build, runtime_image_id, log_path),
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
            if self.connection.execute("SELECT active FROM maintenance WHERE id=1").fetchone()[0]:
                self.connection.execute("COMMIT")
                return None
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
            self._assert_open_locked()
            self.connection.execute(
                "INSERT INTO inputs(id, filename, origin, sha256, width, height, bytes, mime, created_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (input_id, filename, origin, sha256, width, height, size, mime, utcnow()),
            )
        return self.get_input(input_id)

    def get_input(self, input_id, include_deleted=False):
        query = "SELECT * FROM inputs WHERE id=?" + ("" if include_deleted else " AND deleted=0")
        with self.lock:
            row = self.connection.execute(query, (input_id,)).fetchone()
        if row is None:
            raise WorkbenchError("input_not_found", f"Reference image not found: {input_id}", 404)
        return dict(row)

    def delete_input(self, input_id):
        record = self.get_input(input_id)
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_open_locked()
                if self.input_in_use(input_id):
                    raise WorkbenchError("input_in_use", "A referenced input image cannot be deleted", 409)
                self.connection.execute("UPDATE inputs SET deleted=1 WHERE id=?", (input_id,))
                self.connection.execute("COMMIT")
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise
        return record

    def add_image(self, job, path, sha256, image_id=None, metadata=None):
        resolved = job["resolved"]
        request = job["request"]
        image_id = image_id or str(uuid.uuid4())
        metadata = metadata or {**resolved, "prompt": request["prompt"], "image": path.name, "job_id": job["id"], "timestamp": utcnow(), "elapsed_seconds": job.get("duration_seconds")}
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_open_locked()
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
        result["dimensions_verified"] = bool(result["dimensions_verified"])
        return result

    def list_images(self, search=None, model=None, resolution=None, quality=None, width=None, height=None, limit=200):
        clauses = ["deleted=0"]
        values = []
        if search:
            clauses.append("prompt LIKE ?")
            values.append(f"%{search}%")
        for field, value in (("model", model), ("resolution", resolution), ("quality", quality), ("width", width), ("height", height)):
            if value is not None and value != "":
                clauses.append(f"{field}=?")
                values.append(value)
        values.append(limit)
        with self.lock:
            rows = self.connection.execute(
                f"SELECT * FROM images WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ?", values
            ).fetchall()
        return [self._image(row) for row in rows]

    def unverified_images(self):
        with self.lock:
            rows = self.connection.execute("SELECT * FROM images WHERE dimensions_verified=0").fetchall()
        return [self._image(row) for row in rows]

    def verify_image_dimensions(self, image_id, width, height):
        with self.lock:
            self.connection.execute(
                "UPDATE images SET width=?, height=?, dimensions_verified=1 WHERE id=?",
                (width, height, image_id),
            )

    def image_dimensions(self):
        with self.lock:
            rows = self.connection.execute(
                "SELECT DISTINCT width, height FROM images WHERE deleted=0 AND dimensions_verified=1 ORDER BY width, height"
            ).fetchall()
        return [{"width": row["width"], "height": row["height"]} for row in rows]

    def trash_image(self, image_id, permanent=False):
        image = self.get_image(image_id, include_deleted=True)
        with self.lock:
            self._assert_open_locked()
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
            jobs = self.connection.execute("SELECT COUNT(*) FROM job_inputs WHERE input_id=?", (input_id,)).fetchone()[0]
            images = self.connection.execute("SELECT COUNT(*) FROM image_inputs WHERE input_id=?", (input_id,)).fetchone()[0]
        return bool(jobs or images)

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
