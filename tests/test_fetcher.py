"""The yt-dlp wrapper: options, error mapping, what a download reports."""

import threading
from pathlib import Path

import pytest
from conftest import VIDEO, Scenario
from yt_dlp.utils import DownloadCancelled

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
        Fetcher(False, scenario.ydl_class()).info("https://www.youtube.com/playlist?list=PL1")
    assert err.value.code == "unsupported_url"


def test_info_maps_extraction_errors():
    scenario = Scenario(extract_error="ERROR: [youtube] abc: Video unavailable. This video is not available")
    with pytest.raises(FetchError) as err:
        Fetcher(False, scenario.ydl_class()).info("https://www.youtube.com/watch?v=abc")
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
        Fetcher(False, scenario.ydl_class()).download("https://x.test/v", "audio", "m4a", None, tmp_path / "j", "j", Recorder())
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
        Fetcher(False, scenario.ydl_class()).download("https://x.test/v", "audio", "m4a", None, tmp_path / "j", "j", Recorder())
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
    fetcher = Fetcher(False, scenario.ydl_class())
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
        Fetcher(False, scenario.ydl_class()).download("https://x.test/v", "audio", "m4a", None, tmp_path / "j", "j", report)
        done.set()

    threading.Thread(target=run, daemon=True).start()
    assert not done.wait(0.2) and report.progress_seen == [(0, 1000)]
    gate.set()
    assert done.wait(2)
