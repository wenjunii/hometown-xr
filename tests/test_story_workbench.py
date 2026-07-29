import gzip
import json
import threading
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen

from story_review import StoryReviewIndex, load_story_reviews
from story_workbench import _StoryWorkbenchHandler


def _write_export(path):
    row = {
        "story_id": "story-one",
        "record_id": "record-one",
        "match_numbers": [1],
        "language": "en",
        "url": "https://example.test/story",
        "capture_count": 1,
        "seed": {
            "paragraph": "I remembered my hometown.",
            "concept_match": "memories of home",
            "matched_keywords": ["hometown"],
            "semantic_score": 0.8,
        },
        "story": {
            "text": "The exact source story.",
            "character_count": 23,
            "paragraph_count": 1,
            "paragraphs": [{"role": "seed", "text": "The exact source story."}],
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def _json_request(url, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    request = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if data is not None else "GET",
    )
    with urlopen(request, timeout=5) as response:
        return json.loads(response.read())


def test_story_workbench_http_search_detail_and_review(tmp_path):
    export_path = tmp_path / "stories.jsonl.gz"
    reviews_path = tmp_path / "reviews.jsonl"
    _write_export(export_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StoryWorkbenchHandler)
    server.story_index = StoryReviewIndex(export_path, reviews_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        status = _json_request(f"{base}/api/status")
        search = _json_request(f"{base}/api/stories?search=hometown")
        detail = _json_request(f"{base}/api/stories/story-one")
        saved = _json_request(
            f"{base}/api/review",
            {
                "story_id": "story-one",
                "decision": "selected",
                "notes": "Keep exact source",
                "reviewer": "tester",
            },
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert status["stories"] == 1
    assert search["stories"][0]["story_id"] == "story-one"
    assert detail["story"]["text"] == "The exact source story."
    assert saved["decision"] == "selected"
    assert load_story_reviews(reviews_path)["story-one"]["notes"] == "Keep exact source"
