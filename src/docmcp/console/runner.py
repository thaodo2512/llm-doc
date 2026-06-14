"""Subprocess execution for the console.

``run_sync`` is for fast read-only verbs (status/doctor/inventory) — it blocks with a
timeout and returns the combined output. ``start`` launches a long op (build/ingest/
serve) on a worker thread, capturing output into a per-job ring buffer that the SPA
tails by polling (``GET …/log?after=N``) or over SSE. Job state lives here, not in any
request, so the browser can navigate away and re-attach to a running job.

Everything is spawned with ``shell=False`` from an argv list built by ``commands.py``.
"""

from __future__ import annotations

import subprocess
import threading
import time
import uuid

_MAX_LINES = 20000  # ring-buffer cap per job; older lines are dropped (and counted)


class Job:
    """A running or finished subprocess and its captured output."""

    def __init__(self, label: str, argv: list[str]):
        self.id = uuid.uuid4().hex[:16]
        self.label = label
        self.argv = argv
        self.status = "running"  # running | done | failed
        self.exit_code: int | None = None
        self.created_at = int(time.time())
        self._lock = threading.Lock()
        self._lines: list[str] = []  # ring buffer (capped)
        self._dropped = 0  # lines evicted from the front of the ring
        self._done = threading.Event()

    # -- writer side (worker thread) -----------------------------------------
    def append(self, line: str) -> None:
        with self._lock:
            self._lines.append(line)
            if len(self._lines) > _MAX_LINES:
                self._dropped += len(self._lines) - _MAX_LINES
                del self._lines[: len(self._lines) - _MAX_LINES]

    def finish(self, exit_code: int) -> None:
        with self._lock:
            self.exit_code = exit_code
            self.status = "done" if exit_code == 0 else "failed"
        self._done.set()

    # -- reader side (request handlers) --------------------------------------
    def tail(self, after: int = 0) -> tuple[int, list[str]]:
        """Return ``(next_cursor, lines)`` for lines with absolute index ≥ ``after``.

        The cursor is an absolute count across the job's whole life (it survives ring
        eviction), so a client always makes forward progress even if old lines dropped.
        """
        with self._lock:
            total = self._dropped + len(self._lines)
            start = max(after, self._dropped)
            offset = start - self._dropped
            return total, list(self._lines[offset:])

    @property
    def done(self) -> bool:
        return self._done.is_set()

    def wait(self, timeout: float) -> bool:
        return self._done.wait(timeout)

    def to_dict(self) -> dict:
        with self._lock:
            return {
                "id": self.id,
                "label": self.label,
                "status": self.status,
                "exit_code": self.exit_code,
                "created_at": self.created_at,
                "dropped": self._dropped,
            }


class JobBusy(RuntimeError):
    """A lifecycle job is already running (only one at a time)."""


class JobRunner:
    def __init__(self, cwd: str | None = None):
        self._cwd = cwd
        self._jobs: dict[str, Job] = {}
        self._reg_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()  # one build/ingest/serve at a time

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def run_sync(self, argv: list[str], *, env: dict | None = None, timeout: float = 120) -> tuple[int, str]:
        """Run a fast verb to completion. Returns ``(exit_code, combined_output)``.
        A timeout returns exit code 124 with a note (so reads never hang a request)."""
        try:
            proc = subprocess.run(
                argv,
                cwd=self._cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
                check=False,
            )
            return proc.returncode, proc.stdout or ""
        except subprocess.TimeoutExpired as exc:
            partial = exc.stdout or ""
            if isinstance(partial, bytes):
                partial = partial.decode("utf-8", "replace")
            return 124, partial + f"\n[timed out after {timeout}s]\n"
        except OSError as exc:
            return 127, f"failed to run command: {exc}\n"

    def start(self, label: str, argv: list[str], *, env: dict | None = None, lifecycle: bool = False) -> Job:
        """Launch a long op on a worker thread and return its :class:`Job` immediately.

        ``lifecycle=True`` ops (build/ingest/serve/stop) serialize on a single lock — a
        second one while one is running raises :class:`JobBusy`."""
        if lifecycle and not self._lifecycle_lock.acquire(blocking=False):
            raise JobBusy("another build/ingest/serve job is already running")
        job = Job(label, argv)
        with self._reg_lock:
            self._jobs[job.id] = job
        thread = threading.Thread(
            target=self._run, args=(job, argv, env, lifecycle), name=f"job-{job.id}", daemon=True
        )
        thread.start()
        return job

    def _run(self, job: Job, argv: list[str], env: dict | None, lifecycle: bool) -> None:
        try:
            try:
                proc = subprocess.Popen(
                    argv,
                    cwd=self._cwd,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
            except OSError as exc:
                job.append(f"failed to start: {exc}")
                job.finish(127)
                return
            assert proc.stdout is not None
            for line in proc.stdout:
                job.append(line.rstrip("\n"))
            rc = proc.wait()
            job.finish(rc)
        finally:
            if lifecycle:
                self._lifecycle_lock.release()
