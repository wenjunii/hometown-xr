import json
import threading

import pytest

import main
from run_lock import CrawlerRunLock
from story_control import request_story_shutdown, watch_story_shutdown


def test_story_shutdown_request_targets_active_enrichment_pid(tmp_path):
    lock_path = tmp_path / ".crawler.lock"
    request_path = tmp_path / ".story-stop-request.json"

    with CrawlerRunLock("story-enrichment", lock_path):
        result = request_story_shutdown(lock_path, request_path)
        payload = json.loads(request_path.read_text(encoding="utf-8"))

    assert result["requested"]
    assert payload["target_pid"] == result["pid"]


def test_story_shutdown_watcher_sets_event_and_cleans_request(tmp_path):
    lock_path = tmp_path / ".crawler.lock"
    request_path = tmp_path / ".story-stop-request.json"
    shutdown_event = threading.Event()

    with CrawlerRunLock("story-enrichment", lock_path):
        with watch_story_shutdown(
            shutdown_event,
            request_path=request_path,
            poll_seconds=0.01,
        ):
            request_story_shutdown(lock_path, request_path)
            assert shutdown_event.wait(timeout=2)

    assert not request_path.exists()


def test_story_shutdown_watcher_accepts_current_run_token_before_lock(tmp_path):
    request_path = tmp_path / ".story-stop-request.json"
    shutdown_event = threading.Event()

    with watch_story_shutdown(
        shutdown_event,
        request_path=request_path,
        poll_seconds=0.01,
        run_token="current-run",
    ):
        request_path.write_text(
            json.dumps({"run_token": "current-run"}),
            encoding="utf-8",
        )
        assert shutdown_event.wait(timeout=2)

    assert not request_path.exists()


def test_story_shutdown_request_does_not_target_another_profile(tmp_path):
    lock_path = tmp_path / ".crawler.lock"
    request_path = tmp_path / ".story-stop-request.json"

    with CrawlerRunLock("3080", lock_path):
        result = request_story_shutdown(lock_path, request_path)

    assert not result["requested"]
    assert "not story enrichment" in result["reason"]
    assert not request_path.exists()


def test_external_request_is_not_mistaken_for_second_console_signal(monkeypatch):
    shutdown_event = threading.Event()
    shutdown_event.set()
    monkeypatch.setattr(main, "_shutdown_event", shutdown_event)
    monkeypatch.setattr(main, "_shutdown_signal_count", 0)

    main._signal_handler(None, None)

    assert main._shutdown_signal_count == 1
    with pytest.raises(SystemExit):
        main._signal_handler(None, None)
