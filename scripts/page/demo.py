"""The app's page with made-up jobs, inside the built image: the image's own
API code, Python, packages and stylesheet, only the jobs are generated here
instead of fetched. scripts/screenshots.py starts it in a throwaway
container for the Page check and the screenshots:

    docker run --hostname local-media-fetcher --user fetcher -e PYTHONPATH=/usr/lib \
        -v scripts/page:/page:ro --entrypoint python3 <image> /page/demo.py

The page answers only the ingress proxy's address (172.30.32.2), as in Home
Assistant; scripts/page/ingress.py plays that proxy. No job fetches anything:
the done jobs have empty files of the size shown, the downloading one stays
where it is, and a job started on the page fails, since the made-up sites
do not exist.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from aiohttp import web
from media_fetcher.api import PORT, create_app
from media_fetcher.fetcher import MB, Fetcher, versions
from media_fetcher.jobs import Job, JobManager, content_type

ROOT = Path("/tmp/jobs")
SETTINGS = {"trust_home_assistant": True, "max_jobs": 2, "keep_minutes": 60, "max_size_mb": 2000,
            "allow_any_site": False, "token_set": False}

# Made up: no such videos or sites. (id, state, title, url, kind, format, minutes ago, more)
JOBS = [
    ("4be0c1d29a7f3e15", "done", "Evening birdsong in the garden", "https://video.example.org/watch/birdsong",
     "audio", "m4a", 9, {"duration": 417, "size": 6_712_334}),
    ("a91f0e6c2b4d7385", "done", "How a bicycle gear works", "https://video.example.org/watch/gears",
     "video", "mp4", 6, {"duration": 262, "size": 48_120_904}),
    ("3c7d52e08f1ab946", "failed", None, "https://video.example.org/watch/members-only",
     "audio", "mp3", 4, {"error": {"code": "not_available",
                                    "message": "Join this channel to get access to members-only content"}}),
    ("e5a6f81b0d3c2947", "downloading", "A walk through the old harbour", "https://video.example.org/watch/harbour",
     "audio", "opus", 2, {"duration": 1312, "done": 9_404_112, "total": 21_870_400}),
]


def made_up(manager: JobManager) -> None:
    now = time.time()
    for job_id, state, title, url, kind, fmt, minutes, more in JOBS:
        job = Job(job_id, url, kind, fmt, None, ROOT / job_id, now - minutes * 60, manager._lock)
        job.state, job.title, job.duration = state, title, more.get("duration")
        job.done_bytes, job.total_bytes = more.get("done", 0), more.get("total")
        if state == "done":
            job.directory.mkdir(parents=True)
            path = job.directory / f"{job_id}.{fmt}"
            with path.open("wb") as f:
                f.truncate(more["size"])  # sparse: the size without the bytes
            job.filename, job.size, job.content_type = path.name, more["size"], content_type(path)
            job.done_bytes = job.total_bytes = more["size"]
        if state in ("done", "failed"):
            job.finished, job.settled, job.error = now - minutes * 60 + 8, True, more.get("error")
        manager._jobs[job_id] = job


async def main() -> None:
    manager = JobManager(Fetcher(False, max_size=2000 * MB), ROOT, 2, 3600)
    manager.start()
    made_up(manager)
    slug = os.environ.get("HOSTNAME", "").replace("-", "_") or None
    app = create_app(manager, "", versions(), os.environ.get("APP_VERSION", "unknown"), slug=slug,
                     settings=SETTINGS)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    print(f"demo page on port {PORT}, slug {slug}", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
