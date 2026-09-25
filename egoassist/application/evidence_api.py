from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from egoassist.core.settings import asr_settings, perception_settings, tool_settings
from egoassist.memory.recovery import recovery_jobs
from egoassist.product import HumanHelpRequest, OutcomeRecord
from egoassist.prompts import prompt_catalog
from egoassist.realtime import CaseWorkLedger
from egoassist.realtime.prompt_audit import audit_page


@dataclass
class EvidenceEndpoints:
    apply_memory_hint: Any
    artifact_port: Any
    artifacts_for_runtime: Any
    case_owner: Any
    episodic_for_user: Any
    get_state: Any
    human_help_port: Any
    identity: Any
    outcome_ledger: Any
    product_database: Any
    request_timeline_consolidation: Any
    runtime_checkpoint: Any
    runtime_for_user: Any
    scoped_episodic_memory: Any
    scoped_state_for_user: Any
    user_key: Any
    validate_external_observation: Any
    web: Any

    async def get_scene_media(self, request):
        user_id = request[self.user_key]["id"]
        session_id = str(request.query.get("session", ""))
        self.identity.get_context_session(user_id, session_id)
        scene = self.case_owner(user_id, session_id).scene
        asset = str(request.query.get("asset", ""))
        if not re.fullmatch("[a-f0-9]{64}\\.(jpg|wav)", asset):
            raise self.web.HTTPBadRequest(text="invalid scene asset")
        path = scene.root / asset
        if not path.is_file():
            raise self.web.HTTPNotFound()
        return self.web.FileResponse(
            path, headers={"Cache-Control": "private, no-store"}
        )

    async def post_scene_identity(self, request):
        user_id = request[self.user_key]["id"]
        value = await request.json()
        session_id = str(value.get("sessionId", ""))
        self.identity.get_context_session(user_id, session_id)
        owner = self.case_owner(user_id, session_id)
        person_ref = value.get("personRef") or None
        if person_ref and person_ref not in {
            p["personRef"]
            for row in owner.journal.entries()
            if row.get("type") == "scene.result"
            for p in row.get("people", [])
        }:
            raise self.web.HTTPBadRequest(text="unknown visual person reference")
        async with owner.mutation_lock:
            owner.scene.speakers.confirm(
                value.get("speakerId"),
                name=value.get("name"),
                wearer=value.get("wearer", False),
                person_ref=person_ref,
            )
        return self.web.json_response(
            {"state": self.scoped_state_for_user(user_id, session_id)}
        )

    async def post_long_events(self, request):
        user_id = request[self.user_key]['id']
        value = await request.json()
        session_id = str(value.get('sessionId', ''))
        self.identity.get_context_session(user_id, session_id)
        tracker = self.case_owner(user_id, session_id).scene.events
        if not tracker.service.key:
            raise ValueError('未配置事件整理模型密钥')
        if value.get('action') == 'rebuild':
            await tracker.rebuild()
        elif value.get('action') == 'refresh':
            tracker.schedule(force=True)
        else:
            raise ValueError('invalid long event action')
        return self.web.json_response({'state': self.scoped_state_for_user(user_id, session_id)})

    async def get_event_evidence(self, request):
        user_id = request[self.user_key]['id']
        session_id = str(request.query.get('session', ''))
        self.identity.get_context_session(user_id, session_id)
        tracker = self.case_owner(user_id, session_id).scene.events
        return self.web.json_response(tracker.evidence(str(request.query.get('eventId', '')),
            offset=int(request.query.get('offset', '0'))))

    async def post_speaker_merge(self, request):
        user_id = request[self.user_key]['id']
        value = await request.json()
        session_id = str(value.get('sessionId', ''))
        self.identity.get_context_session(user_id, session_id)
        owner = self.case_owner(user_id, session_id)
        async with owner.mutation_lock:
            if value.get('action') == 'merge':
                owner.scene.speakers.merge(value.get('sourceId'), value.get('targetId'))
            elif value.get('action') == 'undo':
                owner.scene.speakers.undo_merge(value.get('mergeId'))
            else:
                raise ValueError('invalid speaker merge action')
        return self.web.json_response({'state': self.scoped_state_for_user(user_id, session_id)})

    async def get_prompt_audit(self, request):
        user_id = request[self.user_key]["id"]
        session_id = str(request.query.get("session", "")).strip()
        self.identity.get_context_session(user_id, session_id)
        owner = self.case_owner(user_id, session_id)
        rows = owner.journal.entries()
        return self.web.json_response(
            {
                "templates": prompt_catalog(),
                **audit_page(
                    rows,
                    before=request.query.get("before"),
                    limit=int(request.query.get("limit", "30")),
                ),
                "sessionId": session_id,
                "configuration": {
                    "asr": asr_settings(),
                    "perception": perception_settings(),
                    "tools": tool_settings(),
                },
                "memoryRecovery": list(recovery_jobs(owner.journal).values()),
            }
        )

    async def get_conversation_history(self, request):
        from egoassist.product.conversation_history import conversation_page
        user_id = request[self.user_key]['id']
        session_id = str(request.query.get('session', '')).strip()
        self.identity.get_context_session(user_id, session_id)
        owner = self.case_owner(user_id, session_id)
        journal = owner.journal
        try:
            page = conversation_page(journal, before=request.query.get('before'),
                                     limit=request.query.get('limit', '50'), scene=owner.scene)
        except ValueError as exc:
            raise self.web.HTTPBadRequest(text=str(exc)) from exc
        return self.web.json_response({'sessionId': session_id, **page},
                                      headers={'Cache-Control': 'private, no-store'})

    async def get_artifacts(self, request):
        user_id = request[self.user_key]["id"]
        session_id = str(request.query.get("session", "")).strip()
        if not session_id:
            raise ValueError("session is required")
        self.identity.get_context_session(user_id, session_id)
        owner = self.case_owner(user_id, session_id)
        return self.web.json_response(
            {
                "artifacts": self.artifacts_for_runtime(owner.runtime, user_id),
                "sessionId": session_id,
            }
        )

    async def get_background_tasks(self, request):
        """Phase 2A: Task queue — background intent execution status snapshot."""
        user_id = request[self.user_key]["id"]
        session_id = str(request.query.get("session", "")).strip() or None
        if not session_id:
            sessions = self.identity.list_context_sessions(user_id)
            if not sessions:
                return self.web.json_response(
                    {"error": "no sessions found"}, status=404
                )
            session_id = sessions[0]["id"]
        self.identity.get_context_session(user_id, session_id)
        owner = self.case_owner(user_id, session_id)
        slots = [
            {
                "taskId": s.task_id,
                "kind": s.kind,
                "textPreview": s.text[:80],
                "status": s.status,
                "createdAt": s.created_at,
                "completedAt": s.completed_at,
                "resultPreview": s.result_preview,
                "turnId": s.turn_id,
                "durationMs": round((s.completed_at - s.created_at) * 1000)
                if s.completed_at
                else None,
            }
            for s in owner.background_tasks.values()
        ]
        return self.web.json_response(
            {
                "sessionId": session_id,
                "timestamp": time.time(),
                "tasks": list(reversed(slots)),
                "running": sum(
                    (
                        1
                        for s in owner.background_tasks.values()
                        if s.status == "running"
                    )
                ),
                "pending": sum(
                    (
                        1
                        for s in owner.background_tasks.values()
                        if s.status == "pending"
                    )
                ),
            }
        )

    async def get_artifact_content(self, request):
        user_id = request[self.user_key]["id"]
        session_id = str(request.query.get("session", "")).strip()
        artifact_id = str(request.match_info.get("artifact_id", "")).strip()
        if not session_id or not artifact_id:
            raise ValueError("session and artifact id are required")
        self.identity.get_context_session(user_id, session_id)
        owner = self.case_owner(user_id, session_id)
        authorized = {
            item["artifact_id"]: item
            for item in self.artifacts_for_runtime(owner.runtime, user_id)
        }
        descriptor = authorized.get(artifact_id)
        if descriptor is None:
            raise self.web.HTTPNotFound(
                text="artifact is not authorized for the current checkpoint"
            )
        path, media_type = self.artifact_port.content(artifact_id)
        owner.journal.append(
            "artifact_access_receipt",
            {
                "artifactId": artifact_id,
                "userId": user_id,
                "skill": descriptor.get("provenance", {}).get("skill"),
                "task": owner.runtime.task.spec.id if owner.runtime.task else None,
                "checkpoint": owner.runtime.task.current_step.id
                if owner.runtime.task
                else None,
                "checksum": descriptor.get("checksum"),
                "status": "resolved",
            },
        )
        return self.web.FileResponse(
            path,
            headers={
                "Content-Type": media_type,
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def post_human_help_preview(self, request):
        if self.human_help_port is None:
            raise self.web.HTTPServiceUnavailable(
                text="human-help connector is not configured", content_type="text/plain"
            )
        user_id = request[self.user_key]["id"]
        payload = await request.json()
        session_id = str(payload.get("sessionId", "")).strip()
        purpose = str(payload.get("purpose", "")).strip()
        message = str(payload.get("message", "")).strip()
        if not session_id or not purpose or (not message):
            raise ValueError("sessionId, purpose, and message are required")
        if len(purpose) > 200 or len(message) > 2000:
            raise ValueError("human-help preview exceeds the supported length")
        self.identity.get_context_session(user_id, session_id)
        owner = self.case_owner(user_id, session_id)
        preview_id = uuid4().hex[:16]
        request_id = f"help-{uuid4().hex[:16]}"
        consent_id = f"consent-{uuid4().hex[:16]}"
        task = owner.runtime.task
        preview = {
            "purpose": purpose,
            "message": message,
            "taskContext": {
                "skill": owner.runtime.skill.name if owner.runtime.skill else None,
                "task": task.spec.id if task else None,
                "checkpoint": task.current_step.id if task else None,
            },
            "dataCategories": ["user-authored-message", "current-task-context"],
            "destination": self.human_help_port.public_summary()["endpointOrigin"],
        }
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
        owner.journal.append(
            "human_help_preview",
            {
                "previewId": preview_id,
                "requestId": request_id,
                "consentId": consent_id,
                "turnId": f"turn-{request_id}",
                "intentId": f"intent-{request_id}",
                "jobId": f"job-{request_id}",
                "resultId": f"result-{request_id}",
                "preview": preview,
                "expiresAt": expires_at.isoformat(),
                "status": "awaiting-consent",
            },
            idempotency_key=f"human-help-preview:{preview_id}",
        )
        return self.web.json_response(
            {
                "previewId": preview_id,
                "preview": preview,
                "expiresAt": expires_at.isoformat(),
                "consentRequired": True,
            }
        )

    async def post_human_help_submit(self, request):
        if self.human_help_port is None:
            raise self.web.HTTPServiceUnavailable(
                text="human-help connector is not configured", content_type="text/plain"
            )
        user_id = request[self.user_key]["id"]
        payload = await request.json()
        session_id = str(payload.get("sessionId", "")).strip()
        preview_id = str(payload.get("previewId", "")).strip()
        if not session_id or not preview_id or payload.get("accepted") is not True:
            raise ValueError(
                "sessionId, previewId, and explicit accepted=true are required"
            )
        self.identity.get_context_session(user_id, session_id)
        owner = self.case_owner(user_id, session_id)
        async with owner.mutation_lock:
            entries = owner.journal.entries()
            preview_entry = next(
                (
                    item
                    for item in reversed(entries)
                    if item.get("type") == "human_help_preview"
                    and str(item.get("previewId", "")) == preview_id
                ),
                None,
            )
            if preview_entry is None:
                raise ValueError("human-help preview not found")
            existing_receipt = next(
                (
                    item
                    for item in reversed(entries)
                    if item.get("type") == "human_help_receipt"
                    and str(item.get("previewId", "")) == preview_id
                ),
                None,
            )
            if existing_receipt is not None:
                return self.web.json_response(
                    {
                        "receipt": dict(existing_receipt.get("receipt") or {}),
                        "deduplicated": True,
                        "state": self.scoped_state_for_user(
                            user_id, session_id, runtime_override=owner.runtime
                        ),
                    }
                )
            expires_at = datetime.fromisoformat(
                str(preview_entry["expiresAt"]).replace("Z", "+00:00")
            )
            if expires_at < datetime.now(timezone.utc):
                raise ValueError("human-help preview has expired")
            consent_id = str(preview_entry["consentId"])
            owner.journal.append(
                "human_help_consent",
                {
                    "consentId": consent_id,
                    "previewId": preview_id,
                    "requestId": preview_entry["requestId"],
                    "purpose": preview_entry["preview"]["purpose"],
                    "dataCategories": preview_entry["preview"]["dataCategories"],
                    "destination": preview_entry["preview"]["destination"],
                    "status": "granted",
                },
                idempotency_key=f"human-help-consent:{consent_id}",
            )
            ledger = owner.work_ledger or CaseWorkLedger(owner.journal)
            turn = ledger.capture_turn(
                preview_entry["preview"]["message"],
                source="explicit-human-help-request",
                turn_id=str(preview_entry["turnId"]),
            )
            intent = ledger.create_intent(
                turn["turnId"],
                intent_id=str(preview_entry["intentId"]),
                kind="background-job",
                status="running-background",
                payload={
                    "purpose": preview_entry["preview"]["purpose"],
                    "previewId": preview_id,
                },
                skill_ref=preview_entry["preview"]["taskContext"],
            )
            job = ledger.create_job(
                intent["intentId"],
                job_id=str(preview_entry["jobId"]),
                kind="human-help-submit",
                status="running",
                payload={
                    "requestId": preview_entry["requestId"],
                    "previewId": preview_id,
                    "connector": "webhook",
                },
            )
            connector_request = HumanHelpRequest(
                request_id=str(preview_entry["requestId"]),
                user_id=user_id,
                intent_id=intent["intentId"],
                connector="webhook",
                purpose=str(preview_entry["preview"]["purpose"]),
                preview=dict(preview_entry["preview"]),
                consent_id=consent_id,
                idempotency_key=f"human-help:{preview_entry['requestId']}",
            )
            owner.journal.append(
                "human_help_request",
                {
                    "previewId": preview_id,
                    "request": connector_request.model_dump(mode="json"),
                    "status": "submitting",
                },
                idempotency_key=f"human-help-request:{preview_entry['requestId']}",
            )
            receipt = await self.human_help_port.submit(connector_request)
            receipt_payload = receipt.model_dump(mode="json")
            self.outcome_ledger.record(
                OutcomeRecord(
                    outcome_id=f"connector:{preview_entry['requestId']}",
                    kind="human-help",
                    expected="acknowledged",
                    observed=str(receipt.status),
                    source="connector-receipt",
                    acknowledged=receipt.status in {"acknowledged", "resolved"},
                    user_id=user_id,
                    session_id=session_id,
                )
            )
            owner.journal.append(
                "human_help_receipt",
                {
                    "previewId": preview_id,
                    "requestId": preview_entry["requestId"],
                    "receipt": receipt_payload,
                    "status": receipt.status,
                },
                idempotency_key=f"human-help-receipt:{preview_entry['requestId']}",
            )
            succeeded = receipt.status in {"submitted", "acknowledged", "resolved"}
            result = ledger.enqueue_result(
                intent["intentId"],
                {
                    "kind": "human-help",
                    "purpose": connector_request.purpose,
                    "status": receipt.status,
                    "message": receipt.message,
                    "error": receipt.error,
                    "externalRequestId": receipt.external_request_id,
                },
                job_id=job["jobId"],
                result_id=str(preview_entry["resultId"]),
                priority=90 if succeeded else 100,
                delivery="display",
            )
            ledger.set_job_status(
                job["jobId"],
                "completed" if succeeded else "failed",
                detail={
                    "resultId": result["resultId"],
                    "receiptStatus": receipt.status,
                },
            )
            ledger.set_intent_status(
                intent["intentId"],
                "completed" if succeeded else "failed",
                detail={
                    "resultId": result["resultId"],
                    "receiptStatus": receipt.status,
                },
            )
            return self.web.json_response(
                {
                    "receipt": receipt_payload,
                    "deduplicated": False,
                    "state": self.scoped_state_for_user(
                        user_id, session_id, runtime_override=owner.runtime
                    ),
                }
            )

    async def post_observation(self, request):
        user_id = request[self.user_key]["id"]
        observation = await request.json()
        session_id = str(observation.get("sessionId", "")).strip() or None
        if session_id:
            self.identity.get_context_session(user_id, session_id)
            owner = self.case_owner(user_id, session_id)
            policy = self.identity.get_context_session(user_id, session_id)[
                "memoryPolicy"
            ]
            async with owner.mutation_lock:
                if owner.runtime.task is None:
                    raise ValueError("no active task for structured observation")
                evidence = self.validate_external_observation(
                    owner.runtime, observation, user_id=user_id, session_id=session_id
                )
                owner.runtime.task.ingest(evidence.model_dump(mode="json"))
                self.product_database.record_observation(evidence)
                episode = self.scoped_episodic_memory(
                    user_id, session_id, policy
                ).remember(
                    str(evidence.evidence.get("summary", "")),
                    kind="observation",
                    source=evidence.source,
                    skill=owner.runtime.skill.name,
                    task=owner.runtime.task.spec.id,
                    session_id=session_id,
                    user_scope=user_id,
                )
                self.apply_memory_hint(owner.runtime)
                owner.journal.append_state_checkpoint(
                    self.runtime_checkpoint(owner.runtime),
                    reason="structured-observation",
                )
            if (
                episode
                and episode.get("status") == "active"
                and (episode.get("tier") == "short")
            ):
                self.request_timeline_consolidation(owner)
            return self.web.json_response(
                self.scoped_state_for_user(user_id, session_id)
            )
        active_runtime = self.runtime_for_user(user_id)
        if active_runtime.task is None:
            raise ValueError("no active task for structured observation")
        evidence = self.validate_external_observation(
            active_runtime, observation, user_id=user_id, session_id="local-default"
        )
        active_runtime.task.ingest(evidence.model_dump(mode="json"))
        self.product_database.record_observation(evidence)
        self.episodic_for_user(user_id).remember(
            str(evidence.evidence.get("summary", "")),
            kind="observation",
            source=evidence.source,
            skill=active_runtime.skill.name,
            task=active_runtime.task.spec.id,
            user_scope=user_id,
        )
        self.apply_memory_hint(active_runtime)
        return await self.get_state(request)

    async def post_replay_observation(self, request):
        user_id = request[self.user_key]["id"]
        payload = await request.json()
        session_id = str(payload.get("sessionId", "")).strip() or None
        if session_id:
            self.identity.get_context_session(user_id, session_id)
            owner = self.case_owner(user_id, session_id)
            policy = self.identity.get_context_session(user_id, session_id)[
                "memoryPolicy"
            ]
            async with owner.mutation_lock:
                if owner.runtime.task is None:
                    raise ValueError("no active task for replay observation")
                observation = owner.runtime.task.replay_observation(
                    str(payload.get("kind", "progress"))
                )
                owner.runtime.task.ingest(observation)
                if payload.get("expectedKind"):
                    self.outcome_ledger.record(
                        OutcomeRecord(
                            outcome_id=f"perception:{uuid4().hex[:16]}",
                            kind="perception-sequence",
                            expected=str(payload["expectedKind"]),
                            observed=str(payload.get("kind", "progress")),
                            source="annotated-sequence",
                            acknowledged=False,
                            user_id=user_id,
                            session_id=session_id,
                        )
                    )
                episode = self.scoped_episodic_memory(
                    user_id, session_id, policy
                ).remember(
                    observation.get("summary", ""),
                    kind="observation",
                    source=str(observation.get("source", "research-replay")),
                    skill=owner.runtime.skill.name,
                    task=owner.runtime.task.spec.id,
                    session_id=session_id,
                    user_scope=user_id,
                )
                self.apply_memory_hint(owner.runtime)
                owner.journal.append_state_checkpoint(
                    self.runtime_checkpoint(owner.runtime), reason="replay-observation"
                )
            if (
                episode
                and episode.get("status") == "active"
                and (episode.get("tier") == "short")
            ):
                self.request_timeline_consolidation(owner)
            return self.web.json_response(
                self.scoped_state_for_user(user_id, session_id)
            )
        active_runtime = self.runtime_for_user(user_id)
        if active_runtime.task is None:
            raise ValueError("no active task for replay observation")
        observation = active_runtime.task.replay_observation(
            str(payload.get("kind", "progress"))
        )
        active_runtime.task.ingest(observation)
        if payload.get("expectedKind"):
            self.outcome_ledger.record(
                OutcomeRecord(
                    outcome_id=f"perception:{uuid4().hex[:16]}",
                    kind="perception-sequence",
                    expected=str(payload["expectedKind"]),
                    observed=str(payload.get("kind", "progress")),
                    source="annotated-sequence",
                    acknowledged=False,
                    user_id=user_id,
                )
            )
        self.episodic_for_user(user_id).remember(
            observation.get("summary", ""),
            kind="observation",
            source=str(observation.get("source", "research-replay")),
            skill=active_runtime.skill.name,
            task=active_runtime.task.spec.id,
            user_scope=user_id,
        )
        self.apply_memory_hint(active_runtime)
        return await self.get_state(request)
