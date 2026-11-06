"""Puts the package on the path and provides a stand-in for yt_dlp.YoutubeDL."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest
from yt_dlp.utils import DownloadError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "media_fetcher/rootfs/usr/lib"))

from media_fetcher.api import create_app  # noqa: E402
from media_fetcher.fetcher import Fetcher  # noqa: E402
from media_fetcher.jobs import JobManager  # noqa: E402

TOKEN = "test-token-0123456789"
VERSIONS = {"yt_dlp": "2026.10.26", "js_runtime": "deno 2.9.8", "ffmpeg": "7.1.2"}
VIDEO = {
    "_type": "video", "id": "abc123", "title": "A talk", "duration": 417.0, "uploader": "Someone",
    "extractor": "youtube", "webpage_url": "https://www.youtube.com/watch?v=abc123",
    "thumbnail": "https://i.example.test/abc123.jpg", "ext": "webm", "is_live": False,
    "formats": [{"format_id": "251", "ext": "webm", "acodec": "opus", "vcodec": "none"}],
    "requested_formats": None,
}


class Scenario:
    """What the fake yt-dlp does: the info it returns or the error it raises,
    the download it plays (two progress events, a file, a converter) and the
    gate at which it waits, so a test can cancel or fill the job slots."""

    def __init__(self, info: dict | None = None, extract_error: str | None = None,
                 download_error: str | None = None, convert_error: str | None = None,
                 size: int = 1000, ext: str = "m4a", converter: str | None = "ExtractAudio",
                 gate: threading.Event | None = None) -> None:
        self.info = dict(VIDEO) if info is None else info
        self.extract_error = extract_error
        self.download_error = download_error
        self.convert_error = convert_error
        self.size = size
        self.ext = ext
        self.converter = converter
        self.gate = gate
        self.instances: list[FakeYoutubeDL] = []
        self.extracted: list[str] = []
        self.processed = 0
        self.sanitized: bool | None = None

    def ydl_class(self):
        return lambda params: FakeYoutubeDL(params, self)


class FakeYoutubeDL:
    def __init__(self, params: dict, scenario: Scenario) -> None:
        self.params = params
        self.scenario = scenario
        scenario.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url: str, download: bool = True, process: bool = True) -> dict:
        self.scenario.extracted.append(url)
        if self.scenario.extract_error:
            raise DownloadError(self.scenario.extract_error)
        return dict(self.scenario.info)

    def sanitize_info(self, info: dict, remove_private_keys: bool = False) -> dict:
        self.scenario.sanitized = remove_private_keys
        return {k: v for k, v in info.items() if not remove_private_keys or v is not None}

    def process_ie_result(self, info: dict, download: bool = True) -> dict:
        s = self.scenario
        s.processed += 1
        hooks = self.params.get("progress_hooks", [])
        pp_hooks = self.params.get("postprocessor_hooks", [])
        post_hooks = self.params.get("post_hooks", [])
        tmp = Path(self.params["outtmpl"]["default"].replace("%(ext)s", "webm"))

        def fire(**status) -> None:
            for hook in hooks:
                hook({"info_dict": info, "filename": str(tmp), **status})

        fire(status="downloading", downloaded_bytes=0, total_bytes=s.size)
        if s.gate is not None:
            s.gate.wait(5)
        fire(status="downloading", downloaded_bytes=s.size // 2, total_bytes=s.size)
        if s.download_error:
            raise DownloadError(s.download_error)
        tmp.write_bytes(b"x" * s.size)
        fire(status="finished", downloaded_bytes=s.size, total_bytes=s.size)
        final = tmp
        if s.converter:
            for hook in pp_hooks:
                hook({"status": "started", "postprocessor": s.converter, "info_dict": info})
            if s.convert_error:
                raise DownloadError(f"ERROR: Postprocessing: {s.convert_error}")
            final = tmp.with_suffix(f".{s.ext}")
            tmp.rename(final)
            for hook in pp_hooks:
                hook({"status": "finished", "postprocessor": s.converter, "info_dict": info})
        for hook in post_hooks:
            hook(str(final))
        return info


class Clock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def wait_for(manager: JobManager, job_id: str, *states, timeout: float = 5.0) -> dict | None:
    """Polls until the job is in one of the states (None: gone)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = manager.snapshot(job_id)
        if job is None and None in states:
            return None
        if job is not None and job["state"] in states:
            return job
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} not in {states}: {manager.snapshot(job_id)}")


@pytest.fixture
def scenario():
    return Scenario()


@pytest.fixture
def clock():
    return Clock()


ADDRESSES = {"www.youtube.com": "142.250.74.206", "x.test": "93.184.216.34", "nas.lan": "192.168.1.20",
             "localhost": "127.0.0.1", "supervisor": "172.30.32.2", "v6.test": "2606:2800:220:1:248:1893:25c8:1946",
             "mapped.test": "::ffff:10.0.0.9", "example.com": "93.184.216.34"}


def resolve(hostname, port):
    """Stands in for socket.getaddrinfo with a fixed address book."""
    import socket
    try:
        address = ADDRESSES[hostname] if hostname in ADDRESSES else str(__import__("ipaddress").ip_address(hostname))
    except ValueError:
        raise socket.gaierror(f"{hostname} unknown") from None
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (address, 0))]


@pytest.fixture
def fetcher(scenario):
    return Fetcher(False, ydl_class=scenario.ydl_class(), resolve=resolve)


@pytest.fixture
def manager(tmp_path, fetcher, clock):
    m = JobManager(fetcher, tmp_path / "jobs", max_jobs=2, keep=3600, clock=clock)
    m.start()
    yield m
    m.shutdown()


@pytest.fixture
def app(manager):
    return create_app(manager, TOKEN, VERSIONS, "0.1.0", peers=frozenset({"127.0.0.1"}),
                      slug="local_media_fetcher", settings={"max_jobs": 2, "keep_minutes": 60,
                                                            "allow_any_site": False, "token_fixed": True})


@pytest.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


def bearer(token: str = TOKEN) -> dict:
    return {"Authorization": f"Bearer {token}"}
