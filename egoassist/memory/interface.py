"""Minimal shared interface for EgoAssist memory backends.

Both HierarchicalMemory (research path) and TimelineMemory (product path)
satisfy this Protocol via their respective adapters in ``adapters.py``.
Callers that only need generic read/write should type-hint against
``MemoryBackend`` rather than a concrete class.

Example
-------
from egoassist.memory import MemoryBackend, TimelineMemoryAdapter, TimelineMemory

mem: MemoryBackend = TimelineMemoryAdapter(TimelineMemory("runtime.sqlite3"))
mem.ingest_text("met Alice at the café", user_id="u1", kind="event", metadata={})
results = mem.retrieve_text("coffee meeting", user_id="u1", limit=5)
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class MemoryBackend(Protocol):
    """Minimal read/write contract shared by all memory implementations.

    This is a *structural* protocol (runtime_checkable) — no base class
    inheritance is required.  Both adapters in ``adapters.py`` implement it.
    """

    def ingest_text(
        self,
        text: str,
        *,
        user_id: str,
        kind: str,
        metadata: dict[str, Any],
    ) -> str:
        """Persist a text memory and return its opaque ID.

        Args:
            text:     The memory content (≤1000 chars; will be truncated).
            user_id:  Owning user.
            kind:     One of the MEMORY_KINDS defined in ``timeline.py``
                      (e.g. ``"fact"``, ``"event"``, ``"observation"``).
                      For HierarchicalMemory the value is stored in metadata.
            metadata: Arbitrary JSON-serialisable key/value pairs.

        Returns:
            Opaque string ID that can be passed back to retrieve or delete.
        """
        ...

    def retrieve_text(
        self,
        query: str,
        *,
        user_id: str,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        """Return the *limit* most relevant memory cards for *query*.

        Each returned dict contains at minimum:
            id    — opaque card ID
            text  — memory content
            score — relevance score ∈ [0, 1]  (higher = more relevant)
        """
        ...
