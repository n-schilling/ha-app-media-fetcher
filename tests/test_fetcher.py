"""The yt-dlp wrapper: options, error mapping, what a download reports."""

import threading
from pathlib import Path

import pytest
from conftest import VIDEO, Scenario, resolve
from yt_dlp.utils import DownloadCancelled, DownloadError

from media_fetcher import fetcher as f
from media_fetcher.fetcher import Fetcher, FetchError


@pytest.mark.parametrize(("raw", "cleaned"), [
    ("ERROR: [youtube] lC3CNRlCcz8: Video unavailable. This video is not available",
     "Video unavailable. This video is not available"),
    ("ERROR: [youtube] abc: Private video. If the owner\nSecond line", "Private video. If the owner"),
    ("ERROR: Unsupported URL: https://example.test/x", "Unsupported URL: https://example.test/x"),
    ("ERROR: No suitable extractor found for URL https://example.test/x",
     "No suitable extractor found for URL https://example.test/x"),
    ("ERROR: [youtube] abc: HTTP Error 403: Forbidden", "HTTP Error 403: Forbidden"),
    ("[youtube] Requested format is not available", "Requested format is not available"),
    ("ERROR: Postprocessing: audio conversion failed: x", "Postprocessing: audio conversion failed: x"),
    ("", "unknown error"),
    ("ERROR: [x] y: " + "z" * 300, "z" * 200),
])
def test_messages_lose_their_prefix(raw, cleaned):
    assert f.clean_message(raw) == cleaned


@pytest.mark.parametrize(("message", "code"), [
    ("ERROR: Unsupported URL: https://x", "unsupported_url"),
    ("ERROR: No suitable extractor found for URL https://x", "unsupported_url"),
    ("ERROR: [youtube] a: This live event will begin in 3 hours.", "live"),
    ("ERROR: [youtube] a: Premieres in 2 hours", "live"),
    ("ERROR: [youtube] a: Video unavailable. This video is not available", "not_available"),
    ("ERROR: [youtube] a: Private video. If the owner of this video has granted you access", "not_available"),
    ("ERROR: [youtube] a: Sign in to confirm your age", "not_available"),
    ("ERROR: [youtube] a: Join this channel to get access to members-only content", "not_available"),
    ("ERROR: [youtube] a: The uploader has not made this video available in your country", "not_available"),
    ("ERROR: [youtube] a: Unable to extract player version; please report this issue", "extract_failed"),
    ("ERROR: Unable to download webpage: timed out", "extract_failed"),
])
def test_errors_get_a_code(message, code):
    assert f.classify(message) == code


def test_base_options_keep_the_generic_extractor_out():
    opts = f.base_options(False)
    assert opts["allowed_extractors"] == ["default", "-generic"]
    assert opts["external_downloader"] == {"default": "native"} and opts["concurrent_fragment_downloads"] == 1
    assert f.base_options(True)["allowed_extractors"] == ["default"]
    assert opts["cachedir"] is False and opts["noplaylist"] is True and opts["cookiefile"] is None
    assert opts["extract_flat"] == "in_playlist" and opts["quiet"] and opts["noprogress"]
    assert hasattr(opts["logger"], "warning")


def test_audio_job_options():
    opts = f.job_options(False, "audio", "m4a", Path("/data/jobs/j1"), "j1")
    assert opts["format"] == "bestaudio[ext=m4a]/bestaudio/best"
    assert opts["postprocessors"] == [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}]
    assert opts["outtmpl"] == {"default": "/data/jobs/j1/j1.%(ext)s"}
    assert f.job_options(False, "audio", "best", Path("/d"), "j")["format"] == "bestaudio/best"
    assert f.job_options(False, "audio", "mp3", Path("/d"), "j")["postprocessors"][0]["preferredcodec"] == "mp3"


def test_video_job_options():
    mp4 = f.job_options(False, "video", "mp4", Path("/d"), "j")
    assert mp4["format"].startswith("bestvideo[ext=mp4]+bestaudio[ext=m4a]/") and mp4["merge_output_format"] == "mp4"
    assert mp4["postprocessors"] == [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]
    mkv = f.job_options(False, "video", "mkv", Path("/d"), "j")
    assert mkv["merge_output_format"] == "mkv" and mkv["postprocessors"][0]["preferedformat"] == "mkv"
    best = f.job_options(False, "video", "best", Path("/d"), "j")
    assert best["format"] == "bestvideo+bestaudio/best" and "postprocessors" not in best


def test_summary_of_a_video():
    assert f.summary(VIDEO) == {
        "id": "abc123", "title": "A talk", "duration": 417, "uploader": "Someone", "extractor": "youtube",
        "webpage_url": "https://www.youtube.com/watch?v=abc123", "thumbnail": "https://i.example.test/abc123.jpg",
        "is_live": False}
    assert f.summary({"id": "x", "channel": "C", "duration": 19.6, "is_live": True})["uploader"] == "C"
    assert f.summary({"id": "x", "duration": 19.6})["duration"] == 20
    assert f.summary({"id": "x"})["duration"] is None


def test_info_of_a_link(scenario, fetcher):
    assert fetcher.info("https://www.youtube.com/watch?v=abc123")["title"] == "A talk"
    assert scenario.extracted == ["https://www.youtube.com/watch?v=abc123"]
    assert scenario.instances[0].params["skip_download"] is True


def test_info_refuses_a_playlist():
    scenario = Scenario(info={"_type": "playlist", "id": "PL1", "title": "List", "entries": []})
    with pytest.raises(FetchError) as err:
        Fetcher(False, scenario.ydl_class(), resolve=resolve).info("https://www.youtube.com/playlist?list=PL1")
    assert err.value.code == "unsupported_url"


def test_info_maps_extraction_errors():
    scenario = Scenario(extract_error="ERROR: [youtube] abc: Video unavailable. This video is not available")
    with pytest.raises(FetchError) as err:
        Fetcher(False, scenario.ydl_class(), resolve=resolve).info("https://www.youtube.com/watch?v=abc")
    assert (err.value.code, err.value.message) == ("not_available", "Video unavailable. This video is not available")


class Recorder:
    """Stands in for the job: records what the fetcher reports."""

    def __init__(self, cancel_after: int | None = None):
        self.states = []
        self.progress_seen = []
        self.info = None
        self.cancel_after = cancel_after

    def is_cancelled(self):
        return self.cancel_after is not None and len(self.progress_seen) >= self.cancel_after

    def set_state(self, state):
        self.states.append(state)

    def probed(self, info):
        self.info = info

    def progress(self, done, total):
        self.progress_seen.append((done, total))


def test_download_reports_states_progress_and_the_file(scenario, fetcher, tmp_path):
    report = Recorder()
    path = fetcher.download("https://www.youtube.com/watch?v=abc123", "audio", "m4a", None, tmp_path / "j1", "j1", report)
    assert path == tmp_path / "j1" / "j1.m4a" and path.stat().st_size == 1000
    assert report.states == ["downloading", "converting"]
    assert report.progress_seen == [(0, 1000), (500, 1000), (1000, 1000)]
    assert report.info["title"] == "A talk"
    assert scenario.processed == 1 and scenario.sanitized is True
    params = scenario.instances[0].params
    assert params["postprocessors"][0]["preferredcodec"] == "m4a" and len(params["progress_hooks"]) == 1


def test_too_long_fails_before_anything_is_downloaded(scenario, fetcher, tmp_path):
    with pytest.raises(FetchError) as err:
        fetcher.download("https://x.test/v", "audio", "m4a", 300, tmp_path / "j", "j", Recorder())
    assert err.value.code == "too_long" and "417" in err.value.message
    assert scenario.processed == 0
    assert fetcher.download("https://x.test/v", "audio", "m4a", 417, tmp_path / "k", "k", Recorder()).exists()


def test_a_live_stream_is_refused(tmp_path):
    scenario = Scenario(info={**VIDEO, "is_live": True, "duration": None})
    with pytest.raises(FetchError) as err:
        Fetcher(False, scenario.ydl_class(), resolve=resolve).download("https://x.test/v", "audio", "m4a", None, tmp_path / "j", "j", Recorder())
    assert err.value.code == "live" and scenario.processed == 0


@pytest.mark.parametrize(("scenario", "code", "message"), [
    (Scenario(download_error="ERROR: unable to download video data: HTTP Error 403: Forbidden"),
     "download_failed", "unable to download video data: HTTP Error 403: Forbidden"),
    (Scenario(convert_error="audio conversion failed: boom"), "convert_failed",
     "Postprocessing: audio conversion failed: boom"),
    (Scenario(extract_error="ERROR: [youtube] abc: This live event will begin in 2 hours."), "live",
     "This live event will begin in 2 hours."),
])
def test_failures_carry_the_phase(scenario, code, message, tmp_path):
    with pytest.raises(FetchError) as err:
        Fetcher(False, scenario.ydl_class(), resolve=resolve).download("https://x.test/v", "audio", "m4a", None, tmp_path / "j", "j", Recorder())
    assert (err.value.code, err.value.message) == (code, message)


def test_cancelling_stops_at_the_next_hook(scenario, fetcher, tmp_path):
    report = Recorder(cancel_after=1)
    with pytest.raises(DownloadCancelled):
        fetcher.download("https://x.test/v", "audio", "m4a", None, tmp_path / "j", "j", report)
    assert report.progress_seen == [(0, 1000)]
    assert not list((tmp_path / "j").glob("*.m4a"))


def test_cancelling_before_the_download(fetcher, tmp_path):
    report = Recorder(cancel_after=0)
    with pytest.raises(DownloadCancelled):
        fetcher.download("https://x.test/v", "audio", "m4a", None, tmp_path / "j", "j", report)
    assert report.states == [] and report.info is not None


def test_the_file_is_found_without_a_post_hook(tmp_path):
    scenario = Scenario(converter=None)
    fetcher = Fetcher(False, scenario.ydl_class(), resolve=resolve)
    path = fetcher.download("https://x.test/v", "video", "best", None, tmp_path / "j", "j", Recorder())
    assert path.name == "j.webm"


def test_versions_name_what_is_there(scenario):
    found = f.versions(scenario.ydl_class())
    assert set(found) == {"yt_dlp", "js_runtime", "ffmpeg"}
    assert found["yt_dlp"].count(".") == 2
    assert found["js_runtime"] is None or found["js_runtime"].startswith("deno ")


def test_gate_is_honoured_by_the_fake(tmp_path):
    """The fake waits at its gate, so the job tests can cancel in time."""
    gate = threading.Event()
    scenario = Scenario(gate=gate)
    report = Recorder()
    done = threading.Event()

    def run():
        Fetcher(False, scenario.ydl_class(), resolve=resolve).download("https://x.test/v", "audio", "m4a", None, tmp_path / "j", "j", report)
        done.set()

    threading.Thread(target=run, daemon=True).start()
    assert not done.wait(0.2) and report.progress_seen == [(0, 1000)]
    gate.set()
    assert done.wait(2)


@pytest.mark.parametrize("url", ["https://www.youtube.com/watch?v=a", "https://x.test/v", "https://v6.test/v",
                                 "https://93.184.216.34/v", "https://X.test:8443/v", "https://[2606:2800:220:1:248:1893:25c8:1946]:443/v"])
def test_public_destinations_pass(url):
    assert f.private_destination(url, resolve) is None


@pytest.mark.parametrize("url", ["http://nas.lan/Mediasite/Play/1", "http://localhost:8123/", "http://127.0.0.1/x",
                                 "http://supervisor/discovery", "http://172.30.32.2/x", "http://[::1]/x",
                                 "http://mapped.test/x", "http://10.0.0.1/x", "http://169.254.169.254/x",
                                 "http://100.64.0.1/x", "http://0.0.0.0/x"])
def test_private_destinations_are_refused(url, scenario):
    code, reason = f.private_destination(url, resolve)
    assert code == "unsupported_url" and "not a public address" in reason
    fetcher = Fetcher(False, scenario.ydl_class(), resolve=resolve)
    with pytest.raises(FetchError) as err:
        fetcher.info(url)
    assert err.value.code == "unsupported_url" and scenario.extracted == []


def test_an_unknown_host_is_refused_too():
    code, reason = f.private_destination("https://unknown.test/v", resolve)
    assert code == "unsupported_url" and "does not resolve" in reason


@pytest.mark.parametrize("url", ["http://x.test\\@nas.lan/", "http://user@x.test/", "http://user:pw@x.test/",
                                 "http://x.test:/", "http://x.test:99999/", "http://x.test%2f@nas.lan/",
                                 "http://x.test /", "http://[x.test]/", "http://x.test:80:80/"])
def test_odd_addresses_are_refused_before_anyone_parses_them(url):
    code, reason = f.private_destination(url, resolve)
    assert code == "invalid_url", (url, reason)


def test_the_guard_refuses_private_addresses_while_yt_dlp_runs(tmp_path):
    """Whatever yt-dlp connects to in a fetching thread (redirects, what a
    page points to) is checked at resolution; other threads are not."""
    import socket

    seen = {}

    class ResolvingYoutubeDL:
        def __init__(self, params):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            try:
                socket.getaddrinfo("localhost", 80)
            except socket.gaierror as err:
                seen["error"] = str(err)
                raise DownloadError(f"ERROR: Unable to download webpage: {err}") from err
            return {"id": "x", "title": "t"}

    fetcher = Fetcher(False, ResolvingYoutubeDL, resolve=resolve)
    with pytest.raises(FetchError) as err:
        fetcher.info("https://x.test/v")
    assert "not a public address" in seen["error"] and err.value.code == "extract_failed"
    # Outside a fetch the same thread resolves anything
    assert socket.getaddrinfo("localhost", 80)


@pytest.mark.parametrize("info", [
    {**VIDEO, "protocol": "rtmp"},
    {**VIDEO, "protocol": "rtsp"},
    {**VIDEO, "protocol": None},
    {**VIDEO, "requested_formats": [{"protocol": "https"}, {"protocol": "rtmp_ffmpeg"}]},
])
def test_streams_that_need_another_program_are_refused(info, tmp_path):
    scenario = Scenario(info=info)
    with pytest.raises(FetchError) as err:
        Fetcher(False, scenario.ydl_class(), resolve=resolve).download("https://x.test/v", "audio", "m4a", None,
                                                                       tmp_path / "j", "j", Recorder())
    assert err.value.code == "unsupported_url" and "protocol" in err.value.message and scenario.processed == 0


@pytest.mark.parametrize("info", [
    {**VIDEO, "protocol": "websocket_frag"},
    {**VIDEO, "protocol": "bunnycdn"},
])
def test_streams_of_other_downloaders_are_refused_too(info, tmp_path):
    scenario = Scenario(info=info)
    with pytest.raises(FetchError) as err:
        Fetcher(False, scenario.ydl_class(), resolve=resolve).download("https://x.test/v", "audio", "m4a", None,
                                                                       tmp_path / "j", "j", Recorder())
    assert err.value.code == "unsupported_url"


def test_ffmpeg_cannot_download_while_a_fetch_runs(scenario):
    from yt_dlp.downloader.external import FFmpegFD

    Fetcher(False, scenario.ydl_class(), resolve=resolve)
    downloader = FFmpegFD.__new__(FFmpegFD)
    with f.public_only(), pytest.raises(RuntimeError, match="ffmpeg"):
        downloader.real_download("x.mp4", {"url": "https://x.test/stream.m3u8"})


@pytest.mark.parametrize("info", [
    {**VIDEO, "protocol": "m3u8_native"},
    {**VIDEO, "protocol": "http_dash_segments"},
    {**VIDEO, "requested_formats": [{"protocol": "https"}, {"protocol": "m3u8"}]},
])
def test_streams_yt_dlp_downloads_itself_pass(info, tmp_path):
    scenario = Scenario(info=info)
    path = Fetcher(False, scenario.ydl_class(), resolve=resolve).download("https://x.test/v", "audio", "m4a", None,
                                                                          tmp_path / "j", "j", Recorder())
    assert path.exists() and f.stream_protocols(info)


# --- NAT64 ---------------------------------------------------------------------------------------
@pytest.mark.parametrize(("address", "public"), [("64:ff9b::a00:1", False), ("64:ff9b::c0a8:101", False),
                                                 ("64:ff9b::5db8:d822", True), ("::ffff:10.0.0.9", False)])
def test_embedded_ipv4_addresses_are_judged_by_themselves(address, public):
    assert f._address(address).is_global is public
    refused = f.private_destination(f"http://[{address}]/x", resolve)
    assert (refused is None) is public


# --- sizes, free space, unknown lengths ----------------------------------------------------------
GB = 1024 * 1024 * 1024


def sized(scenario, max_size=None, free=10 * GB):
    return Fetcher(False, scenario.ydl_class(), resolve=resolve, max_size=max_size, free_space=lambda _: free)


def test_a_file_the_site_announces_as_too_large_is_refused_before_the_download(tmp_path):
    scenario = Scenario(info={**VIDEO, "filesize": 5_000_000})
    with pytest.raises(FetchError) as err:
        sized(scenario, 2_000_000).download("https://x.test/v", "audio", "m4a", None, tmp_path / "j", "j", Recorder())
    assert err.value.code == "too_large" and "5 MB" in err.value.message and "2 MB" in err.value.message
    assert scenario.processed == 0


def test_the_size_of_merged_streams_counts_together(tmp_path):
    parts = [{"protocol": "https", "filesize": 800}, {"protocol": "https", "filesize_approx": 300}]
    assert f.expected_size({"requested_formats": parts}) == 1100
    assert f.expected_size({"requested_formats": [{"filesize": 800}, {}]}) is None
    assert f.expected_size({}) is None


def test_a_download_that_grows_too_large_stops(tmp_path):
    scenario = Scenario(size=1000)
    with pytest.raises(FetchError) as err:
        sized(scenario, 600).download("https://x.test/v", "audio", "m4a", None, tmp_path / "j", "j", Recorder())
    assert err.value.code == "too_large"


def test_a_conversion_that_makes_the_file_too_large_fails(tmp_path):
    scenario = Scenario(size=500, converted_size=900)
    with pytest.raises(FetchError) as err:
        sized(scenario, 800).download("https://x.test/v", "audio", "wav", None, tmp_path / "j", "j", Recorder())
    assert err.value.code == "too_large"
    assert sized(Scenario(size=500, converted_size=700), 800).download(
        "https://x.test/v", "audio", "wav", None, tmp_path / "k", "k", Recorder()).stat().st_size == 700


def test_a_download_stops_when_the_partition_runs_low(tmp_path):
    fetcher = sized(Scenario(), None, free=GB // 2)
    assert fetcher.space_left(tmp_path) is False and sized(Scenario()).space_left(tmp_path) is True
    with pytest.raises(FetchError) as err:
        fetcher.download("https://x.test/v", "audio", "m4a", None, tmp_path / "j", "j", Recorder())
    assert err.value.code == "insufficient_storage"


def test_free_space_looks_at_the_nearest_existing_folder(tmp_path):
    assert f._free_space(tmp_path / "not" / "there") > 0


def test_an_unknown_length_fails_a_max_duration(tmp_path):
    scenario = Scenario(info={**VIDEO, "duration": None})
    fetcher = Fetcher(False, scenario.ydl_class(), resolve=resolve)
    with pytest.raises(FetchError) as err:
        fetcher.download("https://x.test/v", "audio", "m4a", 600, tmp_path / "j", "j", Recorder())
    assert err.value.code == "too_long" and "does not tell" in err.value.message and scenario.processed == 0
    assert fetcher.download("https://x.test/v", "audio", "m4a", None, tmp_path / "k", "k", Recorder()).exists()


# --- ffmpeg reads local media files only --------------------------------------------------------
@pytest.mark.parametrize("head", [b"<?xml version='1.0'?><MPD xmlns='urn:mpeg:dash:schema:mpd:2011'>",
                                  b"#EXTM3U\n#EXT-X-VERSION:3\nhttp://10.0.0.1/seg.ts\n", b"ffconcat version 1.0\n"])
def test_a_manifest_in_disguise_never_reaches_ffmpeg(head, tmp_path):
    scenario = Scenario(content=head * 20)
    with pytest.raises(FetchError) as err:
        Fetcher(False, scenario.ydl_class(), resolve=resolve).download(
            "https://x.test/v", "audio", "m4a", None, tmp_path / "j", "j", Recorder())
    assert (err.value.code, err.value.message) == ("convert_failed", "the download is no media file")


@pytest.mark.parametrize("head", [b"\x00\x00\x00\x20ftypisom", b"\x1a\x45\xdf\xa3", b"OggS\x00\x02",
                                  b"fLaC\x00\x00\x00\x22", b"ID3\x04\x00", b"\xff\xfb\x90\x64", b"RIFF\x24\x08\x00\x00WAVE"])
def test_media_files_pass_the_sniff(head, tmp_path):
    path = tmp_path / "a"
    path.write_bytes(head + b"rest of the file " * 40)
    assert f.is_media(path)
    assert f.is_media(tmp_path / "missing") and f.is_media(tmp_path / "a") is True
    (tmp_path / "empty").write_bytes(b"")
    assert f.is_media(tmp_path / "empty")


def test_every_ffmpeg_postprocessor_reads_files_only():
    args = f.base_options(False)["postprocessor_args"]
    for key in ("extractaudio+ffmpeg_i", "videoremuxer+ffmpeg_i", "merger+ffmpeg_i", "fixupm4a+ffmpeg_i"):
        assert args[key] == ["-protocol_whitelist", "file"]
    # yt-dlp looks the arguments up under exactly these keys
    from yt_dlp.postprocessor import FFmpegExtractAudioPP
    from yt_dlp.utils import _configuration_args
    assert _configuration_args(FFmpegExtractAudioPP.pp_key(), args, "ffmpeg", ["_i1", "_i"]) == [
        "-protocol_whitelist", "file"]


def test_conversions_stop_writing_past_the_size_limit(tmp_path):
    scenario = Scenario()
    sized(scenario, 5_000_000).download("https://x.test/v", "audio", "wav", None, tmp_path / "j", "j", Recorder())
    args = scenario.instances[-1].params["postprocessor_args"]
    assert args["extractaudio+ffmpeg_o"] == ["-fs", "5000000"] and args["merger+ffmpeg_o"] == ["-fs", "5000000"]
    assert args["extractaudio+ffmpeg_i"] == ["-protocol_whitelist", "file"]
    from yt_dlp.postprocessor import FFmpegExtractAudioPP
    from yt_dlp.utils import _configuration_args
    assert _configuration_args(FFmpegExtractAudioPP.pp_key(), args, "ffmpeg", ["_o1", "_o", ""]) == ["-fs", "5000000"]
    assert "extractaudio+ffmpeg_o" not in f.ffmpeg_args(None)
