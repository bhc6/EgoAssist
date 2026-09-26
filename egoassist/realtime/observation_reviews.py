"""Source-bound observation reviews and durable, non-interrupting clarification gates."""
from datetime import datetime, timezone
import hashlib

from egoassist.core.contracts import ObservationReferenceCheck, ObservationNotification


class ObservationReviews:
    def __init__(self, journal):
        self.journal = journal
        self.rows = {}
        self.offered = {}
        self.last_sent_at = None
        for row in journal.entries(types={"observation.review"}):
            if row.get("type") == "observation.review":
                self.rows[row["candidateId"]] = row
                self._index_offered(row)
        # No scheduler requests survive process restart. Reconsider on new evidence.
        for row in list(self.rows.values()):
            if row["status"] == "ready":
                self.record(row, status="deferred", reason="delivery-not-confirmed")

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
        rows = (row for row in self.rows.values() if not questions_only or row.get("question") or row.get("notification"))
        return sorted(rows, key=lambda row: row["timestamp"])[-limit:]

    def reconcile(self, pending):
        for row in list(self.rows.values()):
            if row.get("draftId") and row["status"] in {"audio-sent", "played"} and row["draftId"] != (pending or {}).get("id"):
                self.record(row, status="closed", reason="clarification-no-longer-pending")

    def consider(self, captured, hypothesis, observation, *, fresh, enabled, busy, allowed, pending, cooldown,
                 reminders_enabled=False, output_permission=None):
        self.reconcile(pending)
        reference = captured.reference_context or {}
        supplied = {}
        for item in [*reference.get("userMessages", []), *reference.get("reminderGoals", [])]:
            supplied[item["sourceId"]] = {**item, "sourceIds": [item["sourceId"]]}
        for item in reference.get("memory", []):
            supplied[item["id"]] = item
        aliases = {item["referenceId"]: item.get("id", item.get("sourceId")) for item in
                   [*reference.get("userMessages", []), *reference.get("memory", []),
                    *reference.get("reminderGoals", [])] if item.get("referenceId")}
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
        notification, notification_error = None, None
        if hypothesis.notification is not None:
            try:
                notification = ObservationNotification.model_validate(hypothesis.notification, strict=True).model_dump()
                notification["source_ids"] = [aliases.get(source, source) for source in notification["source_ids"]]
                indices = notification["evidence_indices"]
                if any(not 0 <= index < len(hypothesis.visible_evidence) for index in indices):
                    notification_error = "unknown-visible-evidence"
                requested_sources = {item["sourceId"] for item in
                    [*reference.get("userMessages", []), *reference.get("reminderGoals", [])]}
                if notification["kind"] != "hazard" and not notification["source_ids"]:
                    notification_error = "missing-user-need"
                if notification["kind"] == "requested-reminder" and not (set(notification["source_ids"]) & requested_sources):
                    notification_error = "missing-user-request"
                for source in notification["source_ids"]:
                    item = supplied.get(source)
                    if item is None:
                        notification_error = "unknown-reference"
                        continue
                    roots.update(item.get("sourceIds") or [source])
                    if item.get("recordedAt") and datetime.fromisoformat(item["recordedAt"]) > captured.frame_captured_at:
                        notification_error = "reference-recorded-after-frame"
                    if (item.get("frame") or {}).get("sha256") == (captured.media or {}).get("sha256") and item.get("frame"):
                        notification_error = "same-frame-is-not-independent-evidence"
                if not notification["message"].strip() or not notification["reason"].strip():
                    notification_error = "invalid-notification-schema"
            except ValueError:
                notification_error = "invalid-notification-schema"
        is_reminder = hypothesis.notification is not None
        question = None if is_reminder else hypothesis.clarification
        utterance = (notification or {}).get("message") if is_reminder else question
        # A goal is offered once, even when the next frame paraphrases the same reminder.
        notification_sources = set((notification or {}).get("source_ids", []))
        goal_sources = notification_sources & {item["sourceId"] for item in reference.get("reminderGoals", [])}
        need_sources = goal_sources or {root for source in notification_sources
            for root in (supplied.get(source, {}).get("sourceIds") or [source])}
        key = ("need:" + ",".join(sorted(need_sources))) if need_sources else (
            "hazard-frame:" + captured.media["sha256"] if notification and (captured.media or {}).get("sha256") else (utterance or ""))
        fingerprint = hashlib.sha256(" ".join(key.split()).casefold().encode()).hexdigest()
        prior = self.rows.get(hypothesis.follow_up_of)
        if hypothesis.follow_up_of not in {row.get("candidateId") for row in reference.get("questionHistory", [])}:
            prior = None
        known_follow_up = prior is not None
        if notification_sources and prior and prior.get("fingerprint") != fingerprint:
            prior = None  # A newly configured need must not inherit an old goal's delivery.
        def same_issue(row):
            return utterance and row.get("fingerprint") == fingerprint and (
                is_reminder or not hypothesis.salient_change or (row.get("frame") or {}).get("sha256") == (captured.media or {}).get("sha256"))
        exact = self.offered.get(fingerprint)
        duplicate = prior or (exact if exact and same_issue(exact) else None) or next((row for row in reversed(self.recent(64)) if same_issue(row)), None)
        queued = any(row["status"] == "ready" and same_issue(row) for row in self.rows.values())
        offered_at = (duplicate or {}).get("sentAt") or (duplicate or {}).get("previouslyOfferedAt")
        now = datetime.now(timezone.utc)
        recent_output = self.last_sent_at and (now - datetime.fromisoformat(self.last_sent_at)).total_seconds() < cooldown
        status, reason = "recorded", "no-useful-question"
        if utterance or is_reminder:
            if notification_error or check_error or (hypothesis.follow_up_of and not known_follow_up):
                status, reason = "suppressed", notification_error or check_error or "unknown-follow-up"
            elif offered_at:
                status, reason = "suppressed", "already-offered"
            elif queued:
                status, reason = "deferred", "already-queued"
            elif not fresh:
                status, reason = "deferred", "stale-frame"
            elif not (reminders_enabled if is_reminder else enabled):
                status, reason = "deferred", "reminders-disabled" if is_reminder else "questions-disabled"
            elif not allowed:
                status, reason = "deferred", "output-not-permitted"
            elif pending and not is_reminder:
                status, reason = "deferred", "pending-clarification"
            elif busy:
                status, reason = "deferred", "interaction-busy"
            elif recent_output:
                status, reason = "deferred", "cooldown"
            elif not is_reminder and not hypothesis.salient_change and not check and not duplicate:
                status, reason = "suppressed", "no-material-change"
            else:
                status, reason = "ready", "useful-reminder-at-idle" if is_reminder else "useful-question-at-idle"
        return self.record({
            "candidateId": observation["observation_id"], "observationEventId": observation["id"],
            "capturedAt": captured.frame_captured_at.isoformat(), "frame": captured.media,
            "question": question, "fingerprint": fingerprint, "follows": duplicate.get("candidateId") if duplicate else None,
            "notification": notification, "rawNotification": hypothesis.notification,
            "reminderGoalSourceIds": [item["sourceId"] for item in reference.get("reminderGoals", [])],
            "referenceCheck": check, "referenceStatus": check_error or ("source-bound-hypothesis" if check else "not-requested"),
            "rawReferenceCheck": raw_check,
            "references": [{"id": source, **supplied[source]} for source in dict.fromkeys(
                [*(check or {}).get("source_ids", []), *(notification or {}).get("source_ids", [])]) if source in supplied],
            "memoryCandidates": [item for item in reference.get("memory", []) if item.get("kind") in {"fact", "state", "preference", "event"}],
            "previouslyOfferedAt": offered_at,
            "sourceRoots": sorted(roots), "visibleEvidence": hypothesis.visible_evidence,
            "status": status, "reason": reason, "claim": "model-comparison-not-confirmed-fact",
            "outputPermission": output_permission if reason == 'output-not-permitted' else None,
        })

    def audio_sent(self, candidate_id, timeline, *, user_id, case_id, response_id):
        row = self.rows.get(candidate_id)
        if not row or row["status"] != "ready":
            return
        if row.get("notification"):
            self.record(row, status="audio-sent", reason="awaiting-device-playback",
                        responseId=response_id, sentAt=datetime.now(timezone.utc).isoformat())
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
