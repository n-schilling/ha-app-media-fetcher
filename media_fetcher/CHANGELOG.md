# Changelog

All notable changes to this app are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the app uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-11-06

### Added

- HTTP API under `/api/v1` on port 8099 in Home Assistant's app network: `health`, `info` for what a link holds, and jobs that fetch audio (m4a, mp3, opus, flac, wav or best) or video (mp4, mkv or best) from any link yt-dlp has an extractor for, with progress, a size limit by duration, a file download and cancelling
- yt-dlp with Deno as its JavaScript runtime, so sites that require it work, and ffmpeg for conversions
- Bearer token: a random one per start, announced to Home Assistant through the Supervisor's discovery, or a fixed one from the options
- Options `max_jobs`, `keep_minutes`, `allow_any_site` and `log_level`; the generic extractor stays off unless `allow_any_site` is on
- A status page behind ingress with versions, settings and the jobs
