"""Plays Home Assistant's ingress for the Page check, from 172.30.32.2 in a
throwaway app network, in a second container of the built image (it brings
Python and aiohttp):

- /api/hassio_ingress/<token>/… goes to the app, with the header
  X-Ingress-Path the Supervisor sends, from the ingress proxy's address;
- / is Home Assistant's frame around the page (same origin, like the real
  one), so the page's frame-ancestors and target="_top" links are tested;
  /?lang=de adds a stand-in for the frontend's <home-assistant> element,
  whose hass names that language (and light mode), as the page reads it;
  /?safe=47,34 is the same on a phone with a notch: the safe-area insets top
  and bottom in px that Home Assistant passes on;
- /config/app/<slug>/config stands for the app's settings page, /favicon.ico
  for Home Assistant's icon.

The page at / also plays Home Assistant's part (its app panel, ha-panel-app)
for the page's share/ha-app/ha-frame.js: it answers subscribe-properties with
properties (narrow below 870 px, the insets, the route) and again on every
resize; it shows its 40 px bar #ha-bar above the frame while narrow and not
in kiosk mode, and pads the frame by the insets unless the page handles them
(handleSafeArea). What the page asked for lands in window.__kiosk,
window.__safe and window.__menu (toggle-menu clicks), every message in
window.__messages.

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
# Home Assistant's app panel as far as ha-frame.js talks to it; in the head, so
# it listens before the frame loads
FRAME = """(() => {
  const safe = (new URLSearchParams(location.search).get("safe") || "0,0").split(",");
  const inset = (n) => (parseInt(n, 10) || 0) + "px";
  const insets = { top: inset(safe[0]), right: "0px", bottom: inset(safe[1]), left: "0px" };
  const frame = { get contentWindow() { return document.getElementById("ingress").contentWindow; } };
  Object.assign(window, { __kiosk: null, __safe: null, __menu: 0, __messages: [] });
  let subscribed = false;
  const narrow = () => matchMedia("(max-width: 870px)").matches;
  const layout = () => {
    const bar = document.getElementById("ha-bar"), box = document.getElementById("ha-app");
    if (!bar || !box) return;
    bar.hidden = !(narrow() && !window.__kiosk);
    box.style.paddingTop = window.__safe || !bar.hidden ? "0px" : insets.top;
    box.style.paddingBottom = window.__safe ? "0px" : insets.bottom;
  };
  const send = () => {
    if (subscribed) frame.contentWindow.postMessage({ type: "home-assistant/properties", narrow: narrow(),
      route: { prefix: "/app/demo", path: "" }, safeAreaInsets: insets }, location.origin);
  };
  window.addEventListener("message", (e) => {
    if (e.source !== frame.contentWindow || !e.data) return;
    window.__messages.push(e.data);
    if (e.data.type === "home-assistant/subscribe-properties") {
      subscribed = true;
      window.__kiosk = !!e.data.kioskMode;
      window.__safe = !!e.data.handleSafeArea;
      layout();
      send();
    } else if (e.data.type === "home-assistant/unsubscribe-properties") {
      subscribed = false;
      window.__kiosk = false;
      window.__safe = false;
      layout();
    } else if (e.data.type === "home-assistant/toggle-menu") {
      window.__menu += 1;
    }
  });
  window.addEventListener("resize", () => { layout(); send(); });
  document.addEventListener("DOMContentLoaded", layout);
})();"""
LANG = re.compile(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?")
PASS_BACK = ("Content-Type", "Content-Security-Policy", "Cache-Control", "Content-Disposition",
             "X-Content-Type-Options", "Referrer-Policy")


async def frame(request: web.Request) -> web.Response:
    lang = request.query.get("lang", "")
    hass = HASS.replace("LANG", lang) if LANG.fullmatch(lang) else ""
    return web.Response(content_type="text/html", text=(
        '<!doctype html><html><head><meta charset="utf-8"><title>Home Assistant</title>'
        "<style>html,body{margin:0;height:100%}body{display:flex;flex-direction:column}"
        "#ha-bar{flex:none;height:40px;background:#888}#ha-app{flex:1;min-height:0;box-sizing:border-box}"
        "iframe{border:0;width:100%;height:100%;display:block}</style>"
        f"<script>{FRAME}</script></head><body>{hass}"
        '<div id="ha-bar" hidden></div><div id="ha-app">'
        f'<iframe id="ingress" title="Media Fetcher" src="{PREFIX}/"></iframe></div></body></html>'))


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
