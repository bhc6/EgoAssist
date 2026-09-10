"""Bounded historical references for observation, never current visual evidence."""
from copy import deepcopy

from .settings import integer


def memory_work_snapshot(journal):
    jobs = {}
    for row in journal.entries():
        if row.get("type") in {"memory.recovery.job", "insight.job"}:
            jobs[(row["type"], row.get("turnId") or row.get("jobId"))] = row
    return {status: sum(row.get("status") == status for row in jobs.values())
            for status in ("queued", "running", "failed", "completed")}


def observation_references(journal, timeline, *, user_id, session_id, task=None):
    limit = integer("EGOASSIST_OBSERVATION_REFERENCE_LIMIT", 4, maximum=32)
    text_limit = integer("EGOASSIST_OBSERVATION_REFERENCE_CHARS", 1000, maximum=16000)
    messages = [row for row in journal.entries()
                if row.get("type") == "message" and row.get("message", {}).get("role") == "user"]
    cards = timeline.cards(user_id=user_id, case_id=session_id, scope="case", limit=limit)
    return deepcopy({
        "basis": "historical-references-not-current-visual-evidence",
        "limit": limit, "textLimit": text_limit, "totalUserMessages": len(messages),
        "userMessages": [{"sourceId": row["id"], "recordedAt": row["timestamp"],
                          "text": str(row["message"]["content"])[:text_limit],
                          "truncated": len(str(row["message"]["content"])) > text_limit} for row in messages[-limit:]],
        "memory": [{key: card.get(key) for key in
                    ("id", "text", "kind", "sourceIds", "happenedAt", "recordedAt")}
                   for card in cards],
        "pendingClarification": timeline.pending_draft(user_id=user_id, case_id=session_id),
        "task": {key: task.get(key) for key in ("task", "currentCheckpointId", "completed", "paused")} if task else None,
    })
