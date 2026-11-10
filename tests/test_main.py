"""Starting and stopping the service."""

import logging

from media_fetcher import __main__ as main


class Threads:
    """Stands in for the job manager: threads that stop after some looks."""

    def __init__(self, looks: int | None) -> None:
        self.looks = looks

    def running(self) -> int:
        if self.looks is None:
            return 1
        self.looks -= 1
        return max(self.looks, 0)


def test_the_process_ends_when_the_threads_have_stopped():
    codes = []
    main.finish(Threads(2), grace=5, exit_now=codes.append)
    assert codes == [0]


def test_the_process_ends_after_the_grace_time_anyway(caplog):
    codes = []
    with caplog.at_level(logging.INFO, "media_fetcher"):
        main.finish(Threads(None), grace=0.2, exit_now=codes.append)
    assert codes == [0] and "still running" in caplog.text


def options(**changes):
    from media_fetcher.config import Config
    base = dict(token="", trust_home_assistant=True, max_jobs=2, keep_minutes=60, max_size_mb=2000,
                allow_any_site=False, log_level="INFO")
    return Config(**(base | changes))


def test_only_a_trusted_home_assistant_passes_without_the_token():
    assert main.trusted_peers(options()) == frozenset({"172.30.32.1"})
    assert main.trusted_peers(options(trust_home_assistant=False)) == frozenset()


def test_the_log_says_who_gets_in():
    assert "not let in" in main.access_line(options())
    assert "other clients take the token" in main.access_line(options(token="t"))
    assert "Home Assistant included" in main.access_line(options(token="t", trust_home_assistant=False))
    assert "nobody is let in" in main.access_line(options(trust_home_assistant=False))
