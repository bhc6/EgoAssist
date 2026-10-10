import sqlite3

import numpy as np
import pytest

from egoassist.memory import TimelineMemory
from egoassist.memory.semantic import HashingEmbedder
from egoassist.memory.timeline import memory_key_for, memory_terms, query_terms_for


def remember(memory, text, **overrides):
    values = {"user_id": "u", "case_id": "c", "scope": "case", "kind": "event"}
    values.update(overrides)
    return memory.remember(text, **values)


def ids(result):
    return [item["cardId"] for item in result["selected"]]


def test_cjk_terms_do_not_cross_punctuation():
    assert "口小" not in memory_terms("钥匙在门口。小李来了")
    assert {"钥匙", "门口", "小李"} <= memory_terms("钥匙在门口。小李来了")


def test_query_stop_terms_are_dropped_but_never_empty_the_query():
    terms = query_terms_for("钥匙放在哪里了？")
    assert "哪里" not in terms and "钥匙" in terms
    assert query_terms_for("什么") == {"什么"}


@pytest.mark.parametrize("subject,predicate", [("我的钥匙", "位置"), ("钥匙", "location"), ("my 钥匙", "地点")])
def test_equivalent_claims_share_one_canonical_key(subject, predicate):
    assert memory_key_for(subject, predicate) == "location:钥匙"


def test_named_owner_stays_distinct_and_phone_aliases_map():
    assert memory_key_for("小李的钥匙", "位置") == "location:小李的钥匙"
    assert memory_key_for("小李", "手机号") == "contact:小李:phone"


def test_differently_phrased_update_now_supersedes(tmp_path):
    memory = TimelineMemory(tmp_path / "t.sqlite3")
    old = remember(memory, "我的钥匙在抽屉", kind="state", subject="我的钥匙", predicate="位置", value="抽屉")
    new = remember(memory, "钥匙在门口", kind="state", subject="钥匙", predicate="location", value="门口")
    assert new["supersedes"] == old["id"]
    assert [card["id"] for card in memory.cards(user_id="u", case_id="c", scope="case")] == [new["id"]]


def test_two_character_word_is_recalled_past_the_candidate_limit(tmp_path, monkeypatch):
    # Above the limit candidates come from FTS. The trigram index cannot see
    # "冰箱"; the term index can, so an old target is not lost to recency fill.
    monkeypatch.setenv("EGOASSIST_MEMORY_CANDIDATE_LIMIT", "32")
    memory = TimelineMemory(tmp_path / "t.sqlite3")
    target = remember(memory, "牛奶在冰箱", card_id="target")
    for index in range(80):
        remember(memory, f"第{index}条无关的日常记录", card_id=f"noise-{index}")
    result = memory.retrieve("冰箱", user_id="u", case_id="c", scope="case")
    assert result["timing"]["candidatesTruncated"] is True
    assert ids(result)[0] == target["id"]


class FixedEmbedder:
    """Maps known texts to fixed vectors; everything else is orthogonal noise."""

    name = "fixed-test"
    dim = 4

    def __init__(self, table):
        self.table = table

    def __call__(self, texts, *, role="document"):
        rows = [self.table.get(t, [0.0, 0.0, 0.0, 1.0]) for t in texts]
        matrix = np.asarray(rows, dtype=np.float32)
        return matrix / np.linalg.norm(matrix, axis=1, keepdims=True)


def test_semantic_channel_recalls_paraphrase_with_zero_lexical_overlap(tmp_path):
    table = {"门卡搁在玄关柜上了": [1.0, 0.1, 0.0, 0.0], "我的出入证放哪了": [1.0, 0.0, 0.0, 0.0],
             "晚饭吃了面条": [0.0, 1.0, 0.0, 0.0]}
    lexical = TimelineMemory(tmp_path / "a.sqlite3", embedder=None)
    remember(lexical, "门卡搁在玄关柜上了")
    assert ids(lexical.retrieve("我的出入证放哪了", user_id="u", case_id="c", scope="case")) == []

    memory = TimelineMemory(tmp_path / "b.sqlite3", embedder=FixedEmbedder(table))
    card = remember(memory, "门卡搁在玄关柜上了")
    remember(memory, "晚饭吃了面条")
    result = memory.retrieve("我的出入证放哪了", user_id="u", case_id="c", scope="case")
    assert ids(result) == [card["id"]]
    assert "semantic-match" in result["selected"][0]["reasons"]
    assert result["routing"]["semanticChannel"] == "fixed-test"


def test_semantic_channel_never_bypasses_hard_scope(tmp_path):
    table = {"门卡搁在玄关柜上了": [1.0, 0.0, 0.0, 0.0], "我的出入证放哪了": [1.0, 0.0, 0.0, 0.0]}
    memory = TimelineMemory(tmp_path / "t.sqlite3", embedder=FixedEmbedder(table))
    remember(memory, "门卡搁在玄关柜上了", case_id="other-case")
    assert ids(memory.retrieve("我的出入证放哪了", user_id="u", case_id="c", scope="case")) == []


def test_vectors_cascade_with_card_deletion(tmp_path):
    memory = TimelineMemory(tmp_path / "t.sqlite3", embedder=HashingEmbedder(dim=64))
    remember(memory, "牛奶在冰箱", source_ids=["s1"])
    with sqlite3.connect(memory.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0] == 1
    memory.forget_sources(user_id="u", source_ids=["s1"])
    with sqlite3.connect(memory.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0] == 0


def test_deferred_embedding_can_be_backfilled(tmp_path):
    memory = TimelineMemory(tmp_path / "t.sqlite3", embedder=HashingEmbedder(dim=64), embed_on_write=False)
    remember(memory, "牛奶在冰箱")
    remember(memory, "钥匙在门口")
    assert memory.index_vectors() == 2
    assert memory.index_vectors() == 0


def test_embedding_failure_degrades_to_lexical(tmp_path):
    def broken(texts, *, role="document"):
        raise RuntimeError("provider down")
    broken.name = "broken"
    memory = TimelineMemory(tmp_path / "t.sqlite3", embedder=broken)
    card = remember(memory, "牛奶在冰箱")
    assert ids(memory.retrieve("冰箱里有牛奶吗", user_id="u", case_id="c", scope="case")) == [card["id"]]


def test_household_prefix_is_not_stripped_from_longer_entities():
    # 我家 alone is not a possessive prefix: 我家里的空调 is not 里的空调.
    assert memory_key_for("我家里的空调", "位置") == "location:我家里的空调"
    assert memory_key_for("我家的空调", "位置") == "location:空调"


def test_v4_migration_leaves_one_active_claim_per_canonical_key(tmp_path):
    """Two v3 keys that canonicalise to the same key must not both stay active."""
    path = tmp_path / "t.sqlite3"
    memory = TimelineMemory(path)
    old = remember(memory, "钥匙在抽屉", kind="state", subject="钥匙", predicate="location", value="抽屉")
    new = remember(memory, "钥匙在门口", kind="state", subject="钥匙", predicate="location", value="门口",
                   supersede=False)
    # Rewind to the pre-v4 shape: distinct legacy keys, both active, no term index.
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE memory_cards SET status='active', supersedes=NULL, search_terms=NULL")
        connection.execute("UPDATE memory_cards SET memory_key='位置:我的钥匙', subject='我的钥匙', predicate='位置' WHERE id=?", (old["id"],))
        connection.execute("UPDATE memory_cards SET happened_at='2020-01-01T00:00:00.000+00:00' WHERE id=?", (old["id"],))
        connection.execute("UPDATE timeline_meta SET value='3' WHERE key='schema_version'")

    migrated = TimelineMemory(path)
    active = migrated.cards(user_id="u", case_id="c", scope="case")
    assert [card["id"] for card in active] == [new["id"]]
    assert active[0]["memoryKey"] == "location:钥匙"


def test_embedder_never_runs_on_the_event_loop_thread(tmp_path):
    """A network embedder on the loop thread would freeze every connection."""
    import asyncio
    import threading

    calls = []

    class RecordingEmbedder(FixedEmbedder):
        def __call__(self, texts, *, role="document"):
            calls.append((role, threading.get_ident()))
            return super().__call__(texts, role=role)

    memory = TimelineMemory(tmp_path / "t.sqlite3", embedder=RecordingEmbedder({}))

    async def scenario():
        loop_thread = threading.get_ident()
        card = remember(memory, "牛奶在冰箱")              # write from a coroutine
        result = memory.retrieve("冰箱里有牛奶吗", user_id="u", case_id="c", scope="case")
        await asyncio.gather(*memory._embedding_tasks)     # let the background embed finish
        return loop_thread, card, result

    loop_thread, card, result = asyncio.run(scenario())
    assert calls, "the write should still have been embedded"
    assert all(thread != loop_thread for _, thread in calls)
    # Retrieval on the loop stays lexical instead of blocking, and still works.
    assert ids(result) == [card["id"]]
    with sqlite3.connect(memory.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0] == 1
