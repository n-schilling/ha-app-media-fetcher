# Changelog

All notable changes to this app are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the app uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.1] - 2026-11-11

### Changed

- The page speaks the language of Home Assistant's interface, German or English, no longer English only: it reads the user's language from Home Assistant around its frame, keeps it in a cookie for the app's ingress path and reloads once when it changes. Without that cookie it follows the browser
- CI builds and tests aarch64 natively on an arm64 runner instead of emulating it, the AppArmor test included, and no longer runs a second time on `main`: the release workflow starts on the push to `main` and checks that CI passed for that commit before it tags

## [0.4.0] - 2026-11-11

### Added

- The page's header is the same as on the author's other apps: the time its data were read (*As of …*, updated with the list), a refresh button and the gear to the app's options

### Fixed

- The page's update every 10 seconds no longer reports a content security policy violation in the browser's console

## [0.3.1] - 2026-11-11

### Security

- The service no longer runs as root, but as a user of its own, to which the app hands its data folder on start
- A narrower AppArmor profile: only the capabilities, networks, programs and paths the app uses, tested on a real installation; ffmpeg, ffprobe and Deno keep running without network

## [0.3.0] - 2026-11-10

### Added

- Option `trust_home_assistant` (default on): off, Home Assistant Core needs the token like every other client, and nobody gets into the API without a token
- Option `max_size_mb` (default 2000): a job fails with `too_large` when the site announces a larger file, the download grows past it, or a conversion would make the file larger (ffmpeg stops writing at the limit)
- No job starts, and running ones stop, while less than 1 GiB is free on Home Assistant's data partition: `507 insufficient_storage` when starting, `insufficient_storage` as a job's error
- The web page shows the app's version at the bottom and follows Home Assistant's look with the stylesheet all apps of this author share; on a phone each job is one block
- `POST /info` answers `429 busy` while as many links as `max_jobs` are being looked at
- Requires Home Assistant 2026.11 or later

### Changed

- `max_duration` also refuses a link whose site does not tell its length (`too_long`), instead of letting it through
- ffmpeg, ffprobe and Deno run under an AppArmor child profile without network, and ffmpeg reads local files only; a download that starts as text (a playlist or manifest in place of media) fails as `convert_failed` before ffmpeg sees it
- Addresses in the NAT64 prefix `64:ff9b::/96` count as the IPv4 address they carry, so they cannot reach the home network
- Files are served with the content type of the fixed format table only (else `application/octet-stream`), every answer carries `X-Content-Type-Options: nosniff`, and the page a content security policy
- A download named after a title beyond ASCII gets an ASCII `filename` and the full title in `filename*`
- The app stops within 5 seconds, also while a conversion runs
- The base image is Home Assistant's `base-debian` of 2026.11, pinned by digest; Python packages are pinned with hashes

### Fixed

- Starting a job no longer waits behind slow `info` requests
- Removing a job in the moment it finishes no longer answers 204 and keeps it
- A token with characters beyond ASCII no longer causes a 500
- Deleting and expiring large files no longer blocks the API

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
