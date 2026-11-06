"""Jobs: one directory and one thread each, a registry with a lock.

A job is created in state queued and runs in the manager's thread pool:
probing, downloading, converting, then done, failed or cancelled. Its files
live in <root>/<id>/; the files of done jobs stay for keep seconds, the files
of failed and cancelled jobs go at once. Nothing survives a restart: start()
clears the root.
"""

from __future__ import annotations

import logging
import mimetypes
import secrets
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from yt_dlp.utils import DownloadCancelled

from .fetcher import Fetcher, FetchError, clean_message

LOGGER = logging.getLogger("media_fetcher.jobs")

ACTIVE = frozenset({"queued", "probing", "downloading", "converting"})
CONTENT_TYPES = {
    "m4a": "audio/mp4", "mp3": "audio/mpeg", "opus": "audio/ogg", "ogg": "audio/ogg", "flac": "audio/flac",
    "wav": "audio/wav", "aac": "audio/aac", "mp4": "video/mp4", "mkv": "video/x-matroska",
    "webm": "video/webm", "mov": "video/quicktime",
}


def content_type(path: Path) -> str:
    ext = path.suffix.lstrip(".").lower()
    return CONTENT_TYPES.get(ext) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None


class NotReady(Exception):
    """The job has no file yet, or never will."""


class Job:
    """One job; its fields change only under the manager's lock, which the
    report methods take themselves (the fetcher calls them from the job's
    thread)."""

    def __init__(self, job_id: str, url: str, kind: str, fmt: str, max_duration: int | None,
                 directory: Path, created: float, lock: threading.Lock) -> None:
        self.id = job_id
        self.url = url
        self.kind = kind
        self.format = fmt
        self.max_duration = max_duration
        self.directory = directory
        self.created = created
        self.state = "queued"
        self.title: str | None = None
        self.duration: int | None = None
        self.done_bytes = 0
        self.total_bytes: int | None = None
        self.filename: str | None = None
        self.size: int | None = None
        self.content_type: str | None = None
        self.error: dict | None = None
        self.finished: float | None = None
        self.cancel = threading.Event()
        self.settled = False          # the thread is through with it
        self._lock = lock

    # --- what the fetcher reports ---------------------------------------
    def is_cancelled(self) -> bool:
        return self.cancel.is_set()

    def set_state(self, state: str) -> None:
        with self._lock:
            if self.state in ACTIVE:
                self.state = state

    def probed(self, info: dict) -> None:
        duration = info.get("duration")
        with self._lock:
            self.title = info.get("title")
            self.duration = int(round(duration)) if isinstance(duration, (int, float)) else None

    def progress(self, done: int, total: int | None) -> None:
        with self._lock:
            self.done_bytes = int(done)
            self.total_bytes = int(total) if total else None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "state": self.state, "url": self.url, "kind": self.kind, "format": self.format,
            "title": self.title, "duration": self.duration, "done_bytes": self.done_bytes,
            "total_bytes": self.total_bytes, "filename": self.filename, "size": self.size,
            "content_type": self.content_type, "error": self.error,
            "created": iso(self.created), "finished": iso(self.finished),
        }


class JobManager:
    def __init__(self, fetcher: Fetcher, root: Path, max_jobs: int, keep: float, clock=time.time) -> None:
        self.fetcher = fetcher
        self.root = root
        self.max_jobs = max_jobs
        self.keep = keep                  # seconds a finished job stays
        self._clock = clock
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._pool = ThreadPoolExecutor(max_workers=max_jobs, thread_name_prefix="job")
        # Probes for the info endpoint, apart from the jobs
        self.probe_pool = ThreadPoolExecutor(max_workers=max_jobs, thread_name_prefix="probe")

    def start(self) -> None:
        """Clears what an earlier run left behind."""
        self.root.mkdir(parents=True, exist_ok=True)
        old = 0
        for entry in self.root.iterdir():
            _remove(entry)
            old += 1
        if old:
            LOGGER.info("Removed %d leftover job directories", old)

    def create(self, url: str, kind: str, fmt: str, max_duration: int | None) -> Job | None:
        """A new job, or None while max_jobs are busy."""
        with self._lock:
            if sum(1 for job in self._jobs.values() if not job.settled) >= self.max_jobs:
                return None
            job_id = secrets.token_hex(8)
            job = Job(job_id, url, kind, fmt, max_duration, self.root / job_id, self._clock(), self._lock)
            self._jobs[job_id] = job
        LOGGER.info("Job %s: %s as %s", job_id, kind, fmt)
        LOGGER.debug("Job %s: %s", job_id, url)
        self._pool.submit(self._run, job)
        return job

    def snapshot(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.to_dict() if job else None

    def snapshots(self) -> list[dict]:
        with self._lock:
            return [job.to_dict() for job in sorted(self._jobs.values(), key=lambda j: j.created)]

    def file(self, job_id: str) -> tuple[Path, str, str] | None:
        """Path, content type and file name of a done job; None for an
        unknown job, NotReady for one without a file."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.state != "done" or not job.filename:
                raise NotReady(job.state)
            return job.directory / job.filename, job.content_type or "application/octet-stream", job.filename

    def delete(self, job_id: str) -> bool:
        """Cancels a running job (its thread cleans up) or removes a finished
        one with its file. False for an unknown job."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.settled:
                del self._jobs[job_id]
                directory = job.directory
            else:
                directory = None
                job.cancel.set()
                if job.state in ACTIVE:
                    job.state = "cancelled"
                    job.finished = self._clock()
        if directory:
            _remove(directory)
            LOGGER.info("Job %s removed", job_id)
        else:
            LOGGER.info("Job %s cancelled", job_id)
        return True

    def expire(self) -> int:
        """Removes finished jobs older than keep; returns how many."""
        now = self._clock()
        with self._lock:
            gone = [job for job in self._jobs.values()
                    if job.settled and job.finished is not None and now - job.finished >= self.keep]
            for job in gone:
                del self._jobs[job.id]
        for job in gone:
            _remove(job.directory)
        if gone:
            LOGGER.info("Removed %d expired jobs", len(gone))
        return len(gone)

    def shutdown(self) -> None:
        with self._lock:
            for job in self._jobs.values():
                job.cancel.set()
        self._pool.shutdown(wait=False, cancel_futures=True)
        self.probe_pool.shutdown(wait=False, cancel_futures=True)

    # --- the job's thread ------------------------------------------------
    def _run(self, job: Job) -> None:
        started = self._clock()
        try:
            if job.is_cancelled():
                raise DownloadCancelled("cancelled")
            job.set_state("probing")
            path = self.fetcher.download(job.url, job.kind, job.format, job.max_duration,
                                         job.directory, job.id, job)
            size = path.stat().st_size
            with self._lock:
                if job.state == "cancelled":
                    raise DownloadCancelled("cancelled")
                job.filename, job.size, job.content_type = path.name, size, content_type(path)
                job.done_bytes, job.total_bytes = size, size
                job.state, job.finished = "done", self._clock()
            LOGGER.info("Job %s done: %s, %d bytes, %.0f s", job.id, job.content_type, size, self._clock() - started)
        except DownloadCancelled:
            with self._lock:
                job.state = "cancelled"
                job.finished = job.finished or self._clock()
            LOGGER.debug("Job %s stopped after cancelling", job.id)
        except FetchError as err:
            with self._lock:
                job.state, job.finished = "failed", self._clock()
                job.error = {"code": err.code, "message": err.message}
            LOGGER.warning("Job %s failed (%s): %s", job.id, err.code, err.message)
        except Exception as err:  # a bug; the job still has to end
            LOGGER.exception("Job %s crashed", job.id)
            with self._lock:
                job.state, job.finished = "failed", self._clock()
                job.error = {"code": "download_failed", "message": clean_message(str(err) or type(err).__name__)}
        finally:
            with self._lock:
                job.settled = True
                keep_entry = job.state != "cancelled"
                if not keep_entry:
                    self._jobs.pop(job.id, None)
            if job.state != "done":
                _remove(job.directory)


def _remove(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()
    except OSError as err:
        LOGGER.warning("Cannot remove %s: %s", path, err)
