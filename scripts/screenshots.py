"""The Page check, and the screenshots in docs/screenshots: starts the built
image with made-up jobs in a throwaway container, opens the page with a
headless browser the way Home Assistant's ingress does, checks it and takes
the pictures.

    docker build --build-arg BUILD_VERSION=0.4.1 -t media-fetcher:check media_fetcher
    uv run --with playwright==1.65.0 python scripts/screenshots.py --image media-fetcher:check

Two containers of the image in a throwaway Docker network with Home
Assistant's app network addresses: the app (scripts/page/demo.py, the
image's API code with generated jobs) under the host name the Supervisor
gives it, and the ingress proxy (scripts/page/ingress.py) at 172.30.32.2,
the only address the page answers. The browser talks to the proxy only, on
a port of 127.0.0.1.

Checked in light, dark and at phone width: the header (name, stamp with the
app's time, refresh, settings link with target="_top" and the slug from the
host name), the version line as the last element, no console errors, no
script errors, no content security policy violations, no sideways
scrolling; and once that refresh reloads, the page updates itself with the
stamp, a job is removed, and the settings link leaves the ingress frame; and
that the page takes Home Assistant's language (a stand-in for its frontend
in German, English and French around the frame) over the browser's,
reloading once.

The browser is the installed Google Chrome, else Playwright's Chromium
(`uv run --with playwright==1.65.0 playwright install chromium` once).
Exits 1 when a check fails; the pictures are written either way.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import Frame, Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
HOSTNAME = "local-media-fetcher"       # the Supervisor's name for the container
SLUG = HOSTNAME.replace("-", "_")
TZ = "Europe/Berlin"                    # the Supervisor hands Home Assistant's time zone to the app
SUBNET, GATEWAY = "172.30.32.0/23", "172.30.32.1"
APP_IP, INGRESS_IP = "172.30.33.10", "172.30.32.2"
PREFIX = "/api/hassio_ingress/page-check-token"
TEXT = {  # what the header says, per language
    "en": {"as_of": "As of", "refresh": "Refresh", "settings": "Settings"},
    "de": {"as_of": "Stand", "refresh": "Aktualisieren", "settings": "Einstellungen"},
}
VARIANTS = (  # name, width, height, colour scheme, screenshot
    ("desktop light", 1100, 900, "light", "page.png"),
    ("desktop dark", 1100, 900, "dark", None),
    ("phone light", 390, 844, "light", None),
    ("phone dark", 390, 844, "dark", "phone-dark.png"),
)
# Collects what the browser reports as a content security policy violation
CSP_WATCH = """window.__csp = [];
document.addEventListener("securitypolicyviolation",
  (e) => window.__csp.push(e.violatedDirective + " " + (e.blockedURI || "inline")));"""


def version() -> str:
    for line in (ROOT / "media_fetcher/config.yaml").read_text().splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip().strip('"')
    raise SystemExit("no version in config.yaml")


def docker(*args: str, check: bool = True) -> str:
    done = subprocess.run(["docker", *args], capture_output=True, text=True)
    if check and done.returncode:
        raise SystemExit(f"docker {' '.join(args)}: {done.stderr.strip()}")
    return done.stdout.strip()


def network() -> str:
    """A network with Home Assistant's app network addresses; another check
    on this machine may hold the range for a while."""
    name = f"media-fetcher-page-{uuid.uuid4().hex[:8]}"
    for _ in range(36):
        done = subprocess.run(["docker", "network", "create", "--subnet", SUBNET, "--gateway", GATEWAY, name],
                              capture_output=True, text=True)
        if not done.returncode:
            return name
        if "overlap" not in done.stderr:
            raise SystemExit(f"docker network create: {done.stderr.strip()}")
        print(f"{SUBNET} is in use by another network, waiting", flush=True)
        time.sleep(5)
    raise SystemExit(f"{SUBNET} stayed in use")


def wait_for(url: str, seconds: float = 60) -> None:
    deadline = time.monotonic() + seconds
    while True:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return
        except OSError:
            pass
        if time.monotonic() > deadline:
            raise SystemExit(f"{url} does not answer")
        time.sleep(0.5)


class Checks:
    def __init__(self) -> None:
        self.failed: list[str] = []

    def __call__(self, ok: object, what: str) -> None:
        print(f"{'ok' if ok else 'FAILED'}: {what}", flush=True)
        if not ok:
            self.failed.append(what)


def app_time() -> str:
    return datetime.now(ZoneInfo(TZ)).strftime("%H:%M:%S")


def near(stamp: str, seconds: int = 90) -> bool:
    """The stamp is the app's local time, give or take."""
    now = datetime.now(ZoneInfo(TZ))
    try:
        then = datetime.strptime(stamp, "%H:%M:%S").replace(year=now.year, month=now.month, day=now.day,
                                                             tzinfo=now.tzinfo)
    except ValueError:
        return False
    diff = abs((now - then).total_seconds()) % 86400
    return min(diff, 86400 - diff) <= seconds


def luminance(rgb: str) -> float:
    r, g, b = (int(x) for x in re.findall(r"\d+", rgb)[:3])
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255


def check_header(check: Checks, page: Page | Frame, name: str, width: int, lang: str = "en") -> None:
    t = TEXT[lang]
    header = page.evaluate("""() => {
      const h = document.querySelector("body > header");
      if (!h) return null;
      const kids = [...h.children], actions = h.querySelector(".actions");
      return {
        first: kids.map((e) => e.tagName.toLowerCase()), h1: (h.querySelector("h1") || {}).textContent,
        // The store icon left of the name: loaded, 24 px, in the row of the name
        icon: h.firstElementChild.matches("img.app-icon") ? (() => {
          const box = h.firstElementChild.getBoundingClientRect(), name = h.querySelector(":scope > h1").getBoundingClientRect();
          return {src: h.firstElementChild.getAttribute("src"), loaded: h.firstElementChild.complete && h.firstElementChild.naturalWidth > 0,
                  size: [box.width, box.height], row: Math.abs((box.top + box.bottom) / 2 - (name.top + name.bottom) / 2) < 2};
        })() : null,
        actions: actions ? [...actions.children].map((e) => ({
          tag: e.tagName.toLowerCase(), cls: e.className.baseVal ?? e.className, id: e.id, text: e.textContent,
          href: e.getAttribute("href"), target: e.getAttribute("target"), title: e.getAttribute("title"),
          role: e.getAttribute("role"), svg: !!e.querySelector("svg path"),
          visible: getComputedStyle(e).display !== "none" })) : [],
      };
    }""")
    check(header is not None, f"{name}: the page has a header")
    if header is None:
        return
    check(header["first"] == ["img", "h1", "div"] and header["h1"] == "Media Fetcher",
          f"{name}: header is the icon, h1 'Media Fetcher' and the actions ({header['first']}, {header['h1']!r})")
    check(header["icon"] == {"src": "icon.png", "loaded": True, "size": [24, 24], "row": True},
          f"{name}: the app's icon shows, 24 px, in the row of the name ({header['icon']})")
    acts = header["actions"]
    check([a["tag"] for a in acts] == ["span", "a", "a"], f"{name}: actions are stamp, refresh, settings")
    if len(acts) == 3:
        stamp, refresh, settings = acts
        m = re.fullmatch(rf"{t['as_of']} (\d\d:\d\d:\d\d)", stamp["text"] or "")
        check("stamp" in stamp["cls"] and "opt" in stamp["cls"] and m and near(m.group(1)),
              f"{name}: stamp {stamp['text']!r} is the app's time ({TZ}, now {app_time()})")
        check(stamp["visible"] == (width > 600), f"{name}: stamp {'shown' if width > 600 else 'hidden'} at {width} px")
        check(refresh["id"] == "refresh" and refresh["href"] == "" and refresh["role"] == "button"
              and refresh["title"] == t["refresh"] and "icon-button" in refresh["cls"] and refresh["svg"],
              f"{name}: refresh button")
        check(settings["href"] == f"/config/app/{SLUG}/config" and settings["target"] == "_top"
              and settings["title"] == t["settings"] and "icon-button" in settings["cls"] and settings["svg"],
              f"{name}: settings link {settings['href']} target {settings['target']}")


def check_page(check: Checks, page, name: str, width: int, scheme: str, expected_version: str) -> None:
    check_header(check, page, name, width)
    last = page.evaluate("""() => { const e = document.body.lastElementChild;
      return e ? e.tagName.toLowerCase() + "." + e.className + " " + e.textContent : null; }""")
    check(last == f"footer.version Media Fetcher {expected_version}", f"{name}: version line last ({last!r})")
    over = page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
    check(over <= 0, f"{name}: no sideways scrolling ({over} px over)")
    light = luminance(page.evaluate("() => getComputedStyle(document.body).backgroundColor")) > 0.5
    check(light == (scheme == "light"), f"{name}: {scheme} colours")
    rows = page.locator("table.list tr:has(td)").count()
    check(rows == 4, f"{name}: the four made-up jobs ({rows})")


def run(image: str, out: Path) -> list[str]:
    expected = docker("image", "inspect", "-f", "{{range .Config.Env}}{{println .}}{{end}}", image)
    expected_version = next((line.split("=", 1)[1] for line in expected.splitlines()
                             if line.startswith("APP_VERSION=")), "unknown")
    if expected_version != version():
        print(f"note: the image is version {expected_version}, config.yaml says {version()}")
    page_dir = str(ROOT / "scripts/page")
    net = network()
    containers: list[str] = []
    check = Checks()
    try:
        containers.append(docker(
            "run", "-d", "--network", net, "--ip", APP_IP, "--hostname", HOSTNAME, "--user", "fetcher",
            "-e", f"TZ={TZ}", "-e", "PYTHONPATH=/usr/lib", "-v", f"{page_dir}:/page:ro",
            "--entrypoint", "python3", image, "/page/demo.py"))
        containers.append(docker(
            "run", "-d", "--network", net, "--ip", INGRESS_IP, "-p", "127.0.0.1::8080", "--user", "nobody",
            "-e", f"APP=http://{APP_IP}:8099", "-v", f"{page_dir}:/page:ro",
            "--entrypoint", "python3", image, "/page/ingress.py"))
        port = docker("port", containers[1], "8080/tcp").splitlines()[0].rsplit(":", 1)[1]
        base = f"http://127.0.0.1:{port}"
        wait_for(f"{base}{PREFIX}/api/v1/health")
        # The page answers the ingress proxy only, not Home Assistant Core's address
        direct = docker("run", "--rm", "--network", net, "--entrypoint", "python3", image, "-c",
                        "import urllib.request, urllib.error\n"
                        f"try: urllib.request.urlopen('http://{APP_IP}:8099/', timeout=5)\n"
                        "except urllib.error.HTTPError as e: print(e.code)", check=False)
        check(direct == "403", f"the page refuses other addresses ({direct})")
        out.mkdir(parents=True, exist_ok=True)
        chrome = Path("/Applications/Google Chrome.app").exists() or shutil.which("google-chrome")
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome" if chrome else None)
            for name, width, height, scheme, shot in VARIANTS:
                # A browser in English, whatever this machine's language
                context = browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme,
                                              device_scale_factor=2 if width < 600 else 1, locale="en-US")
                context.add_init_script(CSP_WATCH)
                page = context.new_page()
                errors: list[str] = []
                page.on("console", lambda m, e=errors: m.type == "error" and e.append(m.text))
                page.on("pageerror", lambda x, e=errors: e.append(str(x)))
                resp = page.goto(f"{base}{PREFIX}/")
                check(resp is not None and resp.status == 200, f"{name}: page answers through ingress")
                csp = resp.headers.get("content-security-policy", "") if resp else ""
                check("frame-ancestors 'self'" in csp, f"{name}: content security policy sent")
                page.wait_for_load_state("networkidle")
                check_page(check, page, name, width, scheme, expected_version)
                if shot:
                    page.screenshot(path=out / shot, full_page=True)
                    print(f"wrote {out / shot}")
                check(not errors, f"{name}: no console or script errors {errors}")
                csp_hits = page.evaluate("() => window.__csp")
                check(not csp_hits, f"{name}: no content security policy violations {csp_hits}")
                context.close()
            interact(check, browser, base)
            frame_check(check, browser, base)
            language_check(check, browser, base)
            browser.close()
    finally:
        if check.failed:
            for cid in containers:
                print(f"--- log of {cid[:12]}")
                print(docker("logs", "--tail", "30", cid, check=False))
        for cid in containers:
            docker("rm", "-f", cid, check=False)
        docker("network", "rm", net, check=False)
    return check.failed


def interact(check: Checks, browser, base: str) -> None:
    """Refresh reloads; the page updates itself with its stamp; a job goes."""
    context = browser.new_context(viewport={"width": 1100, "height": 900}, locale="en-US")
    context.add_init_script(CSP_WATCH)
    page = context.new_page()
    errors: list[str] = []
    page.on("console", lambda m: m.type == "error" and errors.append(m.text))
    page.on("pageerror", lambda x: errors.append(str(x)))
    page.goto(f"{base}{PREFIX}/")
    page.wait_for_load_state("networkidle")
    link = page.locator('a.download[href$="/jobs/4be0c1d29a7f3e15/file"]')
    href = link.get_attribute("href") if link.count() else None
    check(href == f"{PREFIX}/jobs/4be0c1d29a7f3e15/file", f"download link under the ingress path ({href})")
    if href:
        resp = page.request.get(base + href)
        check(resp.status == 200 and "Evening birdsong" in resp.headers.get("content-disposition", ""),
              f"download answers ({resp.status})")
    page.evaluate("() => { window.__before = true; }")
    with page.expect_navigation():
        page.click("#refresh")
    page.wait_for_load_state("networkidle")
    check(page.evaluate("() => window.__before === undefined"), "refresh reloads the page")
    stamp = page.text_content("header .stamp")
    page.wait_for_timeout(11_500)
    later = page.text_content("header .stamp")
    check(later != stamp and page.locator("table.list").count() == 1,
          f"the page updates itself, stamp included ({stamp} → {later})")
    page.click('button[data-delete="3c7d52e08f1ab946"]')
    page.wait_for_function("() => !document.querySelector('button[data-delete=\"3c7d52e08f1ab946\"]')",
                           timeout=10_000)
    check(page.locator("table.list tr:has(td)").count() == 3, "Remove takes the failed job off the list")
    check(not errors, f"while clicking: no console or script errors {errors}")
    csp_hits = page.evaluate("() => window.__csp")
    check(not csp_hits, f"while clicking: no content security policy violations {csp_hits}")
    context.close()


def frame_check(check: Checks, browser, base: str) -> None:
    """Inside Home Assistant's frame the page loads, and the gear opens the
    settings page outside the frame."""
    page = browser.new_page()
    errors: list[str] = []
    page.on("console", lambda m: m.type == "error" and errors.append(m.text))
    page.goto(base + "/")
    frame = page.frame_locator("iframe")
    frame.locator("header h1").wait_for()
    frame.locator(f'a[href="/config/app/{SLUG}/config"]').click()
    page.wait_for_url(f"{base}/config/app/{SLUG}/config")
    check(page.url == f"{base}/config/app/{SLUG}/config", f"in the frame the gear opens the settings at the top ({page.url})")
    check(not errors, f"in the frame: no console errors {errors}")
    page.close()


def language_check(check: Checks, browser, base: str) -> None:
    """The browser asks for English; the page follows Home Assistant's
    language (share/ha-app/ha-lang.js): one reload when it changes, none
    when not, the cookie for the ingress path only."""
    context = browser.new_context(viewport={"width": 1100, "height": 900}, locale="en-US",
                                  extra_http_headers={"Accept-Language": "en"})
    context.add_init_script(CSP_WATCH)
    page = context.new_page()
    errors: list[str] = []
    page.on("console", lambda m: m.type == "error" and errors.append(m.text))
    page.on("pageerror", lambda x: errors.append(str(x)))
    for ha_lang, lang, expected in (("de", "de", 2), ("de", "de", 1), ("en-GB", "en", 2), ("fr", "en", 2),
                                    ("fr", "en", 1)):
        where = f"Home Assistant in {ha_lang}"
        app, loads = in_home_assistant(page, base, ha_lang)
        shown = app.evaluate("[document.documentElement.lang, getComputedStyle(document.documentElement).visibility]")
        check(shown == [lang, "visible"], f"{where}: page in {lang} and shown ({shown})")
        check(loads == expected, f"{where}: loaded {expected} time(s) ({loads})")
        check_header(check, app, where, 1100, lang)
        held = [(k["value"], k["path"]) for k in context.cookies() if k["name"] == "ha_lang"]
        check(held == [(ha_lang.split("-")[0], f"{PREFIX}/")], f"{where}: cookie for the ingress path ({held})")
    csp_hits = [v for f in page.frames for v in (f.evaluate("window.__csp || []") or [])]
    check(not errors and not csp_hits, f"language: no console errors, no CSP violations ({errors + csp_hits})")
    context.close()


def in_home_assistant(page: Page, base: str, lang: str) -> tuple[Frame, int]:
    """Opens the page in a stand-in for Home Assistant's frontend in that
    language; the app's frame and how often it loaded."""
    loads: list[str] = []

    def seen(frame: Frame) -> None:
        if frame.parent_frame is not None and frame.url.startswith(base + PREFIX):
            loads.append(frame.url)

    page.on("framenavigated", seen)
    page.goto(f"{base}/?lang={lang}")
    page.wait_for_timeout(1500)  # room for another reload, which must not come
    page.remove_listener("framenavigated", seen)
    app = next(f for f in page.frames if f.parent_frame is not None)
    app.locator("body > header h1").wait_for()
    return app, len(loads)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--image", default="media-fetcher:check", help="the built image (default media-fetcher:check)")
    parser.add_argument("--out", type=Path, default=ROOT / "docs/screenshots", help="where the pictures go")
    args = parser.parse_args()
    failed = run(args.image, args.out)
    print("====================")
    if failed:
        print(f"FAILED: {len(failed)} page checks")
        for what in failed:
            print(f"  {what}")
        sys.exit(1)
    print("All page checks passed.")


if __name__ == "__main__":
    main()
