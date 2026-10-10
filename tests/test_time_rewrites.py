from datetime import datetime, timezone

import pytest

from egoassist.memory import ClockContext, TimelineMemory, resolve_time_semantics

CLOCK = ClockContext.current("Asia/Shanghai", 480, now_utc=datetime(2026, 10, 9, 6, 0, tzinfo=timezone.utc))


@pytest.mark.parametrize("text", ["说慢一点", "再多一点盐", "有一点累", "一点儿都不", "差一点忘了", "一点点"])
def test_degree_word_dian_is_not_a_clock(text):
    result = resolve_time_semantics(text, CLOCK, kind="event")
    assert result["clockExplicit"] is False
    assert result["effectiveAt"] is None


@pytest.mark.parametrize("text,hour", [("下午三点开会", 15), ("十二点吃饭", 12), ("一点钟出发", 1)])
def test_real_cjk_clock_still_parses(text, hour):
    result = resolve_time_semantics(text, CLOCK, kind="event")
    assert result["clockExplicit"] is True
    local = datetime.fromisoformat(result["effectiveAt"]).astimezone(CLOCK.tzinfo)
    assert local.hour == hour


@pytest.mark.parametrize("text,expected", [
    # A multi-digit numeral must never be read from its last digit.
    ("早十一点", (11, 0)), ("明晚十一点到", (23, 0)), ("晚十二点", (12, 0)),
    # 晚 directly before the number marks evening.
    ("晚十点", (22, 0)), ("今天晚八点", (20, 0)),
    # Sentence-final particles after a clock time are not degree words.
    ("十点也行", (10, 0)), ("两点吧", (2, 0)), ("三点都没来", (3, 0)),
    ("三点半", (3, 30)), ("三点十五分", (3, 15)), ("差一点到三点", (3, 0)),
])
def test_cjk_clock_reads_the_whole_numeral(text, expected):
    from egoassist.memory.temporal import _clock_from_text
    assert _clock_from_text(text) == expected


def test_just_now_state_supersedes_older_location(tmp_path):
    memory = TimelineMemory(tmp_path / "t.sqlite3")
    common = {"user_id": "u", "case_id": "c", "scope": "case", "kind": "state",
              "subject": "钥匙", "predicate": "location"}
    old = memory.remember("钥匙在抽屉", value="抽屉", recorded_at=datetime(2026, 10, 9, 5, tzinfo=timezone.utc), **common)
    temporal = resolve_time_semantics("钥匙刚才放桌上了", CLOCK, kind="state")
    assert temporal["status"] == "resolved"
    new = memory.remember("钥匙刚才放桌上了", value="桌上", recorded_at=temporal["recordedAt"],
                          effective_at=temporal["effectiveAt"], **common)
    active = [card["id"] for card in memory.cards(user_id="u", case_id="c", scope="case")]
    assert active == [new["id"]] and old["id"] not in active
