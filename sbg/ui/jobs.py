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

# to_dict() re-sends the log on every 2s poll, so a long run (N wind directions x
# verbose polish output) would ship the whole thing repeatedly. Send only the tail;
# `log_total` tells the client how much it is not seeing.
LOG_TAIL_LINES = 400


class JobCancelled(Exception):
    """Raised inside a worker when the user asked it to stop."""


class Job:
    def __init__(self, job_id):
        self.id = job_id
        self.status = "pending"  # pending | running | done | error | cancelled
        # COOPERATIVE cancellation. A Python thread cannot be killed, and the heavy
        # stages (voxel remesh, decimate, boolean) are single calls into C++ that will
        # not return early -- so "Cancel" means "stop at the next stage boundary", which
        # can be several seconds to a couple of minutes away on a big domain. Saying so
        # honestly in the UI beats a button that looks instant and is not.
        self.cancel_requested = False
        self.stage = None
        # Optional finer-grained progress WITHIN a stage -- e.g. which wind direction
        # of N is being clipped. Without it a 16-direction run shows "clip" for forty
        # minutes with no sense of movement.
        self.substage = None
        self.log = []
        self.result = None
        self.error = None
        self.created_at = datetime.now(timezone.utc).isoformat()
        self._stage_started_at = None

    def cancel(self):
        """Request a stop. Returns False if the job already finished."""
        with _lock:
            if self.status in ("done", "error", "cancelled"):
                return False
            self.cancel_requested = True
            self.log.append("[cancel] requested -- stopping at the next stage boundary")
            return True

    def raise_if_cancelled(self):
        """Call between stages. The runner turns this into status='cancelled'."""
        if self.cancel_requested:
            raise JobCancelled()

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
            self.substage = None
            _cancel = self.cancel_requested
        # Outside the lock: every stage transition is a cancellation checkpoint, so a
        # pipeline gets this for free without any per-stage checking code.
        if _cancel:
            raise JobCancelled()
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
                "substage": self.substage,
                "cancel_requested": self.cancel_requested,
                "log": self.log[-LOG_TAIL_LINES:],
                "log_total": len(self.log),
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
        except JobCancelled:
            # A user-requested stop is not a failure -- distinct status, no traceback.
            job.status = "cancelled"
            job.log_line("[cancel] stopped")
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
