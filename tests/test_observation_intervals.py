from datetime import datetime, timedelta, timezone

from egoassist.memory import TimelineMemory

T0 = datetime(2026, 10, 9, 2, 0, tzinfo=timezone.utc)


def observe(memory, text, at, basis="measured"):
    return memory.remember(text, user_id="u", case_id="c", scope="case", kind="observation",
                           happened_at=at, metadata={"timeBasis": basis} if basis else {})


def test_next_observation_closes_previous_interval(tmp_path):
    memory = TimelineMemory(tmp_path / "t.sqlite3")
    first = observe(memory, "灶台上的锅在冒烟", T0)
    second = observe(memory, "锅已经离开灶台", T0 + timedelta(minutes=3))
    closed = memory.close_open_interval(user_id="u", case_id="c", scope="case", kind="observation",
                                        end=second["effectiveAt"], exclude_id=second["id"])
    assert closed == first["id"]
    cards = {card["id"]: card for card in memory.cards(user_id="u", case_id="c", scope="case")}
    assert cards[first["id"]]["effectiveEnd"] == second["effectiveAt"]
    assert cards[second["id"]]["effectiveEnd"] is None


def test_text_time_inside_measured_observation_is_ignored(tmp_path):
    memory = TimelineMemory(tmp_path / "t.sqlite3")
    card = observe(memory, "日历上写着明天三点开会", T0)
    assert card["effectiveAt"] == T0.isoformat(timespec="milliseconds")
    assert card["effectiveEnd"] is None


def test_unmeasured_cards_are_never_closed(tmp_path):
    memory = TimelineMemory(tmp_path / "t.sqlite3")
    observe(memory, "旧的观察", T0, basis=None)
    assert memory.close_open_interval(user_id="u", case_id="c", scope="case", kind="observation",
                                      end=T0 + timedelta(minutes=1)) is None
