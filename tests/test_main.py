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
