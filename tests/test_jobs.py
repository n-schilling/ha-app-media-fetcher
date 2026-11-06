"""The job manager: threads, states, files, cancelling, cleanup."""

import threading

import pytest
from conftest import Scenario, resolve, wait_for

from media_fetcher.fetcher import Fetcher
from media_fetcher.jobs import JobManager, NotReady, content_type, iso


def test_a_job_runs_to_done(manager, scenario):
    job = manager.create("https://x.test/v", "audio", "m4a", None)
    assert job.state in ("queued", "probing", "downloading", "converting", "done")
    done = wait_for(manager, job.id, "done")
    assert done["filename"] == f"{job.id}.m4a" and done["size"] == 1000 and done["content_type"] == "audio/mp4"
    assert done["title"] == "A talk" and done["duration"] == 417 and done["error"] is None
    assert done["done_bytes"] == 1000 and done["total_bytes"] == 1000
    assert done["created"] == "2023-11-14T22:13:20Z" and done["finished"] == done["created"]
    path, kind, name = manager.file(job.id)
    assert path.read_bytes() == b"x" * 1000 and kind == "audio/mp4" and name == f"{job.id}.m4a"
    assert manager.snapshots() == [done]


def test_too_long_fails_before_the_download(manager, scenario, tmp_path):
    job = manager.create("https://x.test/v", "audio", "m4a", 60)
    failed = wait_for(manager, job.id, "failed")
    assert failed["error"]["code"] == "too_long" and scenario.processed == 0
    assert not (tmp_path / "jobs" / job.id).exists()
    with pytest.raises(NotReady, match="failed"):
        manager.file(job.id)


def test_a_failed_download_keeps_no_files(tmp_path, clock):
    scenario = Scenario(download_error="ERROR: unable to download video data: HTTP Error 403: Forbidden")
    manager = JobManager(Fetcher(False, scenario.ydl_class(), resolve=resolve), tmp_path / "jobs", 2, 3600, clock)
    manager.start()
    job = manager.create("https://x.test/v", "audio", "m4a", None)
    failed = wait_for(manager, job.id, "failed")
    assert failed["error"] == {"code": "download_failed",
                               "message": "unable to download video data: HTTP Error 403: Forbidden"}
    assert not (tmp_path / "jobs" / job.id).exists()
    manager.shutdown()


def test_cancelling_a_running_job(tmp_path, clock):
    gate = threading.Event()
    scenario = Scenario(gate=gate)
    manager = JobManager(Fetcher(False, scenario.ydl_class(), resolve=resolve), tmp_path / "jobs", 2, 3600, clock)
    manager.start()
    job = manager.create("https://x.test/v", "audio", "m4a", None)
    wait_for(manager, job.id, "downloading")
    with pytest.raises(NotReady, match="downloading"):
        manager.file(job.id)
    assert manager.delete(job.id) is True
    assert manager.snapshot(job.id)["state"] == "cancelled"
    gate.set()
    assert wait_for(manager, job.id, None) is None
    assert not (tmp_path / "jobs" / job.id).exists()
    assert scenario.processed == 1
    manager.shutdown()


def test_busy_while_max_jobs_run(tmp_path, clock):
    gate = threading.Event()
    scenario = Scenario(gate=gate)
    manager = JobManager(Fetcher(False, scenario.ydl_class(), resolve=resolve), tmp_path / "jobs", 1, 3600, clock)
    manager.start()
    first = manager.create("https://x.test/v", "audio", "m4a", None)
    assert manager.create("https://x.test/w", "audio", "m4a", None) is None
    gate.set()
    wait_for(manager, first.id, "done")
    assert manager.create("https://x.test/w", "audio", "m4a", None) is not None
    manager.shutdown()


def test_finished_jobs_expire(manager, clock, tmp_path):
    job = manager.create("https://x.test/v", "audio", "m4a", None)
    wait_for(manager, job.id, "done")
    clock.now += 3599
    assert manager.expire() == 0 and manager.snapshot(job.id) is not None
    clock.now += 1
    assert manager.expire() == 1 and manager.snapshot(job.id) is None
    assert not (tmp_path / "jobs" / job.id).exists()


def test_deleting_a_finished_job_removes_its_file(manager, tmp_path):
    job = manager.create("https://x.test/v", "audio", "m4a", None)
    wait_for(manager, job.id, "done")
    assert (tmp_path / "jobs" / job.id / f"{job.id}.m4a").exists()
    assert manager.delete(job.id) is True
    assert manager.snapshot(job.id) is None and not (tmp_path / "jobs" / job.id).exists()
    assert manager.delete(job.id) is False and manager.file("nope") is None


def test_start_clears_leftovers(tmp_path, fetcher):
    old = tmp_path / "jobs" / "old"
    old.mkdir(parents=True)
    (old / "old.m4a").write_bytes(b"x")
    (tmp_path / "jobs" / "stray").write_bytes(b"y")
    manager = JobManager(fetcher, tmp_path / "jobs", 2, 3600)
    manager.start()
    assert list((tmp_path / "jobs").iterdir()) == []
    manager.shutdown()


def test_shutdown_cancels_running_jobs(tmp_path, clock):
    gate = threading.Event()
    scenario = Scenario(gate=gate)
    manager = JobManager(Fetcher(False, scenario.ydl_class(), resolve=resolve), tmp_path / "jobs", 2, 3600, clock)
    manager.start()
    job = manager.create("https://x.test/v", "audio", "m4a", None)
    wait_for(manager, job.id, "downloading")
    manager.shutdown()
    gate.set()
    assert wait_for(manager, job.id, None) is None


def test_content_types_and_times(tmp_path):
    assert content_type(tmp_path / "a.m4a") == "audio/mp4"
    assert content_type(tmp_path / "a.MKV") == "video/x-matroska"
    assert content_type(tmp_path / "a.nothing") == "application/octet-stream"
    assert iso(None) is None and iso(0) is None and iso(1_700_000_000) == "2023-11-14T22:13:20Z"
