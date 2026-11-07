"""The HTTP API under /api/v1 and a status page for Home Assistant's ingress.

Every endpoint but health and the OpenAPI definition needs "Authorization:
Bearer <token>", unless the request comes from Home Assistant Core, which the
app knows by its address in the app network; without a token in the options
nobody else gets in. Errors are JSON: {"error": {"code": "...", "message": "..."}}.

The page at / is HTML for Home Assistant's ingress and answers only the
ingress proxy, so only administrators see it. It shows versions, settings
and the jobs, never the token, and has a form that starts a job and a
download link for every finished one. Its own endpoints (/jobs, /jobs/{id},
/jobs/{id}/file) are the API's without the token: the ingress session is
the lock, and the writing ones take JSON with an X-Requested-With header
only, which a form on another site cannot send.
"""

from __future__ import annotations

import asyncio
import hmac
import html
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from .fetcher import KINDS, FetchError
from .jobs import JobManager, NotReady

PORT = 8099
API = "/api/v1"
HEALTH = f"{API}/health"
OPENAPI = Path(__file__).with_name("openapi.yaml")
OPENAPI_PATH = f"{API}/openapi.yaml"
INGRESS_PEER = "172.30.32.2"       # the Supervisor's ingress proxy
CORE_PEER = "172.30.32.1"          # Home Assistant Core: the host's side of the app network
INGRESS_HEADER = "X-Ingress-Path"  # the Supervisor's path prefix for the page's links
PAGE_HEADER = "X-Requested-With"   # on the page's own POST and DELETE
INGRESS_PATH = re.compile(r"/[A-Za-z0-9_\-/]*")
FILENAME = re.compile(r"[^\w\-. ]+")
URL_LENGTH = 2048
BODY_LENGTH = 64 * 1024
DEFAULT_FORMAT = {"audio": "m4a", "video": "mp4"}
STATUS_CODES = {400: "bad_request", 404: "not_found", 405: "method_not_allowed", 413: "too_large",
                415: "unsupported_media_type"}
CONTROL = re.compile(r"[\x00-\x20\x7f]")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass
class Context:
    manager: JobManager
    token: str                           # for clients other than Home Assistant Core; "" when none
    versions: dict                       # yt_dlp, js_runtime, ffmpeg
    version: str                         # the app's
    page_peers: frozenset[str]           # who may see the status page: the ingress proxy
    core_peers: frozenset[str]           # who needs no token: Home Assistant Core
    slug: str | None = None              # for the link to the app's settings
    settings: dict = field(default_factory=dict)


def error(status: int, code: str, message: str, **headers: str) -> web.Response:
    return web.json_response({"error": {"code": code, "message": message}}, status=status, headers=headers)


@web.middleware
async def errors(request: web.Request, handler) -> web.StreamResponse:
    try:
        return await handler(request)
    except ApiError as err:
        return error(err.status, err.code, err.message)
    except web.HTTPException as err:
        if err.status >= 400 and (request.path.startswith(API) or request.path.startswith("/jobs")):
            return error(err.status, STATUS_CODES.get(err.status, "error"), err.reason or "error")
        raise


def bearer_ok(request: web.Request, token: str) -> bool:
    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    return bool(token) and scheme.lower() == "bearer" and hmac.compare_digest(value.strip(), token)


@web.middleware
async def auth(request: web.Request, handler) -> web.StreamResponse:
    """Home Assistant Core is known by the connection's own address (never a
    forwarded header); everyone else needs the token from the options."""
    if request.path.startswith(API) and request.path not in (HEALTH, OPENAPI_PATH):
        ctx: Context = request.app["ctx"]
        if request.remote not in ctx.core_peers and not bearer_ok(request, ctx.token):
            message = ("a valid bearer token is required" if ctx.token
                       else "no token is set: only Home Assistant may call the API")
            return error(401, "unauthorized", message, **{"WWW-Authenticate": "Bearer"})
    return await handler(request)


# --- requests --------------------------------------------------------------------------------
async def json_body(request: web.Request) -> dict:
    if request.content_length and request.content_length > BODY_LENGTH:
        raise ApiError(413, "too_large", "the request body is too large")
    try:
        data = json.loads(await request.text() or "null")
    except ValueError as err:
        raise ApiError(400, "invalid_json", f"the body is no JSON: {err}") from err
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_json", "the body must be a JSON object")
    return data


def valid_url(value) -> str:
    if not isinstance(value, str) or not value or len(value) > URL_LENGTH or CONTROL.search(value):
        raise ApiError(400, "invalid_url", "url must be an http or https address")
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
    except ValueError as err:
        raise ApiError(400, "invalid_url", f"url is malformed: {err}") from err
    if parts.scheme not in ("http", "https") or not hostname:
        raise ApiError(400, "invalid_url", "url must be an http or https address")
    return value


def job_request(data: dict) -> tuple[str, str, str, int | None]:
    url = valid_url(data.get("url"))
    kind = data.get("kind")
    if kind not in KINDS:
        raise ApiError(400, "invalid_request", "kind must be audio or video")
    fmt = data.get("format", DEFAULT_FORMAT[kind])
    if fmt not in KINDS[kind]:
        raise ApiError(400, "invalid_request", f"format for {kind} must be one of {', '.join(KINDS[kind])}")
    max_duration = data.get("max_duration")
    if max_duration is not None and (isinstance(max_duration, bool) or not isinstance(max_duration, int)
                                     or max_duration <= 0):
        raise ApiError(400, "invalid_request", "max_duration must be a whole number of seconds above 0")
    return url, kind, fmt, max_duration


# --- handlers --------------------------------------------------------------------------------
async def health(request: web.Request) -> web.Response:
    ctx: Context = request.app["ctx"]
    return web.json_response({"ok": True, "version": ctx.version, **ctx.versions})


async def info(request: web.Request) -> web.Response:
    ctx: Context = request.app["ctx"]
    url = valid_url((await json_body(request)).get("url"))
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(ctx.manager.probe_pool, ctx.manager.fetcher.info, url)
    except FetchError as err:
        raise ApiError(400 if err.code == "invalid_url" else 422, err.code, err.message) from err
    return web.json_response(result)


async def start_job(request: web.Request) -> dict:
    ctx: Context = request.app["ctx"]
    url, kind, fmt, max_duration = job_request(await json_body(request))
    try:
        # Resolves the host, so not in the event loop
        await asyncio.get_running_loop().run_in_executor(ctx.manager.probe_pool, ctx.manager.fetcher.check, url)
    except FetchError as err:
        raise ApiError(400 if err.code == "invalid_url" else 422, err.code, err.message) from err
    job = ctx.manager.create(url, kind, fmt, max_duration)
    if job is None:
        raise ApiError(429, "busy", f"{ctx.manager.max_jobs} jobs are running, try again later")
    return {"id": job.id, "state": "queued"}


async def create_job(request: web.Request) -> web.Response:
    return web.json_response(await start_job(request), status=201)


async def list_jobs(request: web.Request) -> web.Response:
    return web.json_response(request.app["ctx"].manager.snapshots())


async def get_job(request: web.Request) -> web.Response:
    job = request.app["ctx"].manager.snapshot(request.match_info["id"])
    if job is None:
        raise ApiError(404, "not_found", "no such job")
    return web.json_response(job)


def file_response(request: web.Request, by_title: bool = False) -> web.StreamResponse:
    """The file of a done job, named after its id, or after its title for
    the page's download link."""
    manager: JobManager = request.app["ctx"].manager
    job_id = request.match_info["id"]
    try:
        found = manager.file(job_id)
    except NotReady as state:
        raise ApiError(409, "not_ready", f"the job is {state}") from None
    if found is None:
        raise ApiError(404, "not_found", "no such job")
    path, content_type, name = found
    if not path.is_file():
        raise ApiError(404, "not_found", "the file is gone")
    if by_title:
        job = manager.snapshot(job_id) or {}
        title = FILENAME.sub(" ", job.get("title") or "").strip()[:80].strip(" .")
        name = f"{title}{path.suffix}" if title else name
    return web.FileResponse(path, headers={"Content-Type": content_type,
                                           "Content-Disposition": f'attachment; filename="{name}"'})


async def get_file(request: web.Request) -> web.StreamResponse:
    return file_response(request)


async def openapi(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(OPENAPI, headers={"Content-Type": "application/yaml; charset=utf-8",
                                              "Cache-Control": "no-store"})


async def delete_job(request: web.Request) -> web.Response:
    if not request.app["ctx"].manager.delete(request.match_info["id"]):
        raise ApiError(404, "not_found", "no such job")
    return web.Response(status=204)


# --- the page's own endpoints: the ingress session is the lock ----------------------------------
def page_only(request: web.Request, writing: bool = False) -> None:
    if request.remote not in request.app["ctx"].page_peers:
        raise ApiError(403, "forbidden", "only through Home Assistant")
    if writing and not request.headers.get(PAGE_HEADER):
        raise ApiError(400, "bad_request", f"the page sends {PAGE_HEADER}")


async def page_create_job(request: web.Request) -> web.Response:
    page_only(request, writing=True)
    return web.json_response(await start_job(request), status=201)


async def page_delete_job(request: web.Request) -> web.Response:
    page_only(request, writing=True)
    return await delete_job(request)


async def page_file(request: web.Request) -> web.StreamResponse:
    page_only(request)
    return file_response(request, by_title=True)


# --- the status page -----------------------------------------------------------------------------
STYLE = """
:root { color-scheme: light dark; --primary-background-color: #fafafa; --card-background-color: #fff;
  --primary-text-color: #212121; --secondary-text-color: #727272; --divider-color: rgba(0,0,0,.12);
  --primary-color: #03a9f4; --success-color: #4caf50; --warning-color: #ff9800; --error-color: #db4437;
  --sidebar-background-color: #fff; --sidebar-text-color: #212121; --sidebar-icon-color: rgba(0,0,0,.5);
  --app-header-border-bottom: 1px solid var(--divider-color); --ha-card-border-radius: 12px;
  --ha-card-border-width: 1px; --ha-card-border-color: var(--divider-color); --ha-card-box-shadow: none;
  --ha-font-family-body: Roboto, Noto, sans-serif; --header-height: 56px; }
@media (prefers-color-scheme: dark) { :root { --primary-background-color: #111; --card-background-color: #1c1c1c;
  --primary-text-color: #e1e1e1; --secondary-text-color: #9b9b9b; --divider-color: rgba(225,225,225,.12);
  --sidebar-background-color: #1c1c1c; --sidebar-text-color: #e1e1e1; --sidebar-icon-color: rgba(255,255,255,.5); } }
* { box-sizing: border-box; }
html, body { margin: 0; background: var(--primary-background-color); color: var(--primary-text-color);
  font: 14px/1.6 var(--ha-font-family-body); -webkit-font-smoothing: antialiased; }
svg { width: 24px; height: 24px; fill: currentColor; flex: none; }
header { position: sticky; top: 0; z-index: 2; display: flex; align-items: center; height: var(--header-height);
  padding: 0 12px; background: var(--sidebar-background-color); color: var(--sidebar-text-color);
  border-bottom: var(--app-header-border-bottom); }
header h1 { margin: 0 0 0 12px; font-size: 20px; font-weight: 400; line-height: 32px; }
header .actions { margin-left: auto; display: flex; align-items: center; }
header .actions a { display: flex; align-items: center; justify-content: center; width: 48px; height: 48px;
  border-radius: 50%; color: var(--sidebar-icon-color); }
header .actions a:hover { background: color-mix(in srgb, var(--primary-text-color) 6%, transparent); }
main { margin: 0 auto; padding: 16px; max-width: 1040px; }
.card { background: var(--card-background-color); border-radius: var(--ha-card-border-radius);
  border: var(--ha-card-border-width) solid var(--ha-card-border-color); box-shadow: var(--ha-card-box-shadow);
  margin-bottom: 16px; overflow: hidden; }
.card h2 { margin: 0; padding: 24px 16px 16px; font-size: 24px; font-weight: 400; line-height: 32px; }
.card .content { padding: 0 16px 16px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 420px), 1fr)); gap: 0 16px; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 8px 16px 8px 0; vertical-align: top; }
td { border-top: 1px solid var(--divider-color); }
th { font-weight: 500; font-size: 12px; white-space: nowrap; }
table.kv th { width: 45%; font-size: 14px; font-weight: 400; color: var(--secondary-text-color);
  border-top: 1px solid var(--divider-color); }
table.kv tr:first-child th, table.kv tr:first-child td { border-top: 0; }
.list th:first-child, .list td:first-child { padding-left: 16px; }
.list th { height: 56px; vertical-align: middle; border-bottom: 1px solid var(--divider-color); }
.list td { height: 52px; vertical-align: middle; font-size: 13px; }
.list tr:first-child td { border-top: 0; }
.ok { color: var(--success-color); } .bad { color: var(--error-color); } .busy { color: var(--primary-color); }
.muted { color: var(--secondary-text-color); } .nowrap { white-space: nowrap; }
.sub { display: block; font-size: 12px; line-height: 16px; color: var(--secondary-text-color); }
.scroll { overflow-x: auto; }
form { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
input, select, button { font: inherit; color: inherit; background: var(--primary-background-color);
  border: 1px solid var(--divider-color); border-radius: 4px; padding: 8px 12px; }
input[type=url] { flex: 1 1 320px; min-width: 0; }
button { cursor: pointer; }
button.primary { background: var(--primary-color); color: #fff; border-color: var(--primary-color); font-weight: 500; }
button.small { padding: 4px 10px; font-size: 12px; }
a.download { color: var(--primary-color); font-weight: 500; text-decoration: none; }
#message { margin: 8px 0 0; min-height: 1.6em; }
#message.bad { color: var(--error-color); }
"""
# Takes Home Assistant's theme from the page around the ingress frame (same
# origin) and swaps in a fresh copy of the page every REFRESH seconds
THEME_VARS = ("primary-background-color", "card-background-color", "primary-text-color", "secondary-text-color",
              "divider-color", "primary-color", "success-color", "warning-color", "error-color",
              "sidebar-background-color", "sidebar-text-color", "sidebar-icon-color", "app-header-border-bottom",
              "ha-card-border-radius", "ha-card-border-width", "ha-card-border-color", "ha-card-box-shadow",
              "ha-font-family-body", "header-height")
REFRESH = 10
SCRIPT = """(() => {
  try {
    const outer = window.parent.document.documentElement;
    if (outer !== document.documentElement) {
      const take = () => {
        const theme = getComputedStyle(outer), own = document.documentElement.style;
        for (const n of NAMES) { const v = theme.getPropertyValue("--" + n).trim(); if (v) own.setProperty("--" + n, v); }
        const ha = window.parent.document.querySelector("home-assistant");
        if (ha && ha.hass) own.colorScheme = ha.hass.themes.darkMode ? "dark" : "light";
      };
      take();
      new MutationObserver(take).observe(outer, { attributes: true, attributeFilter: ["style", "class"] });
    }
  } catch (e) {}
  const update = async () => {
    try {
      const page = await fetch(location.href, { cache: "no-store" });
      if (!page.ok) return;
      const doc = new DOMParser().parseFromString(await page.text(), "text/html");
      document.querySelector("main").replaceWith(doc.querySelector("main"));
    } catch (e) {}
  };
  setInterval(update, REFRESH * 1000);
  // The form and the buttons talk to the page's own endpoints, under the
  // ingress path the Supervisor tells us
  const base = document.body.dataset.base || "";
  const headers = { "Content-Type": "application/json", "X-Requested-With": "media-fetcher" };
  const say = (text, bad) => {
    const m = document.querySelector("#message");
    m.textContent = text;
    m.className = bad ? "bad" : "muted";
  };
  document.querySelector("#fetch").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target, [kind, format] = form.target.value.split("/");
    const body = { url: form.url.value.trim(), kind, format };
    say("Starting …");
    try {
      const resp = await fetch(base + "/jobs", { method: "POST", headers, body: JSON.stringify(body) });
      const data = await resp.json();
      if (!resp.ok) { say((data.error && data.error.message) || resp.statusText, true); return; }
      say("Job " + data.id + " started; the download link appears below when it is done.");
      form.url.value = "";
      update();
    } catch (e) { say("The request failed: " + e, true); }
  });
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-delete]");
    if (!button) return;
    button.disabled = true;
    try {
      const resp = await fetch(base + "/jobs/" + button.dataset.delete, { method: "DELETE", headers });
      if (!resp.ok) { const data = await resp.json(); say((data.error && data.error.message) || resp.statusText, true); }
    } catch (e) { say("The request failed: " + e, true); }
    update();
  });
})();""".replace("NAMES", json.dumps(THEME_VARS)).replace("REFRESH", str(REFRESH))
TARGETS = (("Audio", "audio", ("m4a", "mp3", "opus", "flac", "wav", "best")),
           ("Video", "video", ("mp4", "mkv", "best")))
SETTINGS_ICON = ("M12,8A4,4 0 0,1 16,12A4,4 0 0,1 12,16A4,4 0 0,1 8,12A4,4 0 0,1 12,8M12,10A2,2 0 0,0 10,12A2,2 0 0,0 "
                 "12,14A2,2 0 0,0 14,12A2,2 0 0,0 12,10M10,22C9.75,22 9.54,21.82 9.5,21.58L9.13,18.93C8.5,18.68 "
                 "7.96,18.34 7.44,17.94L4.95,18.95C4.73,19.03 4.46,18.95 4.34,18.73L2.34,15.27C2.21,15.05 2.27,14.78 "
                 "2.46,14.63L4.57,12.97L4.5,12L4.57,11L2.46,9.37C2.27,9.22 2.21,8.95 2.34,8.73L4.34,5.27C4.46,5.05 "
                 "4.73,4.96 4.95,5.05L7.44,6.05C7.96,5.66 8.5,5.32 9.13,5.07L9.5,2.42C9.54,2.18 9.75,2 10,2H14C14.25,2 "
                 "14.46,2.18 14.5,2.42L14.87,5.07C15.5,5.32 16.04,5.66 16.56,6.05L19.05,5.05C19.27,4.96 19.54,5.05 "
                 "19.66,5.27L21.66,8.73C21.79,8.95 21.73,9.22 21.54,9.37L19.43,11L19.5,12L19.43,13L21.54,14.63C21.73,"
                 "14.78 21.79,15.05 21.66,15.27L19.66,18.73C19.54,18.95 19.27,19.04 19.05,18.95L16.56,17.95C16.04,18.34 "
                 "15.5,18.68 14.87,18.93L14.5,21.58C14.46,21.82 14.25,22 14,22H10M11.25,4L10.88,6.61C9.68,6.86 8.62,7.5 "
                 "7.85,8.39L5.44,7.35L4.69,8.65L6.8,10.2C6.4,11.37 6.4,12.64 6.8,13.8L4.68,15.36L5.43,16.66L7.86,15.62C8.63,"
                 "16.5 9.68,17.14 10.87,17.38L11.24,20H12.76L13.13,17.39C14.32,17.14 15.37,16.5 16.14,15.62L18.57,16.66L19.32,"
                 "15.36L17.2,13.81C17.6,12.64 17.6,11.37 17.2,10.2L19.31,8.65L18.56,7.35L16.15,8.39C15.38,7.5 14.32,6.86 "
                 "13.12,6.62L12.75,4H11.25Z")
STATE_CLASS = {"done": "ok", "failed": "bad", "cancelled": "muted", "queued": "muted"}

esc = html.escape


def _size(n: int | None) -> str:
    if n is None:
        return "–"
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return "–"


def _duration(seconds: int | None) -> str:
    if seconds is None:
        return "–"
    return f"{seconds // 60}:{seconds % 60:02d}"


def _kv(rows: list[tuple[str, str]]) -> str:
    return '<table class="kv">' + "".join(f"<tr><th>{esc(k)}</th><td>{v}</td></tr>" for k, v in rows) + "</table>"


def _job_row(job: dict, base: str) -> str:
    state = job["state"]
    cls = STATE_CLASS.get(state, "busy")
    detail = ""
    if state == "downloading":
        detail = (f"{_size(job['done_bytes'])} of {_size(job['total_bytes'])}" if job["total_bytes"]
                  else _size(job["done_bytes"]))
    elif state == "done":
        detail = (f'<a class="download" href="{esc(base)}/jobs/{esc(job["id"])}/file" download>Download</a> '
                  f'<span class="muted">{_size(job["size"])}, {esc(job["content_type"] or "")}</span>')
    elif state == "failed" and job["error"]:
        detail = f'<span class="bad">{esc(job["error"]["code"])}</span>: {esc(job["error"]["message"])}'
    action = "Cancel" if state in ("queued", "probing", "downloading", "converting") else "Remove"
    button = (f'<button class="small" type="button" data-delete="{esc(job["id"])}">{action}</button>'
              if state != "cancelled" else "")
    title = esc(job["title"] or "") or '<span class="muted">(not probed yet)</span>'
    return (f'<tr><td class="nowrap"><span class="{cls}">{esc(state)}</span><span class="sub">{esc(job["id"])}</span></td>'
            f'<td>{title}<span class="sub">{esc(job["url"])}</span></td>'
            f'<td class="nowrap">{esc(job["kind"])} / {esc(job["format"])}<span class="sub">{_duration(job["duration"])}</span></td>'
            f'<td>{detail}<span class="sub">{esc(job["created"] or "")}</span></td><td class="nowrap">{button}</td></tr>')


def _form() -> str:
    options = "".join(
        f'<optgroup label="{label}">' + "".join(
            f'<option value="{kind}/{fmt}"{" selected" if kind == "audio" and fmt == "m4a" else ""}>'
            f'{fmt}{" (keep the source format)" if fmt == "best" else ""}</option>' for fmt in formats) + "</optgroup>"
        for label, kind, formats in TARGETS)
    return ('<form id="fetch"><input type="url" name="url" placeholder="https://…" required autocomplete="off">'
            f'<select name="target" title="Format">{options}</select>'
            '<button class="primary" type="submit">Fetch</button></form>'
            '<p id="message" class="muted">Paste a link, pick a format, and download the file from the list below '
            'when the job is done.</p>')


def render(ctx: Context, jobs: list[dict], base: str = "") -> str:
    versions = ctx.versions
    runtime = versions.get("js_runtime")
    ffmpeg = versions.get("ffmpeg")
    service = _kv([
        ("App", esc(ctx.version)),
        ("yt-dlp", esc(versions.get("yt_dlp") or "–")),
        ("JavaScript runtime", esc(runtime) if runtime else '<span class="bad">none</span>'),
        ("ffmpeg", esc(ffmpeg) if ffmpeg else '<span class="bad">none</span>'),
        ("API definition", f'<a class="download" href="{esc(base)}{OPENAPI_PATH}" download="media-fetcher-openapi.yaml">'
                           'openapi.yaml</a>'),
    ])
    s = ctx.settings
    settings = _kv([
        ("API token", "from the options, for clients other than Home Assistant" if s.get("token_set")
         else "none: Home Assistant only"),
        ("Concurrent jobs", esc(str(s.get("max_jobs", "–")))),
        ("Keep finished jobs", f"{esc(str(s.get('keep_minutes', '–')))} min"),
        ("Allow any site", "yes" if s.get("allow_any_site") else "no"),
    ])
    rows = "".join(_job_row(job, base) for job in jobs)
    table = (f'<div class="scroll"><table class="list"><tr><th>State</th><th>Title</th><th>Kind</th><th>Result</th>'
             f'<th></th></tr>{rows}</table></div>') if jobs else '<div class="content"><p class="muted">No jobs.</p></div>'
    actions = (f'<a href="/config/app/{esc(ctx.slug)}/config" target="_top" title="Settings">'
               f'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="{SETTINGS_ICON}"/></svg></a>' if ctx.slug else "")
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1"><title>Media Fetcher</title>'
            f"<style>{STYLE}</style></head><body data-base=\"{esc(base)}\">"
            f'<header><h1>Media Fetcher</h1><div class="actions">{actions}</div></header>'
            f'<section><div class="card" style="margin:16px auto 0;max-width:1040px"><h2>Fetch</h2>'
            f'<div class="content">{_form()}</div></div></section><main>'
            f'<div class="card"><h2>Jobs</h2>{table}</div>'
            f'<div class="cards"><div class="card"><h2>Service</h2><div class="content">{service}</div></div>'
            f'<div class="card"><h2>Settings</h2><div class="content">{settings}</div></div></div>'
            f"</main><script>{SCRIPT}</script></body></html>")


def ingress_base(request: web.Request) -> str:
    """The path prefix the page's links need: the Supervisor's ingress path,
    nothing when the page is opened directly."""
    base = request.headers.get(INGRESS_HEADER, "")
    return base.rstrip("/") if INGRESS_PATH.fullmatch(base) else ""


async def status_page(request: web.Request) -> web.Response:
    ctx: Context = request.app["ctx"]
    if request.remote not in ctx.page_peers:
        return web.Response(status=403, text="only through Home Assistant")
    return web.Response(text=render(ctx, ctx.manager.snapshots(), ingress_base(request)), content_type="text/html",
                        headers={"Cache-Control": "no-store"})


# --- the application -------------------------------------------------------------------------
def create_app(manager: JobManager, token: str, versions: dict, version: str,
               page_peers: frozenset[str] = frozenset({INGRESS_PEER}),
               core_peers: frozenset[str] = frozenset({CORE_PEER}), slug: str | None = None,
               settings: dict | None = None) -> web.Application:
    app = web.Application(middlewares=[errors, auth], client_max_size=BODY_LENGTH)
    app["ctx"] = Context(manager, token, versions, version, page_peers, core_peers, slug, settings or {})
    app.router.add_get(HEALTH, health)
    app.router.add_get(OPENAPI_PATH, openapi)
    app.router.add_post(f"{API}/info", info)
    app.router.add_get(f"{API}/jobs", list_jobs)
    app.router.add_post(f"{API}/jobs", create_job)
    app.router.add_get(f"{API}/jobs/{{id}}", get_job)
    app.router.add_delete(f"{API}/jobs/{{id}}", delete_job)
    app.router.add_get(f"{API}/jobs/{{id}}/file", get_file)
    app.router.add_get("/", status_page)
    app.router.add_post("/jobs", page_create_job)
    app.router.add_delete("/jobs/{id}", page_delete_job)
    app.router.add_get("/jobs/{id}/file", page_file)
    return app
