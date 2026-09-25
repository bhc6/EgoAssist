"""Three-lane application boundary over the existing independent executors.

Observation acquires evidence, interaction responds and acts, memory retains and
derives knowledge. This module joins their snapshots without awaiting their work.
"""
from copy import deepcopy
from datetime import datetime, timezone

from egoassist.core.settings import integer
from egoassist.realtime.observation_reviews import ObservationReviews


def _counts(rows):
    return {status: sum(row.get("status") == status for row in rows)
            for status in ("queued", "running", "failed", "completed", "cancelled", "foreground")}


class ThreeLaneRuntime:
    def __init__(self, journal, user_id):
        self.journal, self.user_id = journal, user_id
        self.session_id = journal.session_id
        self.reviews = ObservationReviews(journal)

    def observation_goal(self):
        """Keep the source stable until the user changes or clears the goal."""
        def reduce(state, row):
            if row.get("type") != "ambient.settings":
                return
            goal = (row.get("settings") or {}).get("reminderGoal", "").strip()
            if goal != state["text"]:
                state.update(text=goal, sourceId=row["id"], recordedAt=row["timestamp"])
        goal = self.journal.project("observation-reminder-goal", lambda: {"text": ""}, reduce)
        return {**goal, "referenceId": "g1"} if goal["text"] else None

    def observation_references(self, timeline, *, task=None):
        limit = integer("EGOASSIST_OBSERVATION_REFERENCE_LIMIT", 4, maximum=32)
        text_limit = integer("EGOASSIST_OBSERVATION_REFERENCE_CHARS", 1000, maximum=16000)
        def recent_users(state, row):
            if row.get("type") == "message" and row.get("message", {}).get("role") == "user":
                state["count"] += 1
                state["rows"].append(row)
                del state["rows"][:-limit]
        user_history = self.journal.project(("observation-users", limit), lambda: {"count": 0, "rows": []}, recent_users)
        messages = user_history["rows"]
        cards = timeline.cards(user_id=self.user_id, case_id=self.session_id, limit=limit)
        pending = timeline.pending_draft(user_id=self.user_id, case_id=self.session_id)
        self.reviews.reconcile(pending)
        goal = self.observation_goal()
        return deepcopy({
            "basis": "historical-references-not-current-visual-evidence",
            "limit": limit, "textLimit": text_limit, "totalUserMessages": user_history["count"],
            "userMessages": [{"sourceId": row["id"], "referenceId": "u" + str(index + 1), "recordedAt": (row.get("metadata") or {}).get("sourceRecordedAt") or row["timestamp"],
                              "text": str(row["message"]["content"])[:text_limit],
                              "truncated": len(str(row["message"]["content"])) > text_limit}
                             for index, row in enumerate(messages[-limit:])],
            "memory": [{**{key: card.get(key) for key in
                        ("id", "text", "kind", "scope", "subject", "predicate", "sourceIds", "happenedAt", "recordedAt",
                         "effectiveAt", "effectiveEnd", "timezone", "grounding")},
                        "referenceId": "m" + str(index + 1),
                        "frame": card.get("metadata", {}).get("evidenceContext", {}).get("frame")}
                       for index, card in enumerate(cards)],
            "pendingClarification": pending,
            "reminderGoals": [goal] if goal else [],
            "questionHistory": [{key: row.get(key) for key in
                                ("candidateId", "question", "notification", "status", "reason", "capturedAt", "sentAt", "follows")}
                               for row in self.reviews.recent(questions_only=True)],
            "task": {key: task.get(key) for key in ("task", "currentCheckpointId", "completed", "paused")} if task else None,
        })

    def snapshot(self, *, observation, scheduler, pending_inputs=0, classifying=0,
                 consolidating=False, memory_enabled=True, account_diary=None, memory_metrics=None):
        def reduce(state, row):
            jobs, phases = state["jobs"], state["phases"]
            kind = row.get("type")
            if kind == "turn.progress":
                phases[row["turnId"]] = row
            if kind == "turn_inbox_state" and row.get("status") in {"completed", "failed", "cancelled"}:
                phases.pop(row["turnId"], None)
            if kind == 'long_event.reset':
                for key in list(jobs):
                    if key[0] == 'long_event.job':
                        del jobs[key]
            if kind == 'long_event.request':
                # A new batch request replaces interrupted attempts; they are
                # not additional queued workers waiting to execute.
                for key, job in list(jobs.items()):
                    if key[0] == 'long_event.job' and job['status'] in {'queued', 'running'}:
                        jobs[key] = {**job, 'status': 'cancelled'}
            if kind in {"memory.recovery.job", "insight.job", "answer.job", "long_event.job"}:
                # Lane status never consumes prompts, media, or prepared answers.
                # Select before the journal's defensive copy, not afterwards.
                jobs[(kind, row.get("turnId") or row.get("jobId") or row.get('requestId'))] = {
                    key: row[key] for key in ("type", "turnId", "jobId", "requestId", "status", "phase", "timestamp")
                    if key in row
                }
            if kind == 'memory.read':
                state['lastRead'] = {key: row.get(key) for key in ('status', 'failures', 'timestamp')}
            if kind == "answer.job" and row.get("status") in {"completed", "failed", "cancelled", "paused"}:
                phases.pop(row["turnId"], None)
            if kind == "ambient_observation" and row.get("status") == "accepted":
                previous = state["observation"]
                if previous is None or str(row.get("frame_captured_at", "")) >= str(previous.get("frame_captured_at", "")):
                    state["observation"] = row
        projected = self.journal.project("three-lanes", lambda: {"jobs": {}, "phases": {}, "observation": None, 'lastRead': None}, reduce)
        jobs, phases, latest_observation = projected["jobs"], projected["phases"], projected["observation"]
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
                "reviews": self.reviews.recent(),
            },
            "interaction": {
                "scheduler": deepcopy(scheduler), "pendingInputs": pending_inputs,
                "classifying": classifying, "answers": _counts(answer_jobs),
                "preparation": [{"turnId": row["turnId"], "phase": row["phase"], "updatedAt": row["timestamp"],
                                 "queueMs": row.get("queueMs")} for row in phases.values()],
            },
            "memory": {
                "enabled": memory_enabled, "jobs": _counts(memory_jobs), "consolidating": consolidating,
                'scope': 'case', 'lastRead': projected['lastRead'],
                'accountDiary': deepcopy(account_diary),
                'metrics': deepcopy(memory_metrics),
                "activeJobs": [{"id": row.get("jobId") or row.get("turnId") or row.get('requestId'), "kind": row["type"],
                                "status": row["status"], "phase": row.get("phase"), "updatedAt": row["timestamp"]}
                               for row in memory_jobs if row["status"] in {"queued", "running"}],
            },
        }
