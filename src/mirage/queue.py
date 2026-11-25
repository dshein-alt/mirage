from datetime import datetime, timezone
import threading
import time

from .errors import WorkbenchError


class JobQueue:
    def __init__(self, database, runner, downloader):
        self.database = database
        self.runner = runner
        self.downloader = downloader
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.cancellations = {}
        self.threads = []
        self.lock = threading.RLock()

    def start(self):
        self.runner.media.reconcile_publications()
        for job in self.database.list_jobs(status="running"):
            record = self.database.get_job_with_container(job["id"])
            if record.get("container_id"):
                self.runner.engine.remove(record["container_id"], force=True)
        interrupted = self.database.reconcile()
        (self.runner.paths.data / ".active-job").unlink(missing_ok=True)
        if interrupted:
            print(f"Marked {interrupted} interrupted job(s); retry them explicitly", flush=True)
        for kind in ("generation", "model_install"):
            thread = threading.Thread(target=self.worker, args=(kind,), name=f"mirage-{kind}", daemon=True)
            thread.start()
            self.threads.append(thread)

    def stop(self):
        self.stopping.set()
        self.wake.set()
        for thread in self.threads:
            if thread is not threading.current_thread():
                thread.join(timeout=2)

    def notify(self):
        self.wake.set()

    def cancellation(self, job_id):
        with self.lock:
            return self.cancellations.setdefault(job_id, threading.Event())

    def cancel(self, job_id):
        job = self.database.request_cancel(job_id)
        self.cancellation(job_id).set()
        if job["status"] == "cancelled":
            self.cleanup_reservation(job)
        elif job["kind"] == "generation":
            self.runner.cancel(job_id)
        self.wake.set()
        return self.database.get_job(job_id)

    def retry(self, job_id):
        original = self.database.get_job(job_id)
        if original["status"] not in ("failed", "cancelled", "interrupted", "completed"):
            raise WorkbenchError("job_not_retryable", "Only a finished job can be retried", 409)
        resolved = dict(original["resolved"])
        if original["kind"] == "generation":
            from .output import reserve_output

            final = reserve_output(
                self.runner.paths.outputs,
                self.runner.registry.get(original["model"])["label"],
                resolved["resolution"], resolved["quality"],
                self.database.output_name_exists,
            )
            resolved["output_name"] = final.name
        job = self.database.create_job(
            original["kind"], original["request"], resolved, original["model"],
            retry_of=job_id, runtime_revision=original["runtime_revision"], log_path=original["log_path"].replace(".log", f"-retry-{int(time.time())}.log"),
        )
        self.wake.set()
        return job

    def worker(self, kind):
        while not self.stopping.is_set():
            job = self.database.next_job(kind)
            if job is None:
                self.wake.wait(timeout=0.5)
                self.wake.clear()
                continue
            cancel = self.cancellation(job["id"])
            try:
                if kind == "generation":
                    record, duration = self.runner.run(job, cancel)
                    self.database.update_job(job["id"], status="completed", progress=1.0, stage="completed", finished_at=datetime.now(timezone.utc).isoformat(), duration_seconds=duration, output_id=record["id"])
                else:
                    self.run_install(job, cancel)
                    self.database.update_job(job["id"], status="completed", progress=1.0, stage="completed", finished_at=datetime.now(timezone.utc).isoformat())
            except WorkbenchError as error:
                cancelled = cancel.is_set() or error.code in {"job_cancelled", "download_cancelled"}
                self.database.update_job(job["id"], status="cancelled" if cancelled else "failed", stage="cancelled" if cancelled else "failed", error=error.message, finished_at=datetime.now(timezone.utc).isoformat())
                self.cleanup_reservation(job)
            except Exception as error:
                self.database.update_job(job["id"], status="failed", stage="failed", error=f"{type(error).__name__}: {error}", finished_at=datetime.now(timezone.utc).isoformat())
                self.cleanup_reservation(job)
            finally:
                with self.lock:
                    self.cancellations.pop(job["id"], None)

    def run_install(self, job, cancel):
        editing = bool(job["resolved"].get("editing"))
        if job["resolved"].get("verify"):
            self.database.update_job(job["id"], stage="hashing", progress=0.1)
            self.runner.registry.verify(job["model"], editing=editing, checksum=True)
            return
        last = {"value": -1.0}

        def progress(completed, total, filename, stage):
            value = completed / total if total else 1.0
            if value - last["value"] >= 0.005 or value == 1.0 or stage != "downloading":
                self.database.update_job(job["id"], progress=value, stage=stage, error=None)
                self.database.update_job(job["id"], stage=f"{stage}:{filename}")
                last["value"] = value

        result = self.downloader.install(job["model"], editing=editing, progress=progress, cancel=cancel)
        job["resolved"]["installed"] = result["installed"]

    def cleanup_reservation(self, job):
        if job["kind"] != "generation":
            return
        output = self.runner.paths.outputs / job["resolved"]["output_name"]
        if output.is_file() and output.stat().st_size == 0:
            output.unlink(missing_ok=True)
