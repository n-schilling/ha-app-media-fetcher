# Media Fetcher

A small HTTP service in Home Assistant's app network that fetches audio or video from links [yt-dlp](https://github.com/yt-dlp/yt-dlp) understands and hands the file to whoever asked: an integration, a script, an automation. It brings what yt-dlp needs on Home Assistant OS and the Supervisor's images do not have: [Deno](https://deno.com/) as a JavaScript runtime, which many sites require today, and ffmpeg for conversions.

It is not tied to one site or one use: `info` tells what a link holds, a job fetches it as m4a, mp3, opus, flac, wav, mp4 or mkv, and the file is yours to pick up for a while.

## Requirements

- Home Assistant OS or Supervised, aarch64 or amd64
- Internet access for the sites you fetch from

## How clients reach it

The service listens on port 8099 inside Home Assistant's app network, where other apps and Home Assistant Core reach it by the app's host name (the slug with dashes, e.g. `local-media-fetcher` for a locally installed copy). Every request but `health` needs the API token as `Authorization: Bearer <token>`.

**Discovery.** At every start the app tells the Supervisor where it is and which token it takes:

```json
{"service": "media_fetcher", "config": {"host": "<host name>", "port": 8099, "token": "<token>"}}
```

Home Assistant hands that to an integration with the domain `media_fetcher`, or an app reads it through the Supervisor's discovery API. With the `token` option empty (the default) the token is random and new at every start, so it only exists in that message: nobody who does not go through the Supervisor can know it.

**A fixed token.** For clients that do not learn the token through discovery, set `token` in the options; then it stays the same across restarts. Treat it like a password.

**Outside the app network.** The API is not reachable from your network unless you map the port under **Network** on the app's page. Do that only with a fixed token and a network you trust: the token crosses it in clear text, there is no TLS.

## Options

| Option | Default | Meaning |
|---|---|---|
| `token` | | API token. Empty: a random one at every start, announced through discovery only. Set: a fixed one, for clients that cannot use discovery |
| `max_jobs` | `2` | Jobs that run at the same time; further requests get `429 busy` until one finishes |
| `keep_minutes` | `60` | Minutes a finished job and its file stay available; then they are deleted. Everything is deleted when the app starts |
| `allow_any_site` | `false` | Also use yt-dlp's *generic* extractor, which tries to fetch media from any public site. Off, only sites yt-dlp has an extractor for are fetched |
| `log_level` | `info` | How much the app writes to its log: `debug` (with every message of yt-dlp, warnings included), `info`, `warning` or `error` |

## API

Base: `http://<host>:8099/api/v1`, JSON in and out. Errors come as

```json
{"error": {"code": "not_available", "message": "Video unavailable. This video is not available"}}
```

with yt-dlp's message shortened to its first line, at most 200 characters, without the `ERROR: [site] id:` prefix. Without a valid token every endpoint but `health` answers `401 unauthorized`.

The whole API is described in [`openapi.yaml`](rootfs/usr/lib/media_fetcher/openapi.yaml) (OpenAPI 3.1), which the service also serves at `GET /api/v1/openapi.yaml` without a token.

### `GET /health`

No token needed. The versions the service runs with; `js_runtime` or `ffmpeg` is `null` when missing.

```json
{"ok": true, "version": "0.1.0", "yt_dlp": "2026.10.26", "js_runtime": "deno 2.9.8", "ffmpeg": "7.1.2"}
```

### `POST /info`

What a link holds, without downloading. Body: `{"url": "https://…"}`.

```json
{"id": "jNQXAC9IVRw", "title": "Me at the zoo", "duration": 19, "uploader": "jawed", "extractor": "youtube",
 "webpage_url": "https://www.youtube.com/watch?v=jNQXAC9IVRw", "thumbnail": "https://…", "is_live": false}
```

`duration` is in whole seconds, or `null` when the site does not say. A link to one video of a playlist is that video; a playlist itself is not resolved and answers `unsupported_url`.

| Status | `code` | Meaning |
|---|---|---|
| 400 | `invalid_url` | Not an http or https address |
| 422 | `unsupported_url` | No extractor for this address, a playlist, a host at a non-public address, or a stream only another program could fetch |
| 422 | `not_available` | The site refuses it: unavailable, private, removed, members only, sign-in required, not in your country |
| 422 | `live` | A live event that has not started, or a premiere |
| 422 | `extract_failed` | Anything else yt-dlp reports |

### `POST /jobs`

Starts a fetch. Body:

| Field | Meaning |
|---|---|
| `url` | The link |
| `kind` | `audio` or `video` |
| `format` | Audio: `m4a` (default), `mp3`, `opus`, `flac`, `wav` or `best`. Video: `mp4` (default), `mkv` or `best` |
| `max_duration` | Optional: seconds; a longer video fails with `too_long` before anything is downloaded |

Answers `201 {"id": "…", "state": "queued"}`, or `429 busy` while `max_jobs` jobs are running; `400 invalid_request` names a wrong field, `400 invalid_url` and `422 unsupported_url` mean what they mean for `info`.

Audio takes the best audio stream, preferring one that already has the wanted format, and converts it with ffmpeg where needed; a stream that already has the codec is copied, not re-encoded (AAC into m4a, for example). `best` keeps the stream's own format (m4a, opus, …). Video takes the best video and audio streams, merged into the wanted container; `mp4` prefers mp4 and m4a streams and remuxes without re-encoding, `best` keeps whatever container the merge produces.

### `GET /jobs` and `GET /jobs/{id}`

All jobs, oldest first, or one job:

```json
{"id": "3f9a0c1b2d4e5f60", "state": "downloading", "url": "https://…", "kind": "audio", "format": "m4a",
 "title": "Me at the zoo", "duration": 19, "done_bytes": 120000, "total_bytes": 310000,
 "filename": null, "size": null, "content_type": null, "error": null,
 "created": "2026-11-06T21:05:12Z", "finished": null}
```

| `state` | Meaning |
|---|---|
| `queued` | Waiting for a thread |
| `probing` | yt-dlp looks at the link; `title` and `duration` appear when it is done |
| `downloading` | `done_bytes` and `total_bytes` (or `null` when the size is unknown) follow the download of the current stream |
| `converting` | ffmpeg converts, merges or remuxes |
| `done` | `filename`, `size` and `content_type` are set; the file is ready |
| `failed` | `error` says why: the codes of `info`, plus `too_long`, `download_failed` and `convert_failed` |
| `cancelled` | Deleted while running; gone from the list as soon as its thread stopped |

A finished job disappears `keep_minutes` after `finished`, its file with it; `GET` then answers `404`.

### `GET /jobs/{id}/file`

The file, with `Content-Type` by its format (`audio/mp4` for m4a, `audio/mpeg`, `audio/ogg` for opus, `audio/flac`, `audio/wav`, `video/mp4`, `video/x-matroska`), `Content-Length` and `Content-Disposition: attachment; filename="<id>.<ext>"`. Ranges are supported. `409 not_ready` while the job is not done, `404` for an unknown job.

### `DELETE /jobs/{id}`

`204`. A running job is cancelled: the download stops at its next progress step, a running conversion when it ends; its files are deleted. A finished job is deleted with its file.

### Example

```bash
H='Authorization: Bearer <token>'
curl -s -H "$H" -X POST http://local-media-fetcher:8099/api/v1/jobs \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://www.youtube.com/watch?v=jNQXAC9IVRw", "kind": "audio", "format": "m4a", "max_duration": 3600}'
# {"id": "3f9a0c1b2d4e5f60", "state": "queued"}
curl -s -H "$H" http://local-media-fetcher:8099/api/v1/jobs/3f9a0c1b2d4e5f60
# … "state": "done", "filename": "3f9a0c1b2d4e5f60.m4a", "size": 303921 …
curl -s -H "$H" -o zoo.m4a http://local-media-fetcher:8099/api/v1/jobs/3f9a0c1b2d4e5f60/file
```

## The web page

**Open web UI** on the app's page opens a page that only answers Home Assistant's ingress proxy, so only administrators see it. It never shows the token.

- **Fetch:** paste a link, pick a format (audio m4a, mp3, opus, flac, wav or the source's own format; video mp4, mkv or the source's own container) and press *Fetch*. The job appears in the list below; when it is done, its row has a **Download** link that saves the file under the media's title.
- **Jobs:** every job with its state, progress or result, and a *Cancel* or *Remove* button. The list refreshes every 10 seconds; jobs started through the API are there too.
- **Service and Settings:** the versions the service runs with, with a link to download the API definition (`openapi.yaml`), and the options in use.

The page talks to endpoints of its own that take no token; the ingress session is their lock, and the ones that change something take JSON with an `X-Requested-With` header only, which a form on another site cannot send.

## Security

- **The token is the only lock.** Whoever has it can make the app fetch anything from the internet and read the files of every job. The random token per start is the safer choice; a fixed one belongs in a password manager, not in a script that others can read. The token never appears in the log.
- **Stay in the app network.** Map port 8099 to the host only when a client outside Home Assistant needs it, and never expose it to the internet: there is no TLS and no rate limit.
- **Your home network is out of bounds.** Before yt-dlp runs, the app resolves the link's host and refuses the link when the address part is not a plain `host[:port]`, when the host does not resolve, or when any of its addresses is not public: loopback, private ranges, link-local, the Supervisor's network. While yt-dlp runs, every name it resolves goes through the same check, so a redirect or a page that points to a device in your network ends in an error as well. So a link cannot make the app read from your network or from Home Assistant itself, whatever `allow_any_site` says.
- **`allow_any_site` widens the reach.** yt-dlp's generic extractor tries any public site it is given and follows what it finds there. Leave it off unless you need sites without an extractor, and then give the token only to clients you trust.
- **What you may fetch is your business.** The app does not check whether you are allowed to copy a video or a track; the terms of the site and the rights of the owner apply. It sends no cookies and no login, so what needs an account stays out of reach.
- Jobs run with yt-dlp's cache off, without cookies, only with the extractors of yt-dlp's default set (minus the generic one), and only with yt-dlp's own downloaders: a stream that would need ffmpeg, rtmpdump or another program to fetch it (RTMP, RTSP, MMS and other rare protocols) is refused as `unsupported_url`, and an HLS stream that yt-dlp would hand to ffmpeg halfway fails with `download_failed`, because those programs resolve names on their own, outside the address check. Deno runs without network access. Files are kept under the app's data, excluded from backups, and deleted after `keep_minutes` and at every start.

## Known limitations

- Playlists are not fetched; give the link of one video.
- Live streams are not fetched; `info` reports them with `is_live: true`.
- `done_bytes` and `total_bytes` follow one stream at a time: a video download restarts them for its audio stream.
- Cancelling takes effect at yt-dlp's next progress step; a conversion that is already running finishes first, then its files are deleted.
- yt-dlp needs updates when sites change; the app pins its version and gets a new release when yt-dlp has one worth it. If a site stops working, check for an update of the app first.
- The image is large (about 1 GB unpacked): Debian's ffmpeg with its libraries, Deno and yt-dlp.

## Support

Questions, bugs and ideas: open an issue at https://github.com/n-schilling/ha-app-media-fetcher/issues. Please add the app version, the app log with `log_level` set to `debug` (it then holds yt-dlp's messages) and the kind of link, not the link itself if it is private.
