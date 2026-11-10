"""yt-dlp: the options the service runs it with, what it reports, and how its
errors map to the API's error codes.

Everything yt-dlp says, warnings included, goes to the debug log. Its errors
arrive as DownloadError with a message such as "ERROR: [youtube] id: Video
unavailable"; classify() turns the message into one of the API's codes and
clean_message() into the first line without that prefix.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import shutil
import socket
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import yt_dlp
import yt_dlp.postprocessor
from yt_dlp.downloader.external import FFmpegFD
from yt_dlp.postprocessor import FFmpegPostProcessor
from yt_dlp.utils import DownloadCancelled, DownloadError

LOGGER = logging.getLogger("media_fetcher.yt_dlp")

AUDIO_FORMATS = ("m4a", "mp3", "opus", "flac", "wav", "best")
VIDEO_FORMATS = ("mp4", "mkv", "best")
KINDS = {"audio": AUDIO_FORMATS, "video": VIDEO_FORMATS}
# Postprocessors whose start means the job converts (yt-dlp's names)
CONVERTERS = frozenset({"ExtractAudio", "VideoRemuxer", "VideoConvertor", "Merger"})
# Stream protocols yt-dlp downloads itself, in the job's thread, where the
# address guard applies; the others need ffmpeg, rtmpdump or mplayer, which
# resolve names on their own
PROTOCOLS = frozenset({"http", "https", "m3u8", "m3u8_native", "http_dash_segments",
                       "http_dash_segments_generator", "ism", "f4m"})
MESSAGE_LENGTH = 200
MB = 1000 * 1000
# Free space the jobs leave on the data partition, which the app shares with
# Home Assistant's configuration, database and backups
RESERVE = 1024 * 1024 * 1024
# How often a download looks at the free space: every this many bytes
SPACE_CHECK_EVERY = 16 * 1024 * 1024
# A media file is binary from its first bytes; text there is a playlist or
# manifest, which ffmpeg would follow to other addresses on its own
SNIFF_LENGTH = 512

# Error codes of the API, by what yt-dlp says; the first match wins
UNSUPPORTED = re.compile(r"unsupported url|no suitable extractor", re.I)
LIVE = re.compile(r"\blive\b|premiere|will begin", re.I)
NOT_AVAILABLE = re.compile(
    r"unavailable|not available|available in your country|private video|removed|members[- ]only|sign in|log ?in|"
    r"age[- ]restrict|not found|does not exist|\b40[34]\b|forbidden|blocked|restricted|premium|"
    r"terminated|no video formats|requested format is not available|geo", re.I)
# "ERROR: " and then "[extractor] id: " in front of yt-dlp's messages
PREFIX = re.compile(r"^(?:ERROR:\s*)?(?:\[[^\]]+\]\s*(?:\S+:\s+(?!\d))?)?")


# ffmpeg converts local files only and opens no address a file names. yt-dlp
# takes input arguments per postprocessor ("<key>+ffmpeg_i"), so every one of
# its ffmpeg postprocessors gets them
FFMPEG_INPUT_ARGS = {
    f"{cls.pp_key().lower()}+ffmpeg_i": ["-protocol_whitelist", "file"]
    for cls in vars(yt_dlp.postprocessor).values()
    if isinstance(cls, type) and issubclass(cls, FFmpegPostProcessor) and cls is not FFmpegPostProcessor
}


class FetchError(Exception):
    """A failure with one of the API's error codes."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Report(Protocol):
    """What a running job hears from the fetcher."""

    def is_cancelled(self) -> bool: ...
    def set_state(self, state: str) -> None: ...
    def probed(self, info: dict) -> None: ...
    def progress(self, done: int, total: int | None) -> None: ...


HOSTNAME = re.compile(r"[\w.\-]+|[0-9A-Fa-f:.]+")


# The well-known NAT64 prefix: behind a NAT64 gateway, 64:ff9b::a00:1 is
# 10.0.0.1, although Python counts the prefix as global
NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _address(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address to judge: an IPv4 address embedded in IPv6 (mapped or
    NAT64) stands for itself."""
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return ip.ipv4_mapped
        if ip in NAT64:
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


def private_destination(url: str, resolve=socket.getaddrinfo) -> tuple[str, str] | None:
    """Why the link may not be fetched from, as (code, reason): its address
    part is not a plain host[:port] (user names, backslashes and the like,
    which parsers read differently), its host does not resolve, or it
    resolves to a loopback, private, link-local or other non-public
    address, such as a device in the home network or the Supervisor. None
    when every address is public."""
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return "invalid_url", "the link's address is malformed"
    if not host or "\\" in url or not HOSTNAME.fullmatch(host):
        return "invalid_url", "the link's address is no plain host"
    expected = (f"[{host}]" if ":" in host else host) + (f":{port}" if port is not None else "")
    if parts.netloc.lower() != expected.lower():
        return "invalid_url", "the link's address is no plain host"
    try:
        found = resolve(host, None)
    except (OSError, UnicodeError) as err:
        return "unsupported_url", f"{host} does not resolve ({err})"
    if not found:
        return "unsupported_url", f"{host} does not resolve"
    for entry in found:
        ip = _address(entry[4][0])
        if ip is not None and not ip.is_global:
            return "unsupported_url", f"{host} is at {ip}, not a public address"
    return None


# --- the guard at connection time ---------------------------------------------------------------
# yt-dlp follows redirects and fetches what pages point to, so the link's
# host is not the only one it connects to. Every name it resolves in a
# fetching thread goes through this guard, which refuses non-public
# addresses; other threads (the API) are not affected. urllib3, requests and websockets all resolve through
# socket.getaddrinfo and connect to what it returned.
_GUARD = threading.local()
_resolve_unguarded = socket.getaddrinfo


def _guarded_getaddrinfo(host, port, *args, **kwargs):
    found = _resolve_unguarded(host, port, *args, **kwargs)
    if getattr(_GUARD, "public_only", False):
        for entry in found:
            ip = _address(entry[4][0])
            if ip is not None and not ip.is_global:
                raise socket.gaierror(socket.EAI_FAIL, f"{host} is at {ip}, not a public address")
    return found


_ffmpeg_download = FFmpegFD.real_download


def _guarded_ffmpeg_download(self, filename, info_dict):
    # yt-dlp hands some HLS streams to ffmpeg halfway (ones it cannot
    # download itself); ffmpeg resolves names on its own, outside the guard
    if getattr(_GUARD, "public_only", False):
        raise RuntimeError("the stream needs ffmpeg to download it, which is not allowed")
    return _ffmpeg_download(self, filename, info_dict)


def install_guard() -> None:
    if socket.getaddrinfo is not _guarded_getaddrinfo:
        socket.getaddrinfo = _guarded_getaddrinfo
    if FFmpegFD.real_download is not _guarded_ffmpeg_download:
        FFmpegFD.real_download = _guarded_ffmpeg_download


@contextmanager
def public_only():
    """Within: every name this thread resolves must be public."""
    _GUARD.public_only = True
    try:
        yield
    finally:
        _GUARD.public_only = False


class YtdlpLog:
    """yt-dlp's logger: every message, warnings and errors included, goes to
    the debug log; the API reports errors itself."""

    def debug(self, msg: str) -> None:
        LOGGER.debug("%s", msg)

    info = warning = error = debug


def clean_message(text: str) -> str:
    """The first line of a yt-dlp message without "ERROR: [extractor] id: ",
    at most MESSAGE_LENGTH characters."""
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    line = PREFIX.sub("", lines[0], count=1) if lines else ""
    return (line or "unknown error")[:MESSAGE_LENGTH]


def classify(text: str) -> str:
    """The API's error code for an extraction failure."""
    if UNSUPPORTED.search(text):
        return "unsupported_url"
    if LIVE.search(text):
        return "live"
    if NOT_AVAILABLE.search(text):
        return "not_available"
    return "extract_failed"


def base_options(allow_any_site: bool) -> dict:
    return {
        "logger": YtdlpLog(),
        "quiet": True,
        "noprogress": True,
        "cachedir": False,
        "cookiefile": None,
        # A link to one video of a playlist is that video; a playlist itself
        # is not resolved, only recognised (flat, first entry only)
        "noplaylist": True,
        "extract_flat": "in_playlist",
        "playlist_items": "1",
        # The generic extractor fetches any address, the home network included
        "allowed_extractors": ["default"] if allow_any_site else ["default", "-generic"],
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        # yt-dlp's own downloaders only, and fragments in the job's thread:
        # the address guard covers what this thread resolves, not what a
        # subprocess or another thread would
        "external_downloader": {"default": "native"},
        "concurrent_fragment_downloads": 1,
        "postprocessor_args": FFMPEG_INPUT_ARGS,
    }


def probe_options(allow_any_site: bool) -> dict:
    return {**base_options(allow_any_site), "skip_download": True}


def job_options(allow_any_site: bool, kind: str, fmt: str, directory: Path, job_id: str) -> dict:
    opts = base_options(allow_any_site)
    opts["outtmpl"] = {"default": str(directory / f"{job_id}.%(ext)s")}
    if kind == "audio":
        opts["format"] = "bestaudio/best" if fmt == "best" else f"bestaudio[ext={fmt}]/bestaudio/best"
        # Copies the stream when it already has the codec (AAC into m4a),
        # converts otherwise
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": fmt}]
    elif fmt == "mp4":
        opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/bestvideo+bestaudio/best"
        opts["merge_output_format"] = "mp4"
        opts["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]
    elif fmt == "mkv":
        opts["format"] = "bestvideo+bestaudio/best"
        opts["merge_output_format"] = "mkv"
        opts["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": "mkv"}]
    else:
        opts["format"] = "bestvideo+bestaudio/best"
    return opts


def stream_protocols(info: dict) -> list[str]:
    """The protocols of the streams yt-dlp picked: of every part of a merge,
    or of the one format."""
    parts = info.get("requested_formats") or [info]
    return [str(part.get("protocol") or "") for part in parts]


def summary(info: dict) -> dict:
    """What the API tells about a link."""
    duration = info.get("duration")
    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "duration": int(round(duration)) if isinstance(duration, (int, float)) else None,
        "uploader": info.get("uploader") or info.get("channel") or info.get("uploader_id"),
        "extractor": info.get("extractor"),
        "webpage_url": info.get("webpage_url") or info.get("original_url"),
        "thumbnail": info.get("thumbnail"),
        "is_live": bool(info.get("is_live")),
    }


class Fetcher:
    """Runs yt-dlp. Blocking: call it from a thread. ydl_class stands in for
    yt_dlp.YoutubeDL in the tests."""

    def __init__(self, allow_any_site: bool, ydl_class=yt_dlp.YoutubeDL, resolve=socket.getaddrinfo, *,
                 max_size: int | None = None, free_space=None) -> None:
        self.allow_any_site = allow_any_site
        self.max_size = max_size          # bytes a job's file may have; None: no limit
        self._ydl_class = ydl_class
        self._resolve = resolve
        self._free_space = free_space or _free_space
        install_guard()

    def space_left(self, directory: Path) -> bool:
        """Whether a job may start: the data partition has more than RESERVE
        free."""
        return self._free_space(directory) > RESERVE

    def check(self, url: str) -> None:
        """Refuses a link to a non-public address before yt-dlp sees it."""
        refused = private_destination(url, self._resolve)
        if refused:
            raise FetchError(*refused)

    def info(self, url: str) -> dict:
        self.check(url)
        with self._ydl_class(probe_options(self.allow_any_site)) as ydl, public_only():
            return summary(self._extract(ydl, url))

    def _extract(self, ydl, url: str) -> dict:
        try:
            info = ydl.extract_info(url, download=False)
        except DownloadCancelled:
            raise
        except DownloadError as err:
            raise FetchError(classify(str(err)), clean_message(str(err))) from err
        except Exception as err:  # yt-dlp raises more than DownloadError on odd pages
            raise FetchError("extract_failed", clean_message(str(err) or type(err).__name__)) from err
        if not info:
            raise FetchError("extract_failed", "yt-dlp found nothing at this address")
        if info.get("_type") in ("playlist", "multi_video"):
            raise FetchError("unsupported_url", "playlists are not supported, give the link of one video")
        return info

    def download(self, url: str, kind: str, fmt: str, max_duration: int | None, directory: Path,
                 job_id: str, report: Report) -> Path:
        """Probes, then downloads and converts into directory; returns the
        file. Raises FetchError, or DownloadCancelled when the job was
        cancelled: the hooks check at every step of yt-dlp."""
        self.check(url)
        phase = "probing"
        final: list[str] = []
        # Why the job stops, when a hook stops it: yt-dlp passes only
        # DownloadCancelled through unchanged
        stopped: list[FetchError] = []
        finished_bytes = 0                # of the streams already downloaded
        next_space_check = 0

        def stop(code: str, message: str) -> None:
            stopped.append(FetchError(code, message))
            raise DownloadCancelled(message)

        def check_cancelled() -> None:
            if report.is_cancelled():
                raise DownloadCancelled("cancelled")

        def check_size(done: int, total: int | None) -> None:
            nonlocal next_space_check
            if self.max_size is not None:
                if max(done, total or 0) + finished_bytes > self.max_size:
                    stop("too_large", f"larger than the limit of {self.max_size // MB} MB")
            if done + finished_bytes >= next_space_check:
                next_space_check = done + finished_bytes + SPACE_CHECK_EVERY
                if not self.space_left(directory):
                    stop("insufficient_storage", "the data partition is nearly full")

        def on_progress(status: dict) -> None:
            nonlocal phase, finished_bytes
            check_cancelled()
            done = status.get("downloaded_bytes") or 0
            if status.get("status") == "downloading":
                if phase != "downloading":
                    phase = "downloading"
                    report.set_state("downloading")
                total = status.get("total_bytes") or status.get("total_bytes_estimate")
                check_size(done, total)
                report.progress(done, total)
            elif status.get("status") == "finished":
                total = status.get("total_bytes") or done
                check_size(done or total or 0, total)
                report.progress(done or total or 0, total)
                finished_bytes += done or total or 0

        def on_postprocess(status: dict) -> None:
            nonlocal phase
            check_cancelled()
            if status.get("status") != "started":
                return
            info = status.get("info_dict") or {}
            for path in (info.get("__files_to_merge") or [info.get("filepath")]):
                if path and not is_media(Path(path)):
                    stop("convert_failed", "the download is no media file")
            if status.get("postprocessor") in CONVERTERS and phase != "converting":
                phase = "converting"
                report.set_state("converting")

        opts = job_options(self.allow_any_site, kind, fmt, directory, job_id)
        opts["progress_hooks"] = [on_progress]
        opts["postprocessor_hooks"] = [on_postprocess]
        opts["post_hooks"] = [final.append]
        directory.mkdir(parents=True, exist_ok=True)
        with self._ydl_class(opts) as ydl, public_only():
            info = self._extract(ydl, url)
            if info.get("is_live"):
                raise FetchError("live", "live streams cannot be fetched")
            duration = info.get("duration")
            if max_duration and not isinstance(duration, (int, float)):
                raise FetchError("too_long", f"the site does not tell the length, the limit is {max_duration} s")
            if max_duration and duration > max_duration:
                raise FetchError("too_long", f"{int(duration)} s long, the limit is {max_duration} s")
            size = expected_size(info)
            if self.max_size is not None and size and size > self.max_size:
                raise FetchError("too_large", f"about {size // MB} MB, the limit is {self.max_size // MB} MB")
            odd = [proto for proto in stream_protocols(info) if proto not in PROTOCOLS]
            if odd:
                raise FetchError("unsupported_url", f"stream protocol {odd[0] or 'unknown'} is not supported")
            report.probed(info)
            check_cancelled()
            phase = "downloading"
            report.set_state("downloading")
            try:
                # The probe already extracted everything; yt-dlp takes the
                # result back the way it loads its own info files
                ydl.process_ie_result(ydl.sanitize_info(info, remove_private_keys=True), download=True)
            except DownloadCancelled:
                if stopped:
                    raise stopped[0] from None
                raise
            except DownloadError as err:
                raise FetchError(_phase_code(phase), clean_message(str(err))) from err
            except Exception as err:
                raise FetchError(_phase_code(phase), clean_message(str(err) or type(err).__name__)) from err
        path = Path(final[-1]) if final else _only_file(directory)
        if path is None or not path.is_file():
            raise FetchError(_phase_code(phase), "yt-dlp left no file")
        if self.max_size is not None and path.stat().st_size > self.max_size:
            # A conversion can make the file larger than the download
            raise FetchError("too_large", f"larger than the limit of {self.max_size // MB} MB")
        return path


def _free_space(directory: Path) -> int:
    """Free bytes on the partition of directory, or of its nearest existing
    parent."""
    for path in (directory, *directory.parents):
        if path.exists():
            return shutil.disk_usage(path).free
    return 0


def expected_size(info: dict) -> int | None:
    """The size the streams yt-dlp picked will have, as far as the site
    tells; None when it does not."""
    total = 0
    for part in info.get("requested_formats") or [info]:
        size = part.get("filesize") or part.get("filesize_approx")
        if not isinstance(size, (int, float)):
            return None
        total += int(size)
    return total or None


def is_media(path: Path) -> bool:
    """False for a file that starts as text: a playlist or manifest, not
    media. Media containers (mp4, Matroska, Ogg, FLAC, RIFF, MPEG streams)
    carry binary header bytes from the start."""
    try:
        with path.open("rb") as file:
            head = file.read(SNIFF_LENGTH)
    except OSError:
        return True               # gone or unreadable: ffmpeg reports that itself
    if not head:
        return True
    text = sum(1 for b in head if 32 <= b < 127 or b in (9, 10, 13))
    return text < len(head)


def _phase_code(phase: str) -> str:
    return {"converting": "convert_failed", "downloading": "download_failed"}.get(phase, "extract_failed")


def _only_file(directory: Path) -> Path | None:
    """The result when yt-dlp did not report it: the one file left behind."""
    files = [p for p in directory.iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
    return files[0] if len(files) == 1 else None


def versions(ydl_class=yt_dlp.YoutubeDL) -> dict:
    """yt-dlp, the JavaScript runtime it found, and ffmpeg; None for a
    missing one."""
    js = None
    try:
        with ydl_class({"logger": YtdlpLog(), "quiet": True}) as ydl:
            # yt-dlp's own view, from a private attribute; the fallback asks
            # deno itself
            runtime = ydl._js_runtimes.get("deno")
            info = runtime.info if runtime else None
            if info and info.supported is not False:
                js = f"{info.name} {info.version}"
    except Exception:
        js = _deno_version()
    try:
        ffmpeg = FFmpegPostProcessor.get_versions().get("ffmpeg") or None
    except Exception:
        ffmpeg = None
    return {"yt_dlp": yt_dlp.version.__version__, "js_runtime": js, "ffmpeg": ffmpeg}


def _deno_version() -> str | None:
    path = shutil.which("deno")
    if not path:
        return None
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.match(r"deno (\S+)", out.stdout)
    return f"deno {match.group(1)}" if match else None
