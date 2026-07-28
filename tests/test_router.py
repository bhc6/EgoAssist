import numpy as np

from egoassist.backends import MockBackend
from egoassist.config import Config
from egoassist.memory.store import HierarchicalMemory, MemoryItem
from egoassist.retrieval.router import route_and_retrieve


def test_recent_route_deduplicates_current_short_overlap():
    backend = MockBackend(seed=0)
    memory = HierarchicalMemory(backend, Config(retrieve_topk=5))
    item = MemoryItem(
        id=0,
        t_start=0.0,
        t_end=1.0,
        embedding=np.ones(backend.dim, dtype=np.float32) / np.sqrt(backend.dim),
        caption="item",
    )
    memory.current = [item]
    memory.short_term = [item]

    candidates = route_and_retrieve("what happened just now?", memory, backend, memory.cfg)

    assert [candidate.item.id for candidate in candidates] == [0]
