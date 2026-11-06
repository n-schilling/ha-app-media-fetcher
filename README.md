# Media Fetcher app for Home Assistant

[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
![Home Assistant app](https://img.shields.io/badge/Home%20Assistant-app-41BDF5?logo=homeassistant&logoColor=white)
![Supports aarch64](https://img.shields.io/badge/aarch64-yes-green.svg)
![Supports amd64](https://img.shields.io/badge/amd64-yes-green.svg)

A Home Assistant app that fetches audio or video from links [yt-dlp](https://github.com/yt-dlp/yt-dlp) understands and serves the file over a small HTTP API in Home Assistant's app network, for integrations, scripts and automations. It brings [Deno](https://deno.com/) as yt-dlp's JavaScript runtime, which many sites require today and which Home Assistant's own images lack, and ffmpeg for conversions to m4a, mp3, opus, flac, wav, mp4 or mkv.

## Installation

1. Add the repository to your Home Assistant instance:

   [![Add the repository to My Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fn-schilling%2Fha-app-media-fetcher)

   Or add it manually: **Settings → Apps → Install app → ⋮ → Repositories**, then paste:

   ```text
   https://github.com/n-schilling/ha-app-media-fetcher
   ```

2. Open the app from the app store.
3. Install **Media Fetcher** and start it. The Supervisor builds the image on your machine; that takes a few minutes.

See [the documentation](media_fetcher/DOCS.md) for the options, the API, security notes and limitations. Changes are listed in the [changelog](media_fetcher/CHANGELOG.md).

## The API in short

Base `http://<host>:8099/api/v1`, JSON, `Authorization: Bearer <token>` on everything but `health`. The host is the app's name in the app network (e.g. `local-media-fetcher`); the token is random at every start and announced to Home Assistant through the Supervisor's discovery, or fixed through the `token` option.

| Endpoint | Does |
|---|---|
| `GET /health` | Versions of the app, yt-dlp, the JavaScript runtime and ffmpeg |
| `POST /info` `{"url"}` | Title, duration, uploader, thumbnail of a link, without downloading |
| `POST /jobs` `{"url", "kind": "audio"\|"video", "format", "max_duration"}` | Starts a fetch; `201 {"id", "state": "queued"}` |
| `GET /jobs`, `GET /jobs/{id}` | State, progress, title, file name and size, error |
| `GET /jobs/{id}/file` | The file, once the job is `done` |
| `DELETE /jobs/{id}` | Cancels a running job or deletes a finished one with its file |

```bash
H='Authorization: Bearer <token>'
curl -s -H "$H" -X POST http://local-media-fetcher:8099/api/v1/jobs -H 'Content-Type: application/json' \
  -d '{"url": "https://www.youtube.com/watch?v=jNQXAC9IVRw", "kind": "audio", "format": "m4a"}'
curl -s -H "$H" http://local-media-fetcher:8099/api/v1/jobs/<id>
curl -s -H "$H" -o zoo.m4a http://local-media-fetcher:8099/api/v1/jobs/<id>/file
```

Errors are `{"error": {"code": "…", "message": "…"}}`: `invalid_url`, `unsupported_url`, `not_available`, `live`, `extract_failed`, `too_long`, `download_failed`, `convert_failed`, `busy`, `not_ready`, `not_found`, `unauthorized`. The whole API is described in [`openapi.yaml`](media_fetcher/rootfs/usr/lib/media_fetcher/openapi.yaml), which the service also serves at `/api/v1/openapi.yaml`.

**Open web UI** on the app's page (administrators only) has a form to fetch a link and download the file, the list of jobs, and the versions in use.

## Security in short

The token is the only lock on the API, so keep it to the clients that need it; the random token per start never leaves the Supervisor. The API has no TLS: leave port 8099 unmapped unless a client outside Home Assistant needs it, and never expose it to the internet. Links to non-public addresses (your home network, Home Assistant, the Supervisor) are refused before yt-dlp runs. `allow_any_site` lets yt-dlp's generic extractor try any public site; it is off by default. The app does not check whether you are allowed to copy what you fetch; the site's terms and the owner's rights apply.

## Development

The service is a Python package in [`media_fetcher/rootfs/usr/lib/media_fetcher`](media_fetcher/rootfs/usr/lib/media_fetcher): `api.py` (aiohttp, the status page), `jobs.py` (threads, files, cleanup), `fetcher.py` (yt-dlp options and error mapping), `supervisor.py` (discovery). The tests replace `yt_dlp.YoutubeDL` with a fake and need no network:

```bash
pip install -r media_fetcher/requirements.txt pytest pytest-aiohttp pyyaml ruff
ruff check media_fetcher/rootfs/usr/lib/media_fetcher tests
pytest
```

A local image for a real run, with a fixed token:

```bash
docker build --build-arg BUILD_VERSION=dev -t media-fetcher:dev media_fetcher
mkdir -p /tmp/mf && echo '{"token": "dev-token", "max_jobs": 2, "keep_minutes": 60, "allow_any_site": false, "log_level": "debug"}' > /tmp/mf/options.json
docker run --rm -p 8099:8099 -v /tmp/mf:/data media-fetcher:dev
```

## Support

Questions, bugs and ideas: [open an issue](https://github.com/n-schilling/ha-app-media-fetcher/issues/new/choose) in this repository. Security problems: see [SECURITY.md](SECURITY.md).

## License

[Apache License 2.0](LICENSE)
