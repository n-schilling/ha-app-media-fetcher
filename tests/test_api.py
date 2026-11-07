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
    assert await resp.read() == b"x" * 1000
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
    assert await resp.read() == b"x" * 1000
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
