"""JobRunner: output capture, forward-progress polling, exit-code surfacing, and the
single-lifecycle lock. (Uses a trivial in-test `sh -c` fixture, not user input.)"""

from __future__ import annotations

import pytest

from docmcp.console.runner import JobBusy, JobRunner


def test_run_sync_captures_output_and_rc():
    r = JobRunner()
    rc, out = r.run_sync(["sh", "-c", "echo hello; echo world; exit 3"])
    assert rc == 3
    assert "hello" in out and "world" in out


def test_run_sync_timeout():
    r = JobRunner()
    rc, out = r.run_sync(["sh", "-c", "sleep 5"], timeout=0.3)
    assert rc == 124 and "timed out" in out


def test_job_streams_lines_and_finishes():
    r = JobRunner()
    job = r.start("echo", ["sh", "-c", "echo a; echo b; echo c"])
    assert job.wait(5)
    cursor, lines = job.tail(0)
    assert lines == ["a", "b", "c"] and cursor == 3
    assert job.status == "done" and job.exit_code == 0
    # tail(after) makes forward progress
    cursor2, lines2 = job.tail(cursor)
    assert lines2 == [] and cursor2 == 3


def test_failed_job_status():
    r = JobRunner()
    job = r.start("fail", ["sh", "-c", "exit 2"])
    assert job.wait(5)
    assert job.status == "failed" and job.exit_code == 2


def test_lifecycle_lock_serializes():
    r = JobRunner()
    j1 = r.start("slow", ["sh", "-c", "sleep 0.5"], lifecycle=True)
    # a second lifecycle job while the first holds the lock is refused
    with pytest.raises(JobBusy):
        r.start("second", ["sh", "-c", "true"], lifecycle=True)
    assert j1.wait(5)
    # once the first finishes the lock is free again
    j2 = r.start("third", ["sh", "-c", "true"], lifecycle=True)
    assert j2.wait(5) and j2.status == "done"


def test_nonlifecycle_jobs_not_locked():
    r = JobRunner()
    a = r.start("a", ["sh", "-c", "true"])
    b = r.start("b", ["sh", "-c", "true"])  # no JobBusy for non-lifecycle
    assert a.wait(5) and b.wait(5)
