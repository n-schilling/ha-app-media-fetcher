"""The Supervisor's discovery: tells Home Assistant where the service is
and which token it takes."""

from __future__ import annotations

import asyncio
import logging
import os

import aiohttp

LOGGER = logging.getLogger("media_fetcher.supervisor")
SERVICE = "media_fetcher"
ATTEMPTS = 5
PAUSE = 5


async def announce(host: str, port: int, token: str, service: str = SERVICE,
                   url: str = "http://supervisor/discovery") -> bool:
    """Posts {"service", "config": {"host", "port", "token"}} to the
    Supervisor, which hands it to Home Assistant. False outside the
    Supervisor or when it does not take it; the token is never logged."""
    supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
    if not supervisor_token:
        LOGGER.info("No Supervisor, no discovery")
        return False
    payload = {"service": service, "config": {"host": host, "port": port, "token": token}}
    headers = {"Authorization": f"Bearer {supervisor_token}"}
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        for attempt in range(1, ATTEMPTS + 1):
            try:
                async with session.post(url, json=payload) as resp:
                    body = await resp.json(content_type=None)
                    if resp.status == 200 and body.get("result") == "ok":
                        LOGGER.info("Discovery sent: service %s at %s:%d", service, host, port)
                        return True
                    reason = f"{resp.status} {body.get('message') or ''}".strip()
            except (TimeoutError, aiohttp.ClientError, ValueError, AttributeError) as err:
                reason = str(err) or type(err).__name__
            if attempt < ATTEMPTS:
                LOGGER.debug("Discovery attempt %d failed (%s), again in %d s", attempt, reason, PAUSE)
                await asyncio.sleep(PAUSE)
    LOGGER.warning("Discovery failed (%s); clients need the token from the options", reason)
    return False
