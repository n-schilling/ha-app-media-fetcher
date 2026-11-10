"""Renders the web page with made-up jobs and takes the screenshots in
docs/screenshots with a headless browser.

    uv run --with playwright==1.65.0 python scripts/screenshots.py

The page is rendered by the service's own code (api.render) from jobs made
up here; no link is fetched. The browser is the installed Google Chrome,
else Playwright's Chromium (`uv run --with playwright==1.65.0 playwright
install chromium` once), both headless.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs/screenshots"
sys.path.insert(0, str(ROOT / "media_fetcher/rootfs/usr/lib"))

from media_fetcher import api  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

VERSIONS = {"yt_dlp": "2026.10.26", "js_runtime": "deno 2.9.8", "ffmpeg": "7.1.5-0"}
SETTINGS = {"max_jobs": 2, "keep_minutes": 60, "max_size_mb": 2000, "allow_any_site": False, "token_set": False,
            "trust_home_assistant": True}


def version() -> str:
    for line in (ROOT / "media_fetcher/config.yaml").read_text().splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip().strip('"')
    raise SystemExit("no version in config.yaml")


def job(job_id: str, state: str, title: str | None, url: str, kind: str, fmt: str, created: str, **more) -> dict:
    base = {"id": job_id, "state": state, "url": url, "kind": kind, "format": fmt, "title": title,
            "duration": None, "done_bytes": 0, "total_bytes": None, "filename": None, "size": None,
            "content_type": None, "error": None, "created": created, "finished": None}
    return {**base, **more}


# Made up for these pictures: no such videos or sites
JOBS = [
    job("4be0c1d29a7f3e15", "done", "Evening birdsong in the garden", "https://video.example.org/watch/birdsong",
        "audio", "m4a", "2026-11-10T17:02:11Z", duration=417, done_bytes=6_712_334, total_bytes=6_712_334,
        filename="4be0c1d29a7f3e15.m4a", size=6_712_334, content_type="audio/mp4", finished="2026-11-10T17:02:19Z"),
    job("a91f0e6c2b4d7385", "done", "How a bicycle gear works", "https://video.example.org/watch/gears",
        "video", "mp4", "2026-11-10T17:05:40Z", duration=262, done_bytes=48_120_904, total_bytes=48_120_904,
        filename="a91f0e6c2b4d7385.mp4", size=48_120_904, content_type="video/mp4", finished="2026-11-10T17:06:31Z"),
    job("3c7d52e08f1ab946", "failed", None, "https://video.example.org/watch/members-only",
        "audio", "mp3", "2026-11-10T17:08:02Z", finished="2026-11-10T17:08:04Z",
        error={"code": "not_available", "message": "Join this channel to get access to members-only content"}),
    job("e5a6f81b0d3c2947", "downloading", "A walk through the old harbour", "https://video.example.org/watch/harbour",
        "audio", "opus", "2026-11-10T17:09:15Z", duration=1312, done_bytes=9_404_112, total_bytes=21_870_400),
]


def main() -> None:
    ctx = api.Context(manager=None, token="", versions=VERSIONS, version=version(), page_peers=frozenset(),
                      core_peers=frozenset(), slug="example_media_fetcher", settings=SETTINGS)
    OUT.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="media-fetcher-shots-"))
    try:
        page_file = work / "index.html"
        page_file.write_text(api.render(ctx, JOBS))
        chrome = Path("/Applications/Google Chrome.app").exists() or shutil.which("google-chrome")
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome" if chrome else None)
            for name, width, height, scheme in (("page.png", 1100, 900, "light"),
                                                ("phone-dark.png", 390, 844, "dark")):
                page = browser.new_page(viewport={"width": width, "height": height}, color_scheme=scheme,
                                        device_scale_factor=2 if width < 600 else 1)
                page.goto(page_file.as_uri())
                page.wait_for_timeout(300)
                page.screenshot(path=OUT / name, full_page=True)
                page.close()
                print(f"wrote {OUT / name}")
            browser.close()
    finally:
        shutil.rmtree(work)


if __name__ == "__main__":
    main()
