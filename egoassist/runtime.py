"""Three-lane application boundary over the existing independent executors.

Observation acquires evidence, interaction responds and acts, memory retains and
derives knowledge. This module joins their snapshots without awaiting their work.
"""
from copy import deepcopy
from datetime import datetime, timezone

from egoassist.realtime.settings import integer


def _counts(rows):
    return {status: sum(row.get("status") == status for row in rows)
            for status in ("queued", "running", "failed", "completed", "cancelled", "foreground")}


class ThreeLaneRuntime:
    def __init__(self, journal, user_id):
        self.journal, self.user_id = journal, user_id
        self.session_id = journal.session_id

    def observation_references(self, timeline, *, task=None):
        limit = integer("EGOASSIST_OBSERVATION_REFERENCE_LIMIT", 4, maximum=32)
        text_limit = integer("EGOASSIST_OBSERVATION_REFERENCE_CHARS", 1000, maximum=16000)
        messages = [row for row in self.journal.entries()
                    if row.get("type") == "message" and row.get("message", {}).get("role") == "user"]
        cards = timeline.cards(user_id=self.user_id, case_id=self.session_id, scope="case", limit=limit)
        return deepcopy({
            "basis": "historical-references-not-current-visual-evidence",
            "limit": limit, "textLimit": text_limit, "totalUserMessages": len(messages),
            "userMessages": [{"sourceId": row["id"], "recordedAt": row["timestamp"],
                              "text": str(row["message"]["content"])[:text_limit],
                              "truncated": len(str(row["message"]["content"])) > text_limit}
                             for row in messages[-limit:]],
            "memory": [{key: card.get(key) for key in
                        ("id", "text", "kind", "sourceIds", "happenedAt", "recordedAt")}
                       for card in cards],
            "pendingClarification": timeline.pending_draft(user_id=self.user_id, case_id=self.session_id),
            "task": {key: task.get(key) for key in ("task", "currentCheckpointId", "completed", "paused")} if task else None,
        })

    def snapshot(self, *, observation, scheduler, pending_inputs=0, classifying=0,
                 consolidating=False, memory_enabled=True):
        jobs, latest_observation = {}, None
        for row in self.journal.entries():
            kind = row.get("type")
            if kind in {"memory.recovery.job", "insight.job", "answer.job"}:
                jobs[(kind, row.get("turnId") or row.get("jobId"))] = row
            if kind == "ambient_observation" and row.get("status") == "accepted":
                if latest_observation is None or str(row.get("frame_captured_at", "")) >= str(latest_observation.get("frame_captured_at", "")):
                    latest_observation = row
        memory_jobs = [row for (kind, _), row in jobs.items() if kind != "answer.job"]
        answer_jobs = [row for (kind, _), row in jobs.items() if kind == "answer.job"]
        evidence = (latest_observation or {}).get("evidence") or {}
        ambient = observation or {}
        lane = ambient.get("lane") or {}
        camera_at = ambient.get("cameraAt")
        camera_age = None
        if camera_at:
            camera_age = max(0, round((datetime.now(timezone.utc) - datetime.fromisoformat(camera_at)).total_seconds() * 1000))
        return {
            "schemaVersion": "three-lanes.v1", "sessionId": self.session_id,
            "observation": {
                "enabled": bool(ambient.get("enabled") and ambient.get("consented")),
                "connected": bool(ambient.get("connected")), "cameraAt": camera_at, "cameraAgeMs": camera_age,
                "ambient": lane, "task": ambient.get("taskLane"),
                "processedThrough": evidence.get("capturedAt"),
                "latestEvidenceId": (latest_observation or {}).get("id"),
                "coverage": "sampled-frames",
            },
            "interaction": {
                "scheduler": deepcopy(scheduler), "pendingInputs": pending_inputs,
                "classifying": classifying, "answers": _counts(answer_jobs),
            },
            "memory": {
                "enabled": memory_enabled, "jobs": _counts(memory_jobs), "consolidating": consolidating,
                "activeJobs": [{"id": row.get("jobId") or row.get("turnId"), "kind": row["type"],
                                "status": row["status"], "phase": row.get("phase"), "updatedAt": row["timestamp"]}
                               for row in memory_jobs if row["status"] in {"queued", "running"}],
            },
        }
