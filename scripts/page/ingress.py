"""Plays Home Assistant's ingress for the Page check, from 172.30.32.2 in a
throwaway app network, in a second container of the built image (it brings
Python and aiohttp):

- /api/hassio_ingress/<token>/… goes to the app, with the header
  X-Ingress-Path the Supervisor sends, from the ingress proxy's address;
- / is Home Assistant's frame around the page (same origin, like the real
  one), so the page's frame-ancestors and target="_top" links are tested;
  /?lang=de adds a stand-in for the frontend's <home-assistant> element,
  whose hass names that language (and light mode), as the page reads it;
- /config/app/<slug>/config stands for the app's settings page, /favicon.ico
  for Home Assistant's icon.

    APP=http://<app address>:8099 python3 /page/ingress.py
"""

from __future__ import annotations

import html
import os
import re

import aiohttp
from aiohttp import web

APP = os.environ["APP"]
PREFIX = "/api/hassio_ingress/page-check-token"
# Home Assistant's frontend as far as the page reads it: the user's language
HASS = """<home-assistant></home-assistant><script>document.querySelector("home-assistant").hass =
{ language: "LANG", locale: { language: "LANG" }, themes: { darkMode: false } };</script>"""
LANG = re.compile(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?")
PASS_BACK = ("Content-Type", "Content-Security-Policy", "Cache-Control", "Content-Disposition",
             "X-Content-Type-Options")


async def frame(request: web.Request) -> web.Response:
    lang = request.query.get("lang", "")
    hass = HASS.replace("LANG", lang) if LANG.fullmatch(lang) else ""
    return web.Response(content_type="text/html", text=(
        '<!doctype html><html><head><meta charset="utf-8"><title>Home Assistant</title>'
        "<style>html,body{margin:0;height:100%}iframe{border:0;width:100%;height:100%;display:block}</style>"
        f'</head><body>{hass}<iframe title="Media Fetcher" src="{PREFIX}/"></iframe></body></html>'))


async def favicon(request: web.Request) -> web.Response:
    """Home Assistant has one; the browser asks for it."""
    return web.Response(content_type="image/svg+xml",
                        text='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"><rect width="1" height="1"/></svg>')


async def settings(request: web.Request) -> web.Response:
    slug = html.escape(request.match_info["slug"])
    return web.Response(content_type="text/html",
                        text=f"<!doctype html><title>Settings</title><h1>Settings of {slug}</h1>")


async def forward(request: web.Request) -> web.Response:
    headers = {name: value for name, value in request.headers.items()
               if name.lower() in ("accept", "accept-language", "content-type", "cookie", "x-requested-with", "range")}
    headers["X-Ingress-Path"] = PREFIX
    url = APP + "/" + request.match_info["tail"] + (f"?{request.query_string}" if request.query_string else "")
    async with request.app["session"].request(request.method, url, headers=headers, data=await request.read(),
                                              allow_redirects=False) as resp:
        body = await resp.read()
        back = {name: resp.headers[name] for name in PASS_BACK if name in resp.headers}
        return web.Response(status=resp.status, body=body, headers=back)


async def session(app: web.Application):
    app["session"] = aiohttp.ClientSession()
    yield
    await app["session"].close()


def main() -> None:
    app = web.Application(client_max_size=1024 * 1024)
    app.cleanup_ctx.append(session)
    app.router.add_get("/", frame)
    app.router.add_get("/favicon.ico", favicon)
    app.router.add_get("/config/app/{slug}/config", settings)
    app.router.add_route("*", PREFIX + "/{tail:.*}", forward)
    web.run_app(app, host="0.0.0.0", port=8080, access_log=None, print=lambda *_: print("ingress on 8080", flush=True))


if __name__ == "__main__":
    main()
