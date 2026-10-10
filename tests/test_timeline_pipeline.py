from datetime import datetime, timedelta, timezone

from egoassist.backends import MockBackend
from egoassist.capture.stream import InMemoryStream
from egoassist.config import Config
from egoassist.data import make_day
from egoassist.memory import TimelineMemory
from egoassist.qa import TimelineAssistant

ORIGIN = datetime(2026, 10, 9, 1, 0, tzinfo=timezone.utc)


def build(tmp_path):
    stream, _ = make_day(n_events=12, seed=0)
    timeline = TimelineMemory(tmp_path / "t.sqlite3")
    assistant = TimelineAssistant(MockBackend(seed=0), Config(seed=0), timeline=timeline,
                                  user_id="u", case_id="video", video_origin=ORIGIN)
    return assistant, timeline, stream


def test_scene_cards_carry_real_video_interval_not_ingest_time(tmp_path):
    assistant, timeline, stream = build(tmp_path)
    assert assistant.ingest(InMemoryStream(stream)) > 0
    cards = timeline.cards(user_id="u", case_id="video", scope="case", kinds=("scene",))
    assert cards
    for card in cards:
        meta = card["metadata"]
        assert card["effectiveAt"] == (ORIGIN + timedelta(seconds=meta["videoStartSeconds"])).isoformat(timespec="milliseconds")
        assert card["effectiveEnd"] is not None and card["effectiveEnd"] >= card["effectiveAt"]
        assert card["text"] != "[no visual content]"


def test_reingest_is_idempotent_and_all_captions_is_not_truncated(tmp_path):
    assistant, timeline, stream = build(tmp_path)
    assistant.ingest(InMemoryStream(stream))
    first = len(timeline.cards(user_id="u", case_id="video", scope="case", kinds=("scene",)))
    assistant.ingest(InMemoryStream(stream))
    assert len(timeline.cards(user_id="u", case_id="video", scope="case", kinds=("scene",))) == first
    assert len(assistant.all_captions()) == first
