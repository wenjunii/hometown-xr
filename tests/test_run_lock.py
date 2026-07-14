import json
import socket

import pytest

from run_lock import CrawlerAlreadyRunning, CrawlerRunLock


def test_lock_blocks_a_second_local_session_and_releases(tmp_path):
    path = tmp_path / ".crawler.lock"
    with CrawlerRunLock("3080", path):
        with pytest.raises(CrawlerAlreadyRunning):
            CrawlerRunLock("4090", path).acquire()
    with CrawlerRunLock("4090", path):
        assert path.exists()
    assert not path.exists()


def test_lock_replaces_stale_pid_from_same_host(tmp_path):
    path = tmp_path / ".crawler.lock"
    path.write_text(
        json.dumps({"pid": 999_999_999, "host": socket.gethostname()}),
        encoding="utf-8",
    )
    with CrawlerRunLock("3080", path):
        assert path.exists()

