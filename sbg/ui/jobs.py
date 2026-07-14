"""In-process async job manager for long-running pipeline work (the STL
build chain takes minutes -- see pipeline.py). Runs jobs on a background
thread so they don't block the FastAPI/uvicorn event loop, with pollable
status via GET /api/pipeline/jobs/{id}.

Single-process, single-user model, matching this project's own established
"share via Drive" single-instance-per-scientist assumption (see project
plan) -- a plain in-memory dict is enough, no real task queue/broker needed.
"""
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

_executor = ThreadPoolExecutor(max_workers=2)
_jobs = {}
_lock = threading.Lock()


class Job:
    def __init__(self, job_id):
        self.id = job_id
        self.status = "pending"  # pending | running | done | error
        self.stage = None
        self.log = []
        self.result = None
        self.error = None
        self.created_at = datetime.now(timezone.utc).isoformat()
        self._stage_started_at = None

    def set_stage(self, stage):
        """Automatically logs how long the PREVIOUS stage took, every time a
        new one starts -- real measured wall-clock time, not eyeballed from
        when successive log lines happened to print. Every stage gets this
        for free just by calling set_stage(); no per-callsite timing code
        needed in pipeline.py.
        """
        with _lock:
            now = time.perf_counter()
            if self.stage is not None and self._stage_started_at is not None:
                elapsed = now - self._stage_started_at
                self.log.append(f"[{self.stage}] took {elapsed:.1f}s")
            self.stage = stage
            self._stage_started_at = now
            self.log.append(stage)

    def log_line(self, line):
        with _lock:
            self.log.append(line)

    def to_dict(self):
        with _lock:
            return {
                "id": self.id,
                "status": self.status,
                "stage": self.stage,
                "log": list(self.log),
                "result": self.result,
                "error": self.error,
                "created_at": self.created_at,
            }


def create_job(fn, *args, **kwargs):
    """Starts fn(job, *args, **kwargs) on a background thread. fn should call
    job.set_stage(...)/job.log_line(...) as it progresses and return a
    JSON-serializable dict to be stored as job.result, or raise on failure
    (the exception is caught, recorded, and re-raised nowhere -- callers
    poll job status instead of awaiting this call).
    """
    job = Job(str(uuid.uuid4()))
    with _lock:
        _jobs[job.id] = job

    def _run():
        job.status = "running"
        try:
            job.result = fn(job, *args, **kwargs)
            job.status = "done"
        except Exception as e:
            job.status = "error"
            job.error = str(e)
            job.log_line(f"ERROR: {e}")
            job.log_line(traceback.format_exc())

    _executor.submit(_run)
    return job


def get_job(job_id):
    with _lock:
        return _jobs.get(job_id)
