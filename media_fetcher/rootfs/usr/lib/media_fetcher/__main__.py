"""Runs the service: options, the job manager, the API, discovery."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import sys
from pathlib import Path

from aiohttp import web

from . import config as cfg_module
from .api import PORT, create_app
from .fetcher import Fetcher, versions
from .jobs import JobManager
from .supervisor import announce

LOGGER = logging.getLogger("media_fetcher")
JOBS = Path("/data/jobs")
EXPIRE_EVERY = 60


def hostname() -> str:
    """The container's name in Home Assistant's app network."""
    return os.environ.get("HOSTNAME") or socket.gethostname()


async def expire_loop(manager: JobManager) -> None:
    while True:
        await asyncio.sleep(EXPIRE_EVERY)
        try:
            manager.expire()
        except Exception:
            LOGGER.exception("Expiring jobs failed")


async def serve(cfg: cfg_module.Config, manager: JobManager, found: dict, version: str) -> None:
    host = hostname()
    app = create_app(manager, cfg.token, found, version, slug=host.replace("-", "_"),
                     settings={"max_jobs": cfg.max_jobs, "keep_minutes": cfg.keep_minutes,
                               "allow_any_site": cfg.allow_any_site, "token_fixed": cfg.token_fixed})
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    LOGGER.info("API on port %d, token %s", PORT, "from the options" if cfg.token_fixed else "random for this start")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    expirer = asyncio.create_task(expire_loop(manager))
    announcer = asyncio.create_task(announce(host, PORT, cfg.token))
    try:
        await stop.wait()
        LOGGER.info("Stopping")
    finally:
        for task in (expirer, announcer):
            task.cancel()
        manager.shutdown()
        await runner.cleanup()


def main() -> None:
    try:
        cfg = cfg_module.load()
    except cfg_module.ConfigError as err:
        logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s")
        LOGGER.error("%s", err)
        sys.exit(1)
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.INFO)
    version = os.environ.get("APP_VERSION", "unknown")
    LOGGER.info("Media Fetcher %s", version)
    found = versions()
    LOGGER.info("yt-dlp %s, JavaScript runtime %s, ffmpeg %s", found["yt_dlp"],
                found["js_runtime"] or "none", found["ffmpeg"] or "none")
    if not found["js_runtime"]:
        LOGGER.warning("No JavaScript runtime: many sites will refuse or fail")
    if not found["ffmpeg"]:
        LOGGER.warning("No ffmpeg: no conversions and no merged video")
    manager = JobManager(Fetcher(cfg.allow_any_site), JOBS, cfg.max_jobs, cfg.keep_minutes * 60)
    manager.start()
    asyncio.run(serve(cfg, manager, found, version))


if __name__ == "__main__":
    main()
