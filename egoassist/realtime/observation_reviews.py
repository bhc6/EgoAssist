"""Source-bound observation reviews and durable, non-interrupting clarification gates."""
from datetime import datetime, timezone
import hashlib

from egoassist.core.contracts import ObservationReferenceCheck


class ObservationReviews:
    def __init__(self, journal):
        self.journal = journal
        self.rows = {}
        self.offered = {}
        self.last_sent_at = None
        for row in journal.entries():
            if row.get("type") == "observation.review":
                self.rows[row["candidateId"]] = row
                self._index_offered(row)

    def _index_offered(self, row):
        if row.get("sentAt"):
            self.offered[row["fingerprint"]] = row
            self.last_sent_at = max(self.last_sent_at or row["sentAt"], row["sentAt"])

    def record(self, row, **changes):
        value = {k: v for k, v in row.items() if k not in {"id", "type", "timestamp", "parentId"}}
        saved = self.journal.append("observation.review", {**value, **changes})
        self.rows[saved["candidateId"]] = saved
        self._index_offered(saved)
        return saved

    def recent(self, limit=6, *, questions_only=False):
        rows = (row for row in self.rows.values() if not questions_only or row.get("question"))
        return sorted(rows, key=lambda row: row["timestamp"])[-limit:]

    def reconcile(self, pending):
        for row in list(self.rows.values()):
            if row.get("draftId") and row["status"] in {"audio-sent", "played"} and row["draftId"] != (pending or {}).get("id"):
                self.record(row, status="closed", reason="clarification-no-longer-pending")

    def consider(self, captured, hypothesis, observation, *, fresh, enabled, busy, allowed, pending, cooldown):
        self.reconcile(pending)
        reference = captured.reference_context or {}
        supplied = {}
        for item in reference.get("userMessages", []):
            supplied[item["sourceId"]] = {**item, "sourceIds": [item["sourceId"]]}
        for item in reference.get("memory", []):
            supplied[item["id"]] = item
        aliases = {item["referenceId"]: item.get("id", item.get("sourceId")) for item in
                   [*reference.get("userMessages", []), *reference.get("memory", [])] if item.get("referenceId")}
        raw_check = hypothesis.reference_check
        check = None
        roots, check_error = set(), None
        if raw_check is not None:
            try:
                check = ObservationReferenceCheck.model_validate(raw_check, strict=True).model_dump()
                check["source_ids"] = [aliases.get(source, source) for source in check["source_ids"]]
            except ValueError:
                check_error = "invalid-reference-schema"
        if check:
            for source in check["source_ids"]:
                item = supplied.get(source)
                if item is None:
                    check_error = "unknown-reference"
                    break
                roots.update(item.get("sourceIds") or [source])
                if item.get("recordedAt") and datetime.fromisoformat(item["recordedAt"]) > captured.frame_captured_at:
                    check_error = "reference-recorded-after-frame"
                if (item.get("frame") or {}).get("sha256") and (item["frame"]["sha256"] == (captured.media or {}).get("sha256")):
                    check_error = "same-frame-is-not-independent-evidence"
            if any(type(index) is not int or not 0 <= index < len(hypothesis.visible_evidence) for index in check["evidence_indices"]):
                check_error = "unknown-visible-evidence"
        question = hypothesis.clarification
        fingerprint = hashlib.sha256(" ".join((question or "").split()).casefold().encode()).hexdigest()
        prior = self.rows.get(hypothesis.follow_up_of)
        if hypothesis.follow_up_of not in {row.get("candidateId") for row in reference.get("questionHistory", [])}:
            prior = None
        def same_issue(row):
            return question and row.get("fingerprint") == fingerprint and (
                not hypothesis.salient_change or (row.get("frame") or {}).get("sha256") == (captured.media or {}).get("sha256"))
        exact = self.offered.get(fingerprint)
        duplicate = prior or (exact if exact and same_issue(exact) else None) or next((row for row in reversed(self.recent(64)) if same_issue(row)), None)
        offered_at = (duplicate or {}).get("sentAt") or (duplicate or {}).get("previouslyOfferedAt")
        now = datetime.now(timezone.utc)
        recent_output = self.last_sent_at and (now - datetime.fromisoformat(self.last_sent_at)).total_seconds() < cooldown
        status, reason = "recorded", "no-useful-question"
        if question:
            if check_error or (hypothesis.follow_up_of and prior is None):
                status, reason = "suppressed", check_error or "unknown-follow-up"
            elif offered_at:
                status, reason = "suppressed", "already-offered"
            elif not fresh:
                status, reason = "deferred", "stale-frame"
            elif not enabled:
                status, reason = "deferred", "questions-disabled"
            elif not allowed:
                status, reason = "deferred", "output-not-permitted"
            elif pending:
                status, reason = "deferred", "pending-clarification"
            elif busy:
                status, reason = "deferred", "interaction-busy"
            elif recent_output:
                status, reason = "deferred", "cooldown"
            elif not hypothesis.salient_change and not check and not duplicate:
                status, reason = "suppressed", "no-material-change"
            else:
                status, reason = "ready", "useful-question-at-idle"
        return self.record({
            "candidateId": observation["observation_id"], "observationEventId": observation["id"],
            "capturedAt": captured.frame_captured_at.isoformat(), "frame": captured.media,
            "question": question, "fingerprint": fingerprint, "follows": duplicate.get("candidateId") if duplicate else None,
            "referenceCheck": check, "referenceStatus": check_error or ("source-bound-hypothesis" if check else "not-requested"),
            "rawReferenceCheck": raw_check,
            "references": [{"id": source, **supplied[source]} for source in (check or {}).get("source_ids", []) if source in supplied],
            "memoryCandidates": [item for item in reference.get("memory", []) if item.get("kind") in {"fact", "state", "preference", "event"}],
            "previouslyOfferedAt": offered_at,
            "sourceRoots": sorted(roots), "visibleEvidence": hypothesis.visible_evidence,
            "status": status, "reason": reason, "claim": "model-comparison-not-confirmed-fact",
        })

    def audio_sent(self, candidate_id, timeline, *, user_id, case_id, response_id):
        row = self.rows.get(candidate_id)
        if not row or row["status"] != "ready":
            return
        if timeline.pending_draft(user_id=user_id, case_id=case_id):
            self.record(row, status="deferred", reason="pending-clarification")
            return
        draft = timeline.begin_draft(user_id=user_id, case_id=case_id, predicate="memory.value",
                                     missing_fields=[row["question"]], language="zh",
                                     source_ids=[row["observationEventId"]])
        self.record(row, status="audio-sent", reason="awaiting-device-playback", draftId=draft["id"],
                    responseId=response_id, sentAt=datetime.now(timezone.utc).isoformat())

    def played(self, candidate_id):
        row = self.rows.get(candidate_id)
        if row and row["status"] == "audio-sent":
            self.record(row, status="played", reason="device-playback-confirmed")

    def interrupted(self, candidate_id):
        row = self.rows.get(candidate_id)
        if row and row["status"] == "ready":
            self.record(row, status="deferred", reason="output-interrupted-before-audio")
