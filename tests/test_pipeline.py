"""端到端 pipeline 的单元测试。"""

import pytest

from egoassist.backends import MockBackend
from egoassist.capture.stream import InMemoryStream
from egoassist.config import Config
from egoassist.data import make_day
from egoassist.eval import evaluate
from egoassist.qa import StreamingMemoryAssistant


@pytest.fixture(scope="module")
def built():
    """One ingest for all pipeline assertions (make_day + ingest is not cheap)."""
    cfg = Config(seed=0)
    stream, queries = make_day(n_events=40, seed=0)
    asst = StreamingMemoryAssistant(MockBackend(seed=0), cfg)
    n = asst.ingest(InMemoryStream(stream))
    return asst, queries, n


def test_ingest_creates_events(built):
    asst, queries, n = built
    assert n >= 20                                   # 分出足够多微事件
    assert len(asst.memory.all_items()) > 0


def test_answerable_retrieval_correct(built):
    asst, queries, _ = built
    ans = [q for q in queries if q.answerable]
    recs = evaluate(asst, ans)
    acc = sum(r.pred_correct for r in recs) / len(recs)
    assert acc >= 0.7, f"可回答查询正确率过低: {acc}"


def test_answerable_scores_higher_than_unanswerable(built):
    asst, queries, _ = built
    recs = evaluate(asst, queries)
    ans = [r.score for r in recs if r.answerable]
    una = [r.score for r in recs if not r.answerable]
    assert sum(ans) / len(ans) > sum(una) / len(una), "可回答分数应高于不可回答"


def test_localization_iou_reasonable(built):
    asst, queries, _ = built
    recs = evaluate(asst, [q for q in queries if q.answerable])
    hit = [r for r in recs if r.pred_correct]
    miou = sum(r.iou for r in hit) / len(hit)
    assert miou > 0.1, f"命中样本的定位 mIoU 过低: {miou}"
