"""The HTTP API and the status page."""

import threading

import pytest
from conftest import TOKEN, Scenario, bearer, create_app, resolve, wait_for

from media_fetcher.fetcher import Fetcher
from media_fetcher.jobs import JobManager

URL = "https://www.youtube.com/watch?v=abc123"


async def test_health_needs_no_token(client):
    resp = await client.get("/api/v1/health")
    assert resp.status == 200
    assert await resp.json() == {"ok": True, "version": "0.2.0", "yt_dlp": "2026.10.26",
                                 "js_runtime": "deno 2.9.8", "ffmpeg": "7.1.2"}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {TOKEN}"},
                                     {"Authorization": "Bearer"}])
async def test_everything_else_needs_the_token(client, headers):
    for method, path in (("get", "/api/v1/jobs"), ("post", "/api/v1/info"), ("post", "/api/v1/jobs"),
                         ("get", "/api/v1/jobs/x"), ("delete", "/api/v1/jobs/x"), ("get", "/api/v1/jobs/x/file")):
        resp = await client.request(method.upper(), path, headers=headers)
        assert resp.status == 401 and resp.headers["WWW-Authenticate"] == "Bearer"
        assert (await resp.json())["error"]["code"] == "unauthorized"


async def test_home_assistant_core_needs_no_token(manager, aiohttp_client):
    """The test client connects from 127.0.0.1; here that address is Home
    Assistant Core's."""
    core = await aiohttp_client(create_app(manager, TOKEN, {}, "0.2.0", core_peers=frozenset({"127.0.0.1"})))
    for headers in ({}, {"Authorization": "Bearer wrong"}, bearer()):
        resp = await core.get("/api/v1/jobs", headers=headers)
        assert resp.status == 200 and await resp.json() == []
        resp = await core.post("/api/v1/info", json={"url": URL}, headers=headers)
        assert resp.status == 200 and (await resp.json())["id"] == "abc123"
    # The address opens the API, not the page's own endpoints
    resp = await core.post("/jobs", json={"url": URL, "kind": "audio"},
                           headers={"Content-Type": "application/json", "X-Requested-With": "media-fetcher"})
    assert resp.status == 403
    resp = await core.get("/")
    assert resp.status == 403


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer "}, {"Authorization": "Bearer wrong"}, bearer()])
async def test_without_a_token_only_home_assistant_gets_in(manager, aiohttp_client, headers):
    closed = await aiohttp_client(create_app(manager, "", {}, "0.2.0", core_peers=frozenset({"172.30.32.1"})))
    resp = await closed.get("/api/v1/jobs", headers=headers)
    assert resp.status == 401 and resp.headers["WWW-Authenticate"] == "Bearer"
    error = (await resp.json())["error"]
    assert error["code"] == "unauthorized" and "Home Assistant" in error["message"]
    assert (await closed.get("/api/v1/health")).status == 200
    core = await aiohttp_client(create_app(manager, "", {}, "0.2.0", core_peers=frozenset({"127.0.0.1"}),
                                           settings={"token_set": False}))
    assert (await core.get("/api/v1/jobs", headers=headers)).status == 200
    page = await (await aiohttp_client(create_app(manager, "", {}, "0.2.0", page_peers=frozenset({"127.0.0.1"}),
                                                  settings={"token_set": False}))).get("/")
    assert "Home Assistant only" in await page.text()


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
async def test_an_untrusted_home_assistant_needs_the_token(manager, aiohttp_client, headers):
    """trust_home_assistant off: no address passes without the token."""
    untrusted = await aiohttp_client(create_app(manager, TOKEN, {}, "0.3.0", core_peers=frozenset(),
                                                page_peers=frozenset({"127.0.0.1"}),
                                                settings={"token_set": True, "trust_home_assistant": False}))
    resp = await untrusted.get("/api/v1/jobs", headers=headers)
    assert resp.status == 401 and (await resp.json())["error"]["message"] == "a valid bearer token is required"
    assert (await untrusted.get("/api/v1/jobs", headers=bearer())).status == 200
    text = await (await untrusted.get("/")).text()
    assert "for every client" in text and "Trust Home Assistant" in text


async def test_untrusted_and_without_a_token_nobody_gets_in(manager, aiohttp_client):
    closed = await aiohttp_client(create_app(manager, "", {}, "0.3.0", core_peers=frozenset(),
                                             page_peers=frozenset({"127.0.0.1"}),
                                             settings={"token_set": False, "trust_home_assistant": False}))
    for headers in ({}, bearer()):
        resp = await closed.get("/api/v1/jobs", headers=headers)
        assert resp.status == 401 and "nobody" in (await resp.json())["error"]["message"]
    assert (await closed.get("/api/v1/health")).status == 200
    assert "none: nobody gets in" in await (await closed.get("/")).text()


async def test_unknown_routes_are_json(client):
    resp = await client.get("/api/v1/nothing", headers=bearer())
    assert resp.status == 404 and (await resp.json())["error"]["code"] == "not_found"
    resp = await client.put("/api/v1/jobs", headers=bearer())
    assert resp.status == 405 and (await resp.json())["error"]["code"] == "method_not_allowed"


async def test_info(client):
    resp = await client.post("/api/v1/info", json={"url": URL}, headers=bearer())
    assert resp.status == 200
    body = await resp.json()
    assert (body["id"], body["title"], body["duration"], body["is_live"]) == ("abc123", "A talk", 417, False)
    assert body["extractor"] == "youtube" and body["thumbnail"].startswith("https://")


@pytest.mark.parametrize("body", [{"url": "ftp://x.test/a"}, {"url": ""}, {"url": 5}, {}, {"url": "https://"},
                                  {"url": "http://x.test/a b"}, {"url": "javascript:alert(1)"}])
async def test_invalid_urls(client, body):
    resp = await client.post("/api/v1/info", json=body, headers=bearer())
    assert resp.status == 400 and (await resp.json())["error"]["code"] == "invalid_url"
    resp = await client.post("/api/v1/jobs", json={**body, "kind": "audio"}, headers=bearer())
    assert resp.status == 400 and (await resp.json())["error"]["code"] == "invalid_url"


async def test_bad_json(client):
    resp = await client.post("/api/v1/info", data=b"{", headers=bearer())
    assert resp.status == 400 and (await resp.json())["error"]["code"] == "invalid_json"
    resp = await client.post("/api/v1/jobs", data=b"[1]", headers=bearer())
    assert resp.status == 400 and (await resp.json())["error"]["code"] == "invalid_json"


@pytest.mark.parametrize(("scenario", "code"), [
    (Scenario(info={"_type": "playlist", "id": "PL", "title": "L", "entries": []}), "unsupported_url"),
    (Scenario(extract_error="ERROR: No suitable extractor found for URL https://x.test/a"), "unsupported_url"),
    (Scenario(extract_error="ERROR: [youtube] a: Video unavailable"), "not_available"),
    (Scenario(extract_error="ERROR: [youtube] a: This live event will begin in 3 hours."), "live"),
    (Scenario(extract_error="ERROR: [youtube] a: Unable to extract player version"), "extract_failed"),
])
async def test_info_errors(aiohttp_client, tmp_path, scenario, code):
    manager = JobManager(Fetcher(False, scenario.ydl_class(), resolve=resolve), tmp_path / "jobs", 2, 3600)
    manager.start()
    client = await aiohttp_client(create_app(manager, TOKEN, {}, "0.2.0"))
    resp = await client.post("/api/v1/info", json={"url": "https://x.test/a"}, headers=bearer())
    assert resp.status == 422
    error = (await resp.json())["error"]
    assert error["code"] == code and error["message"] and not error["message"].startswith("ERROR")
    manager.shutdown()


async def test_a_job_from_start_to_file_to_delete(client, manager):
    resp = await client.post("/api/v1/jobs", json={"url": URL, "kind": "audio"}, headers=bearer())
    assert resp.status == 201
    created = await resp.json()
    assert created["state"] == "queued" and len(created["id"]) == 16
    job_id = created["id"]
    resp = await client.get("/api/v1/jobs", headers=bearer())
    assert [j["id"] for j in await resp.json()] == [job_id]
    wait_for(manager, job_id, "done")
    resp = await client.get(f"/api/v1/jobs/{job_id}", headers=bearer())
    job = await resp.json()
    assert resp.status == 200 and job["state"] == "done" and job["format"] == "m4a" and job["kind"] == "audio"
    assert job["filename"] == f"{job_id}.m4a" and job["content_type"] == "audio/mp4" and job["url"] == URL
    resp = await client.get(f"/api/v1/jobs/{job_id}/file", headers=bearer())
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "audio/mp4" and resp.headers["Content-Length"] == "1000"
    assert resp.headers["Content-Disposition"] == f'attachment; filename="{job_id}.m4a"'
    assert await resp.read() == Scenario().content
    resp = await client.delete(f"/api/v1/jobs/{job_id}", headers=bearer())
    assert resp.status == 204
    for path in (f"/api/v1/jobs/{job_id}", f"/api/v1/jobs/{job_id}/file"):
        resp = await client.get(path, headers=bearer())
        assert resp.status == 404 and (await resp.json())["error"]["code"] == "not_found"
    resp = await client.delete(f"/api/v1/jobs/{job_id}", headers=bearer())
    assert resp.status == 404


async def test_video_job_takes_its_defaults(client, manager, scenario):
    resp = await client.post("/api/v1/jobs", json={"url": URL, "kind": "video", "max_duration": 600}, headers=bearer())
    job = wait_for(manager, (await resp.json())["id"], "done", "failed")
    assert job["format"] == "mp4" and job["state"] == "done"
    assert scenario.instances[-1].params["merge_output_format"] == "mp4"


@pytest.mark.parametrize("body", [{"url": URL}, {"url": URL, "kind": "text"}, {"url": URL, "kind": "audio", "format": "mp4"},
                                  {"url": URL, "kind": "video", "format": "mp3"},
                                  {"url": URL, "kind": "audio", "max_duration": "5"},
                                  {"url": URL, "kind": "audio", "max_duration": 0},
                                  {"url": URL, "kind": "audio", "max_duration": True}])
async def test_invalid_job_requests(client, body):
    resp = await client.post("/api/v1/jobs", json=body, headers=bearer())
    assert resp.status == 400 and (await resp.json())["error"]["code"] == "invalid_request"


async def test_not_ready_and_busy(aiohttp_client, tmp_path):
    gate = threading.Event()
    scenario = Scenario(gate=gate)
    manager = JobManager(Fetcher(False, scenario.ydl_class(), resolve=resolve), tmp_path / "jobs", 1, 3600)
    manager.start()
    client = await aiohttp_client(create_app(manager, TOKEN, {}, "0.2.0"))
    resp = await client.post("/api/v1/jobs", json={"url": URL, "kind": "audio"}, headers=bearer())
    job_id = (await resp.json())["id"]
    wait_for(manager, job_id, "downloading")
    resp = await client.get(f"/api/v1/jobs/{job_id}/file", headers=bearer())
    assert resp.status == 409 and (await resp.json())["error"]["code"] == "not_ready"
    resp = await client.post("/api/v1/jobs", json={"url": URL, "kind": "audio"}, headers=bearer())
    assert resp.status == 429 and (await resp.json())["error"]["code"] == "busy"
    resp = await client.delete(f"/api/v1/jobs/{job_id}", headers=bearer())
    assert resp.status == 204
    gate.set()
    wait_for(manager, job_id, None)
    manager.shutdown()


async def test_status_page_for_the_ingress_peer_only(client, manager, aiohttp_client):
    resp = await client.post("/api/v1/jobs", json={"url": URL, "kind": "audio"}, headers=bearer())
    job_id = (await resp.json())["id"]
    wait_for(manager, job_id, "done")
    resp = await client.get("/")
    assert resp.status == 200 and resp.headers["Content-Type"].startswith("text/html")
    page = await resp.text()
    assert "A talk" in page and job_id in page and "deno 2.9.8" in page and "done" in page
    assert TOKEN not in page and "/config/app/local_media_fetcher/config" in page
    other = await aiohttp_client(create_app(manager, TOKEN, {}, "0.2.0", page_peers=frozenset({"172.30.32.2"})))
    resp = await other.get("/")
    assert resp.status == 403
    resp = await other.get("/api/v1/health")
    assert resp.status == 200


async def test_status_page_without_jobs(client):
    page = await (await client.get("/")).text()
    assert "No jobs." in page and "from the options" in page and "Home Assistant only" not in page


async def test_private_addresses_are_refused_before_a_job_starts(client, scenario):
    for path in ("/api/v1/info", "/api/v1/jobs"):
        resp = await client.post(path, json={"url": "http://nas.lan/Mediasite/Play/1", "kind": "audio"}, headers=bearer())
        assert resp.status == 422
        error = (await resp.json())["error"]
        assert error["code"] == "unsupported_url" and "not a public address" in error["message"]
    assert scenario.extracted == [] and (await (await client.get("/api/v1/jobs", headers=bearer())).json()) == []


async def test_openapi_definition_matches_the_app(client, app):
    import yaml

    resp = await client.get("/api/v1/openapi.yaml")
    assert resp.status == 200 and resp.headers["Content-Type"].startswith("application/yaml")
    spec = yaml.safe_load(await resp.text())
    assert spec["openapi"].startswith("3.1")
    routes = {(r.resource.canonical, r.method) for r in app.router.routes()
              if r.resource.canonical.startswith("/api/v1") and r.method != "HEAD"}
    documented = {(f"/api/v1{path}", method.upper()) for path, item in spec["paths"].items()
                  for method in item if method in ("get", "post", "delete")}
    assert documented == routes
    from media_fetcher.jobs import ACTIVE
    states = set(spec["components"]["schemas"]["Job"]["properties"]["state"]["enum"])
    assert states == ACTIVE | {"done", "failed", "cancelled"}
    # The page offers the definition for download, without the token
    page = await (await client.get("/")).text()
    assert 'href="/api/v1/openapi.yaml" download=' in page
    resp = await client.get("/api/v1/openapi.yaml", headers={"Authorization": "Bearer wrong"})
    assert resp.status == 200


async def test_openapi_version_is_the_apps(app):
    import yaml
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "media_fetcher"
    spec = yaml.safe_load((root / "rootfs/usr/lib/media_fetcher/openapi.yaml").read_text())
    config = yaml.safe_load((root / "config.yaml").read_text())
    assert spec["info"]["version"] == config["version"]


async def test_the_page_starts_jobs_and_serves_downloads(client, manager):
    headers = {"Content-Type": "application/json", "X-Requested-With": "media-fetcher"}
    resp = await client.post("/jobs", json={"url": URL, "kind": "audio", "format": "m4a"}, headers=headers)
    assert resp.status == 201
    job_id = (await resp.json())["id"]
    wait_for(manager, job_id, "done")
    page = await (await client.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/abc-123"})).text()
    assert 'data-base="/api/hassio_ingress/abc-123"' in page
    assert f'href="/api/hassio_ingress/abc-123/jobs/{job_id}/file" download' in page
    assert f'data-delete="{job_id}"' in page and ">Remove<" in page and '<form id="fetch"' in page
    assert 'href="/api/hassio_ingress/abc-123/api/v1/openapi.yaml"' in page
    page = await (await client.get("/", headers={"X-Ingress-Path": "../evil"})).text()
    assert 'data-base=""' in page
    resp = await client.get(f"/jobs/{job_id}/file")
    assert resp.status == 200 and resp.headers["Content-Type"] == "audio/mp4"
    assert resp.headers["Content-Disposition"] == 'attachment; filename="A talk.m4a"'
    assert await resp.read() == Scenario().content
    resp = await client.delete(f"/jobs/{job_id}", headers=headers)
    assert resp.status == 204
    resp = await client.get(f"/jobs/{job_id}/file")
    assert resp.status == 404 and (await resp.json())["error"]["code"] == "not_found"


async def test_the_pages_endpoints_need_the_ingress_peer_and_the_header(client, manager, aiohttp_client):
    resp = await client.post("/jobs", json={"url": URL, "kind": "audio"})
    assert resp.status == 400 and (await resp.json())["error"]["code"] == "bad_request"
    resp = await client.delete("/jobs/x")
    assert resp.status == 400
    assert manager.snapshots() == []
    other = await aiohttp_client(create_app(manager, TOKEN, {}, "0.2.0", page_peers=frozenset({"172.30.32.2"})))
    headers = {"Content-Type": "application/json", "X-Requested-With": "media-fetcher"}
    for method, path in (("POST", "/jobs"), ("DELETE", "/jobs/x"), ("GET", "/jobs/x/file")):
        resp = await other.request(method, path, json={"url": URL, "kind": "audio"}, headers=headers)
        assert resp.status == 403 and (await resp.json())["error"]["code"] == "forbidden"
    assert manager.snapshots() == []


async def test_page_rows_show_failures_and_cancel_buttons(aiohttp_client, tmp_path):
    gate = threading.Event()
    scenario = Scenario(gate=gate)
    manager = JobManager(Fetcher(False, scenario.ydl_class(), resolve=resolve), tmp_path / "jobs", 2, 3600)
    manager.start()
    client = await aiohttp_client(create_app(manager, TOKEN, {}, "0.2.0", page_peers=frozenset({"127.0.0.1"})))
    resp = await client.post("/api/v1/jobs", json={"url": URL, "kind": "audio", "max_duration": 1}, headers=bearer())
    failed = (await resp.json())["id"]
    wait_for(manager, failed, "failed")
    resp = await client.post("/api/v1/jobs", json={"url": URL, "kind": "audio"}, headers=bearer())
    running = (await resp.json())["id"]
    wait_for(manager, running, "downloading")
    page = await (await client.get("/")).text()
    assert "too_long" in page and f'data-delete="{failed}">Remove<' in page
    assert f'data-delete="{running}">Cancel<' in page and "Download" not in page
    gate.set()
    wait_for(manager, running, "done")
    manager.shutdown()


# --- limits and headers ----------------------------------------------------------------------------
async def test_info_refuses_more_probes_than_max_jobs(aiohttp_client, tmp_path):
    """Probes beyond max_jobs get busy at once; a job still starts meanwhile."""
    release = threading.Event()
    entered = threading.Semaphore(0)

    class SlowFetcher(Fetcher):
        def info(self, url):
            entered.release()
            release.wait(5)
            return super().info(url)

    scenario = Scenario()
    manager = JobManager(SlowFetcher(False, scenario.ydl_class(), resolve=resolve), tmp_path / "jobs", 2, 3600)
    manager.start()
    client = await aiohttp_client(create_app(manager, TOKEN, {}, "0.3.0"))
    import asyncio
    probes = [asyncio.create_task(client.post("/api/v1/info", json={"url": URL}, headers=bearer())) for _ in range(2)]
    for _ in range(2):
        assert await asyncio.to_thread(entered.acquire, True, 5)
    resp = await client.post("/api/v1/info", json={"url": URL}, headers=bearer())
    assert resp.status == 429 and (await resp.json())["error"]["code"] == "busy"
    resp = await asyncio.wait_for(client.post("/api/v1/jobs", json={"url": URL, "kind": "audio"}, headers=bearer()), 3)
    assert resp.status == 201
    release.set()
    assert [(await p).status for p in probes] == [200, 200]
    assert (await client.post("/api/v1/info", json={"url": URL}, headers=bearer())).status == 200
    wait_for(manager, (await resp.json())["id"], "done")
    manager.shutdown()


async def test_no_job_starts_on_a_nearly_full_partition(aiohttp_client, tmp_path):
    scenario = Scenario()
    fetcher = Fetcher(False, scenario.ydl_class(), resolve=resolve, free_space=lambda _: 500 * 1024 * 1024)
    manager = JobManager(fetcher, tmp_path / "jobs", 2, 3600)
    manager.start()
    client = await aiohttp_client(create_app(manager, TOKEN, {}, "0.3.0"))
    resp = await client.post("/api/v1/jobs", json={"url": URL, "kind": "audio"}, headers=bearer())
    assert resp.status == 507 and (await resp.json())["error"]["code"] == "insufficient_storage"
    assert manager.snapshots() == []
    manager.shutdown()


async def test_a_token_beyond_ascii_is_refused_not_a_crash(manager, aiohttp_client):
    client = await aiohttp_client(create_app(manager, "schlüssel", {}, "0.3.0"))
    for value in ("Bearer ä", "Bearer wrong", "Bearer schlüssel"):
        resp = await client.get("/api/v1/jobs", headers={"Authorization": value.encode().decode("latin-1")})
        assert resp.status in (200, 401)
    resp = await client.get("/api/v1/jobs", headers={"Authorization": "Bearer ä".encode().decode("latin-1")})
    assert resp.status == 401


def test_bearer_compares_bytes():
    from types import SimpleNamespace

    from media_fetcher.api import bearer_ok
    assert bearer_ok(SimpleNamespace(headers={"Authorization": "Bearer schlüssel"}), "schlüssel")
    assert not bearer_ok(SimpleNamespace(headers={"Authorization": "Bearer ä"}), "abc")
    assert not bearer_ok(SimpleNamespace(headers={"Authorization": "Bearer abc"}), "")


async def test_every_answer_forbids_sniffing(client):
    for path in ("/api/v1/health", "/api/v1/jobs", "/api/v1/nothing", "/"):
        resp = await client.get(path)
        assert resp.headers["X-Content-Type-Options"] == "nosniff", path


@pytest.mark.parametrize(("title", "header"), [
    (None, 'attachment; filename="j.m4a"'),
    ("A talk", 'attachment; filename="A talk.m4a"'),
    ("Müller Straßenlied", "attachment; filename=\"Muller Straenlied.m4a\"; "
                           "filename*=UTF-8''M%C3%BCller%20Stra%C3%9Fenlied.m4a"),
    ("Песня", "attachment; filename=\"j.m4a\"; filename*=UTF-8''%D0%9F%D0%B5%D1%81%D0%BD%D1%8F.m4a"),
])
def test_file_names_beyond_ascii_have_a_fallback(title, header):
    from media_fetcher.api import disposition
    assert disposition(title, ".m4a", "j.m4a") == header


def test_the_form_offers_exactly_the_formats_of_the_api():
    from media_fetcher.api import _form
    from media_fetcher.fetcher import KINDS
    form = _form()
    for kind, formats in KINDS.items():
        for fmt in formats:
            assert f'value="{kind}/{fmt}"' in form
    assert form.count("<option") == sum(len(v) for v in KINDS.values())
    assert 'value="audio/m4a" selected' in form


async def test_the_page_runs_only_its_own_script_and_style(client):
    import re
    resp = await client.get("/")
    policy = resp.headers["Content-Security-Policy"]
    nonce = re.search(r"'nonce-([^']+)'", policy).group(1)
    page = await resp.text()
    assert page.count(f'nonce="{nonce}"') == 3 and "style=" not in page
    assert "default-src 'none'" in policy and "frame-ancestors 'self'" in policy and "connect-src 'self'" in policy
    again = (await client.get("/")).headers["Content-Security-Policy"]
    assert again != policy


async def test_the_page_shows_the_version_at_the_bottom_in_home_assistants_look(client):
    page = await (await client.get("/")).text()
    assert page.rstrip().endswith("</script></body></html>")
    assert '<footer class="version">Media Fetcher 0.2.0</footer><script' in page
    assert page.index("</main>") < page.index('<footer class="version">')
    # The shared stylesheet, unchanged, then the page's own rules
    from media_fetcher.api import HA_CSS
    shared = HA_CSS.read_text()
    assert shared.startswith("/* ha.css: Home Assistant's look") and shared in page
    assert page.index(shared) < page.index("section.fetch {")
    assert 'class="button filled" type="submit"' in page and 'class="icon-button"' in page


async def test_the_version_comes_from_the_build(manager, aiohttp_client):
    page = await (await aiohttp_client(create_app(manager, TOKEN, {}, "9.8.7", page_peers=frozenset({"127.0.0.1"}))
                  )).get("/")
    assert '<footer class="version">Media Fetcher 9.8.7</footer>' in await page.text()
