# Changelog

All notable changes to this app are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the app uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-11-07

### Changed

- Home Assistant Core needs no token: requests from its address in the app network (172.30.32.1, the host's side of it) pass without one. Every other client still needs the token from the `token` option; with the option empty there is no token at all, and the API answers Home Assistant alone
- The `token` option is thereby for other clients only; the random token per start is gone with the discovery it was announced through

### Removed

- Discovery through the Supervisor. Home Assistant took the service name for an integration domain and logged "Cannot find integration media_fetcher" at every start of the app; the Supervisor accepts only service names fixed in the app's config, and only Home Assistant Core may read the messages, so no other app could have used them. An integration finds the app through the Supervisor's list of installed apps instead, and the app no longer asks for the Supervisor API (`hassio_api`). A message that 0.1.0 left in the Supervisor stays until the app is uninstalled, and Home Assistant logs the line above once more at each of its own starts until then

## [0.1.0] - 2026-11-06

### Added

- HTTP API under `/api/v1` on port 8099 in Home Assistant's app network: `health`, `info` for what a link holds, and jobs that fetch audio (m4a, mp3, opus, flac, wav or best) or video (mp4, mkv or best) from any link yt-dlp has an extractor for, with progress, a size limit by duration, a file download and cancelling
- yt-dlp with Deno as its JavaScript runtime, so sites that require it work, and ffmpeg for conversions
- Bearer token: a random one per start, announced to Home Assistant through the Supervisor's discovery, or a fixed one from the options
- Options `max_jobs`, `keep_minutes`, `allow_any_site` and `log_level`; the generic extractor stays off unless `allow_any_site` is on, and links to non-public addresses (the home network, Home Assistant, the Supervisor) are refused, both before yt-dlp runs and for every name it resolves while it runs
- A web page behind ingress (administrators only): a form that fetches a link in the chosen format with a download link for the file, the jobs with their progress and a cancel or remove button, the versions and settings in use, and a download of the API definition
- The API is described in `openapi.yaml` (OpenAPI 3.1), served at `/api/v1/openapi.yaml` without a token
- Streams that only ffmpeg, rtmpdump or another program could fetch are refused; yt-dlp downloads with its own downloaders only, and Deno runs without network access
