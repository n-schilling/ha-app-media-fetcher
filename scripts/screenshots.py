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

Checked in light, dark and at phone width: the header as Home Assistant's
own pages draw it (STANDARD.md, section 7: the menu button first, shown
only when narrow, below 870 px; then for this single view the store icon
left of the name, the name exactly as `name` in config.yaml, everything
within the header's box, no tabs and no bottom bar; stamp with the app's
time, hidden when narrow, refresh, settings link with target="_top" and the
slug from the host name), the version line as the last element, no console
errors, no script errors, no content security policy violations, no
sideways scrolling; Home Assistant's frame (the stand-in plays its part,
share/ha-app/ha-frame.js the page's): no kiosk mode on a wide screen; on a
phone (390 x 844, with a notch of 47 px and a home bar of 34 px, and
without) kiosk mode, so no bar of Home Assistant above the app, the insets
handled by the page, the header from the top and 56 px plus the notch, the
menu button opening Home Assistant's sidebar, wider and back switching
kiosk mode off and on with one message each way; and once that refresh
reloads and moves the stamp, the page updates itself with the stamp, a job
is removed, and the settings link leaves the ingress frame; and that the
page takes Home Assistant's language (a stand-in for its frontend in
German, English and French around the frame) over the browser's, reloading
once.

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
    "en": {"as_of": "As of", "refresh": "Refresh", "settings": "Settings", "menu": "Menu"},
    "de": {"as_of": "Stand", "refresh": "Aktualisieren", "settings": "Einstellungen", "menu": "Menü"},
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


def config(key: str) -> str:
    for line in (ROOT / "media_fetcher/config.yaml").read_text().splitlines():
        if line.startswith(f"{key}:"):
            return line.split(":", 1)[1].strip().strip('"')
    raise SystemExit(f"no {key} in config.yaml")


def version() -> str:
    return config("version")


NAME = config("name")  # the name the app store shows; the header must say exactly this


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


def check_header(check: Checks, page: Page | Frame, name: str, width: int, lang: str = "en",
                 safe: tuple[int, int] = (0, 0)) -> None:
    """The header as Home Assistant's own pages draw it (STANDARD.md, section 7): narrow is
    Home Assistant's (below 870 px), safe the insets top and bottom the page keeps clear of."""
    t = TEXT[lang]
    phone = width <= 870
    header = page.evaluate("""() => {
      const h = document.querySelector("body > header");
      if (!h) return null;
      const kids = [...h.children], actions = h.querySelector(".actions"), box = h.getBoundingClientRect();
      const shown = (e) => !!e && getComputedStyle(e).display !== "none";
      const m = h.querySelector(":scope > .menu"), brand = h.querySelector(":scope > img.brand");
      return {
        tags: kids.map((k) => k.tagName.toLowerCase() + (k.className ? "." + k.className.split(" ").join(".") : "")),
        h1: (h.querySelector("h1") || {}).textContent,
        narrow: document.documentElement.classList.contains("narrow"), top: box.top, height: box.height,
        menu: m && {first: kids[0] === m, ha: m.hasAttribute("data-ha-menu"), role: m.getAttribute("role"),
                    title: m.title, label: m.getAttribute("aria-label"), icon: !!m.querySelector("svg path"), shown: shown(m)},
        // The store icon left of the name: loaded, 24 px, in the row of the name
        icon: brand ? (() => {
          const r = brand.getBoundingClientRect(), name = h.querySelector(":scope > h1").getBoundingClientRect();
          return {src: brand.getAttribute("src"), loaded: brand.complete && brand.naturalWidth > 0,
                  size: [r.width, r.height], row: Math.abs((r.top + r.bottom) / 2 - (name.top + name.bottom) / 2) < 2,
                  left: r.right <= name.left};
        })() : null,
        // Menu, icon, name and buttons within the header's box (below the notch, on a phone too)
        inside: [m, brand, h.querySelector(":scope > h1"), ...(actions ? actions.children : [])]
          .filter(shown).every((e) => { const r = e.getBoundingClientRect();
            return r.left >= box.left - 0.5 && r.right <= box.right + 0.5 && r.top >= box.top - 0.5 && r.bottom <= box.bottom + 0.5; }),
        // A single view: no tabs, so no bar at the bottom either
        tabs: document.querySelectorAll("header .tabs, header .tab").length,
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
    check(header["narrow"] == phone, f"{name}: html.narrow {phone} (Home Assistant's narrow)")
    check(header["tags"] == ["a.icon-button.menu", "img.brand", "h1.name", "div.actions"] and header["h1"] == NAME,
          f"{name}: header is the menu button, the icon, h1 {NAME!r} (config.yaml) and the actions "
          f"({header['tags']}, {header['h1']!r})")
    check(abs(header["top"]) < 0.5 and abs(header["height"] - 56 - safe[0]) < 0.5,
          f"{name}: header from the top, 56 + {safe[0]} px high ({header['top']}, {header['height']})")
    m = header["menu"] or {}
    check(m.get("first") and m.get("ha") and m.get("role") == "button" and m.get("title") == t["menu"] == m.get("label")
          and m.get("icon") and m.get("shown") == phone, f"{name}: menu button first, {'shown' if phone else 'hidden'} ({m})")
    check(header["icon"] == {"src": "icon.png", "loaded": True, "size": [24, 24], "row": True, "left": True},
          f"{name}: the app's icon shows, 24 px, left of the name in its row ({header['icon']})")
    check(header["inside"], f"{name}: menu, icon, name and buttons within the header at {width} px")
    check(header["tabs"] == 0, f"{name}: a single view, no tabs and no bottom bar")
    acts = header["actions"]
    check([a["tag"] for a in acts] == ["span", "a", "a"], f"{name}: actions are stamp, refresh, settings")
    if len(acts) == 3:
        stamp, refresh, settings = acts
        m = re.fullmatch(rf"{t['as_of']} (\d\d:\d\d:\d\d)", stamp["text"] or "")
        check("stamp" in stamp["cls"] and "opt" in stamp["cls"] and m and near(m.group(1)),
              f"{name}: stamp {stamp['text']!r} is the app's time ({TZ}, now {app_time()})")
        check(stamp["visible"] != phone, f"{name}: stamp {'hidden' if phone else 'shown'} at {width} px")
        check(refresh["id"] == "refresh" and refresh["href"] == "" and refresh["role"] == "button"
              and refresh["title"] == t["refresh"] and "icon-button" in refresh["cls"] and refresh["svg"],
              f"{name}: refresh button")
        check(settings["href"] == f"/config/app/{SLUG}/config" and settings["target"] == "_top"
              and settings["title"] == t["settings"] and "icon-button" in settings["cls"] and settings["svg"],
              f"{name}: settings link {settings['href']} target {settings['target']}")


def security_headers(check: Checks, headers: dict[str, str], where: str) -> None:
    """The headers STANDARD.md section 3 asks of every answer of the page."""
    policy = {
        part.split()[0]: part.split()[1:]
        for part in headers.get("content-security-policy", "").split(";") if part.strip()
    }
    scripts = policy.get("script-src", [])
    check(policy.get("default-src") == ["'none'"], f"{where}: CSP default-src 'none' ({policy.get('default-src')})")
    check("'unsafe-inline'" not in scripts and any(s.startswith(("'sha256-", "'nonce-")) for s in scripts),
          f"{where}: CSP script-src by hash or nonce ({scripts})")
    check(policy.get("frame-ancestors") == ["'self'"], f"{where}: CSP frame-ancestors 'self'")
    check(policy.get("base-uri") == ["'none'"], f"{where}: CSP base-uri 'none'")
    check(policy.get("form-action") in (["'none'"], ["'self'"]), f"{where}: CSP form-action")
    check(headers.get("x-content-type-options") == "nosniff", f"{where}: X-Content-Type-Options nosniff")
    check(headers.get("referrer-policy") in ("no-referrer", "same-origin"),
          f"{where}: Referrer-Policy ({headers.get('referrer-policy')})")
    check(headers.get("cache-control") == "no-store", f"{where}: Cache-Control no-store")


def check_page(check: Checks, page, name: str, width: int, scheme: str, expected_version: str) -> None:
    check_header(check, page, name, width)
    last = page.evaluate("""() => { const e = document.body.lastElementChild;
      return e ? e.tagName.toLowerCase() + "." + e.className + " " + e.textContent : null; }""")
    check(last == f"footer.version {NAME} {expected_version}", f"{name}: version line last ({last!r})")
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
                if resp is not None:
                    security_headers(check, resp.headers, name)
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
            for width, height, safe, lang in ((1200, 760, (0, 0), "en"), (390, 844, (47, 34), "de"),
                                              (390, 844, (0, 0), "en")):
                # The phone with a notch as a picture for review, not for the README
                notch = safe != (0, 0) and out.resolve() != (ROOT / "docs/screenshots").resolve()
                ha_frame(check, browser, base, width, height, safe, lang, out / "phone-notch.png" if notch else None)
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
    before = page.text_content("header .stamp")
    page.wait_for_timeout(1_100)  # the stamp counts whole seconds
    with page.expect_navigation():
        page.click("#refresh")
    page.wait_for_load_state("networkidle")
    check(page.evaluate("() => window.__before === undefined"), "refresh reloads the page")
    stamp = page.text_content("header .stamp")
    check(stamp != before, f"refresh fetches again and moves the stamp ({before} → {stamp})")
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


def ha_frame(check: Checks, browser, base: str, width: int, height: int, safe: tuple[int, int], lang: str,
             shot: Path | None) -> None:
    """Home Assistant's part of the frame (scripts/page/ingress.py plays it, share/ha-app/ha-frame.js
    the page's): kiosk mode only while narrow, the notch and the home bar by Home Assistant's insets,
    the menu button opening the sidebar."""
    phone = width <= 870
    where = f"frame, {width} px" + (f", notch {safe[0]} px, home bar {safe[1]} px" if safe != (0, 0) else "")
    print(f"==> {where}", flush=True)
    context = browser.new_context(viewport={"width": width, "height": height}, color_scheme="dark",
                                  locale="de-DE" if lang == "de" else "en-US", extra_http_headers={"Accept-Language": lang},
                                  device_scale_factor=2 if phone else 1)
    context.add_init_script(CSP_WATCH)
    page = context.new_page()
    errors: list[str] = []
    page.on("console", lambda m: m.type == "error" and errors.append(m.text))
    page.on("pageerror", lambda x: errors.append(str(x)))
    page.goto(base + "/" + ("?safe=" + ",".join(map(str, safe)) if safe != (0, 0) else ""))
    page.frame_locator("#ingress").locator("body > header h1").wait_for(state="attached")
    page.wait_for_function("window.__safe !== null")
    page.wait_for_timeout(500)  # room for the answer, and for any further message, which must not loop
    app = next(f for f in page.frames if f.parent_frame is not None)
    app.wait_for_selector("html.narrow" if phone else "html:not(.narrow)", state="attached")
    if safe != (0, 0):
        app.wait_for_function("getComputedStyle(document.documentElement)"
                              f".getPropertyValue('--safe-area-inset-top').trim() === '{safe[0]}px'")
    seen = page.evaluate("""() => ({kiosk: window.__kiosk, safe: window.__safe,
      messages: window.__messages.map((m) => m.type + (m.type.endsWith("/subscribe-properties") ? ":" + m.kioskMode : "")),
      bar: getComputedStyle(document.getElementById("ha-bar")).display !== "none",
      frame: (() => { const r = document.getElementById("ingress").getBoundingClientRect(); return [r.top, r.bottom - innerHeight]; })()})""")
    check(seen["kiosk"] is phone, f"{where}: kiosk mode {'' if phone else 'not '}requested ({seen['messages']})")
    check(seen["safe"] is True, f"{where}: the page keeps clear of the notch itself (handleSafeArea)")
    check(not seen["bar"], f"{where}: no bar of Home Assistant above the app")
    check(seen["frame"] == [0, 0], f"{where}: the frame fills the window ({seen['frame']})")
    check(len(seen["messages"]) == 1, f"{where}: one message, no loop ({seen['messages']})")
    check_header(check, app, where, width, lang, safe)
    over = app.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
    check(over <= 0, f"{where}: no sideways scrolling ({over} px over)")
    if safe != (0, 0):
        # The version line, the page's last, at the very end of the page: clear of the home bar
        gap = app.evaluate("""() => { window.scrollTo(0, document.documentElement.scrollHeight);
          return innerHeight - document.querySelector("footer.version").getBoundingClientRect().bottom; }""")
        check(gap >= safe[1] - 0.5, f"{where}: the version line {safe[1]} px clear of the home bar ({gap})")
        app.evaluate("() => window.scrollTo(0, 0)")
    if shot:
        page.screenshot(path=shot)
        print(f"wrote {shot}")
    if phone:
        app.locator("body > header .menu").click()
        page.wait_for_timeout(300)  # the message reaches Home Assistant's window after the click
        check(page.evaluate("window.__menu") == 1, f"{where}: the menu button opens Home Assistant's sidebar")
        if safe != (0, 0):
            # Wider than narrow and back: kiosk mode off (Home Assistant's bar back), then on again
            page.set_viewport_size({"width": 1200, "height": height})
            app.wait_for_selector("html:not(.narrow)", state="attached")
            page.wait_for_timeout(300)
            check(page.evaluate("window.__kiosk") is False, f"{where}: wide again, kiosk mode off")
            page.set_viewport_size({"width": width, "height": height})
            app.wait_for_selector("html.narrow", state="attached")
            page.wait_for_timeout(300)
            sent = page.evaluate("""window.__messages.filter((m) => m.type.endsWith("-properties"))
              .map((m) => m.type.split("/")[1] + (m.type.endsWith("/subscribe-properties") ? ":" + m.kioskMode : ""))""")
            check(page.evaluate("window.__kiosk") is True and sent == [
                "subscribe-properties:true", "unsubscribe-properties", "subscribe-properties:false",
                "subscribe-properties:true"], f"{where}: narrow again, kiosk mode on, one message each way ({sent})")
            check_header(check, app, where, width, lang, safe)
    csp_hits = [v for f in page.frames for v in (f.evaluate("window.__csp || []") or [])]
    check(not errors and not csp_hits, f"{where}: no console errors, no CSP violations ({errors + csp_hits})")
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
