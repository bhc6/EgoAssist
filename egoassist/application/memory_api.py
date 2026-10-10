from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

from egoassist.product.insight_jobs import InsightQueueFull, visible_insight_jobs
from egoassist.realtime import default_memory_policy, memory_profiles
from egoassist.timefmt import format_beijing_iso


@dataclass
class MemoryEndpoints:
    retention: Any
    AUTH_COOKIE: Any
    ambient_consent: Any
    ambient_settings: Any
    ambient_status: Any
    answer_executor: Any
    append_memory_audit: Any
    case_episodics: Any
    case_journal: Any
    case_owner: Any
    case_owners: Any
    clock_for_session: Any
    deleting_insight_users: Any
    identity: Any
    insight_executor: Any
    invalidate_live_sessions: Any
    memory_recovery: Any
    outcome_ledger: Any
    privacy_governance: Any
    product_database: Any
    projection_cache: Any
    scoped_episodic_memory: Any
    scoped_state_for_user: Any
    timeline: Any
    user_episodics: Any
    user_key: Any
    user_routing: Any
    user_runtimes: Any
    user_tools: Any
    web: Any
    memory_service: Any = None

    async def post_memory_policy(self, request):
        user = request[self.user_key]
        user_id = user["id"]
        payload = await request.json()
        session_id = str(payload.get("sessionId", "")).strip()
        if not session_id:
            raise ValueError("memory policy requires an experiment case id")
        experiment_case = self.identity.get_context_session(user_id, session_id)
        previous = experiment_case["memoryPolicy"]
        requested = payload.get("policy")
        if requested is None:
            requested = {
                "profile": payload.get("profile", "all-online"),
                "components": payload.get("components", {}),
            }
        if not isinstance(requested, dict):
            raise ValueError("memory policy must be an object")
        updated = self.identity.update_memory_policy(user_id, session_id, requested)
        try:
            if updated != previous:
                journal = self.case_journal(
                    user_id, session_id, initial_memory_policy=previous
                )
                journal.append(
                    "memory_policy_changed",
                    {
                        "previous": previous,
                        "policy": updated,
                        "source": "research-console",
                    },
                )
        except Exception:
            self.identity.update_memory_policy(user_id, session_id, previous)
            raise
        return self.web.json_response(
            {
                "session": self.identity.get_context_session(user_id, session_id),
                "memoryPolicy": updated,
                "profiles": memory_profiles(),
            }
        )

    async def post_memory_recovery_retry(self, request):
        user_id = request[self.user_key]["id"]
        payload = await request.json()
        session_id = str(payload.get("sessionId", ""))
        self.identity.get_context_session(user_id, session_id)
        self.memory_recovery.retry(
            self.case_owner(user_id, session_id).journal, str(payload.get("turnId", ""))
        )
        return self.web.json_response({"status": "queued"})

    async def get_insights(self, request):
        user_id = request[self.user_key]["id"]
        session_id = str(request.query.get("session", "")).strip()
        self.identity.get_context_session(user_id, session_id)
        journal = self.case_owner(user_id, session_id).journal
        self.insight_executor.recover(user_id, session_id, journal)
        policies = journal.entries(types={"insight.policy"}, tail=1)
        policy = policies[-1] if policies else {}
        results = []
        result_id = request.query.get("result")
        if request.query.get("includeResults") != "false":
            if result_id:
                row = journal.entries_by_id([result_id]).get(result_id)
                results = [row] if row and row.get("type") == "insight.result" else []
            else:
                results = journal.entries(types={"insight.result"}, tail=1)
        return self.web.json_response(
            {
                "results": results[-1:],
                "jobs": visible_insight_jobs(journal),
                "proactiveEnabled": policy.get("enabled", False),
                "sessionId": session_id,
            }
        )

    async def post_insight_cancel(self, request):
        payload = await request.json()
        user_id = request[self.user_key]["id"]
        session_id = str(payload.get("sessionId", ""))
        self.identity.get_context_session(user_id, session_id)
        journal = self.case_owner(user_id, session_id).journal
        await self.insight_executor.cancel(str(payload.get("jobId", "")), journal)
        return self.web.json_response({"jobs": visible_insight_jobs(journal)})

    async def post_insight_policy(self, request):
        payload = await request.json()
        user_id = request[self.user_key]["id"]
        session_id = str(payload.get("sessionId", ""))
        self.identity.get_context_session(user_id, session_id)
        if type(payload.get("enabled")) is not bool:
            raise ValueError("enabled must be a boolean")
        entry = self.case_owner(user_id, session_id).journal.append(
            "insight.policy",
            {
                "enabled": payload["enabled"],
                "execution": "online-console",
                "scope": "case",
            },
        )
        return self.web.json_response(entry)

    async def post_insight(self, request):
        payload = await request.json()
        user_id = request[self.user_key]["id"]
        if user_id in self.deleting_insight_users:
            raise self.web.HTTPConflict(text="Account deletion is in progress")
        session_id = str(payload.get("sessionId", ""))
        self.identity.get_context_session(user_id, session_id)
        owner = self.case_owner(user_id, session_id)
        scope = payload.get("scope", "case")
        if scope not in {"case", "account"}:
            raise ValueError("invalid insight scope")
        kind = payload.get("kind", "daily")
        if payload.get("automatic") is True:
            policy = next(
                (
                    row
                    for row in reversed(owner.journal.entries())
                    if row.get("type") == "insight.policy"
                ),
                {},
            )
            if not policy.get("enabled") or kind != "suggestions" or scope != "case":
                raise ValueError("automatic suggestions are not enabled for this case")
        started = time.perf_counter()
        try:
            job = self.insight_executor.submit(
                user_id=user_id,
                session_id=session_id,
                journal=owner.journal,
                kind=kind,
                scope=scope,
                anchor=payload.get("anchor"),
                clock=self.clock_for_session(user_id, session_id),
            )
        except InsightQueueFull as exc:
            raise self.web.HTTPTooManyRequests(text=str(exc))
        return self.web.json_response(
            {
                "job": job,
                "acceptedMs": round((time.perf_counter() - started) * 1000, 3),
            },
            status=202,
        )

    async def get_memory_state(self, request):
        """Phase 1: Memory observability — current memory layer state snapshot."""
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
        episodic_snap = self.timeline.snapshot(
            user_id=user_id, scope="case", case_id=session_id
        )
        journal_entries = owner.journal.entries(tail=20)
        return self.web.json_response(
            {
                "sessionId": session_id,
                "timestamp": time.time(),
                "workingMemory": {
                    "entryCount": owner.journal.entry_count,
                    "manifestSnapshot": owner.harness._last_manifest,
                    "recentEntries": journal_entries[-20:],
                },
                "episodicMemory": episodic_snap,
                "recentOps": owner.memory_bus.recent(40) if owner.memory_bus else [],
                "memoryMetrics": self.memory_service.status(user_id, session_id) if self.memory_service else None,
            }
        )

    async def get_timeline_memory(self, request):
        """Unified TimelineMemory view — all kinds with Beijing time formatting."""
        from egoassist.memory.timeline import RETRIEVABLE_KINDS

        user_id = request[self.user_key]["id"]
        session_id = str(request.query.get("session", "")).strip() or None
        limit = min(int(request.query.get("limit", "60")), 200)
        if not session_id:
            sessions = self.identity.list_context_sessions(user_id)
            if not sessions:
                return self.web.json_response(
                    {"error": "no sessions found"}, status=404
                )
            session_id = sessions[0]["id"]
        self.identity.get_context_session(user_id, session_id)
        raw = self.timeline.retrieve(
            "",
            user_id=user_id,
            case_id=session_id,
            scope="case",
            kinds=RETRIEVABLE_KINDS - {"legacy"},
            limit=limit,
        )
        snap = self.timeline.snapshot(user_id=user_id, case_id=session_id, scope="case")

        def _card_view(card: dict) -> dict:
            ts = card.get("happenedAt") or card.get("recordedAt")
            return {
                "id": card["id"],
                "kind": card.get("kind", "?"),
                "tier": card.get("tier", "short"),
                "scope": card.get("scope"),
                "text": card.get("text", ""),
                "happenedAt": ts,
                "happenedAtBeijing": format_beijing_iso(ts),
                "importance": card.get("importance"),
                "status": card.get("status"),
                "subject": card.get("subject"),
                "predicate": card.get("predicate"),
                "value": card.get("value"),
                "metadata": card.get("metadata", {}),
            }

        all_candidates = raw.get("candidates", [])
        kind_counts: dict[str, int] = {}
        for c in all_candidates:
            k = c.get("card", {}).get("kind", "?")
            kind_counts[k] = kind_counts.get(k, 0) + 1
        recent_cards = [_card_view(c) for c in snap.get("recent", [])]
        return self.web.json_response(
            {
                "sessionId": session_id,
                "userId": user_id,
                "timestamp": time.time(),
                "kindCounts": kind_counts,
                "totalCards": snap.get("count", 0),
                "layers": snap.get("layers", {}),
                "recentCards": recent_cards,
                "retrievalMethod": raw.get("method"),
            }
        )

    async def post_candidate(self, request):
        user_id = request[self.user_key]["id"]
        payload = await request.json()
        session_id = str(payload.get("sessionId", "")).strip() or None
        policy = (
            self.identity.get_context_session(user_id, session_id)["memoryPolicy"]
            if session_id
            else default_memory_policy()
        )
        active_episodic = self.scoped_episodic_memory(user_id, session_id, policy)
        accepted = bool(payload.get("accepted"))
        if accepted and (not policy["components"]["timeline"]["preference_enabled"]):
            raise ValueError(
                "confirmed preference memory is disabled for this experiment case"
            )
        candidate = self.memory_service.snapshot_preference(active_episodic, candidate_scope=session_id).get(
            "candidate"
        )
        self.memory_service.resolve_preference(active_episodic, accepted, candidate_scope=session_id)
        if session_id:
            self.append_memory_audit(
                self.case_journal(user_id, session_id),
                "memory.confirmed" if accepted else "memory.rejected",
                "preference-memory",
                {
                    "memoryId": candidate["id"] if candidate else None,
                    "trigger": candidate.get("trigger") if candidate else None,
                    "preference": candidate.get("preference")
                    if candidate and accepted
                    else None,
                    "storageScope": policy["components"]["timeline"]["scope"],
                    "accepted": accepted,
                },
            )
        return self.web.json_response(self.scoped_state_for_user(user_id, session_id))

    async def get_pending_claims(self, request):
        """List inferred-consent claims awaiting user confirmation."""
        user_id = request[self.user_key]["id"]
        session_id = str(request.query.get("session", "")).strip() or None
        scope = "case" if session_id else "participant"
        if session_id:
            self.identity.get_context_session(user_id, session_id)
        try:
            limit = max(1, min(int(request.query.get("limit", "50")), 200))
        except ValueError:
            return self.web.json_response({"error": "limit must be an integer"}, status=400)
        cards = self.memory_service.pending_claims(user_id, session_id, scope=scope, limit=limit)
        return self.web.json_response({
            "sessionId": session_id,
            "scope": scope,
            "claims": [
                {
                    "id": card["id"], "kind": card["kind"], "text": card["text"],
                    "happenedAt": card.get("happenedAt"),
                    "happenedAtBeijing": format_beijing_iso(card.get("happenedAt")),
                    "subject": card.get("subject"), "predicate": card.get("predicate"),
                    "value": card.get("value"),
                    "referenceStatus": card.get("grounding", {}).get("referenceStatus"),
                }
                for card in cards
            ],
        })

    async def post_pending_claim(self, request):
        """Confirm or reject one pending claim; rejection is a soft delete."""
        user_id = request[self.user_key]["id"]
        payload = await request.json()
        card_id = str(payload.get("cardId", "")).strip()
        if not card_id:
            return self.web.json_response({"error": "cardId is required"}, status=400)
        session_id = str(payload.get("sessionId", "")).strip() or None
        if session_id:
            self.identity.get_context_session(user_id, session_id)
        accepted = bool(payload.get("accepted"))
        try:
            card = self.memory_service.resolve_pending_claim(user_id, session_id, card_id, accepted)
        except ValueError as exc:
            return self.web.json_response({"error": str(exc)}, status=404)
        if session_id:
            self.append_memory_audit(
                self.case_journal(user_id, session_id),
                "memory.confirmed" if accepted else "memory.rejected",
                "pending-claim",
                {"memoryId": card["id"], "kind": card["kind"], "accepted": accepted},
            )
        return self.web.json_response({"card": {"id": card["id"], "status": card["status"]}})

    async def post_delete(self, request):
        user_id = request[self.user_key]["id"]
        payload = await request.json()
        session_id = str(payload.get("sessionId", "")).strip() or None
        policy = (
            self.identity.get_context_session(user_id, session_id)["memoryPolicy"]
            if session_id
            else default_memory_policy()
        )
        active_episodic = self.scoped_episodic_memory(user_id, session_id, policy)
        record_id = str(payload.get("id", ""))
        record = next(
            (
                item
                for item in self.memory_service.snapshot_preference(active_episodic,
                    candidate_scope=session_id
                )["records"]
                if item["id"] == record_id
            ),
            None,
        )
        deleted_card = None
        if record is not None:
            self.memory_service.delete_preference(active_episodic, record_id, candidate_scope=session_id)
        else:
            deleted_card = self.memory_service.delete_card(user_id, session_id, record_id)
        if session_id:
            self.append_memory_audit(
                self.case_journal(user_id, session_id),
                "memory.deleted",
                "preference-memory",
                {
                    "memoryId": record_id,
                    "trigger": record.get("trigger") if record else None,
                    "kind": deleted_card.get("kind")
                    if deleted_card
                    else record.get("kind"),
                    "storageScope": deleted_card.get("scope")
                    if deleted_card
                    else policy["components"]["timeline"]["scope"],
                },
            )
        return self.web.json_response(self.scoped_state_for_user(user_id, session_id))

    async def get_privacy(self, request):
        user_id = request[self.user_key]["id"]
        grant = self.ambient_consent(user_id)
        return self.web.json_response(
            {
                **self.privacy_governance.disclosure(
                    ambient=bool(self.ambient_settings["enabled"] and grant is not None)
                ),
                "retentionPolicies": self.product_database.retention_policies(user_id),
                "evaluation": self.outcome_ledger.summarize(user_id=user_id),
                "ambient": self.ambient_status(user_id),
            }
        )

    async def get_privacy_export(self, request):
        user_id = request[self.user_key]["id"]
        content = await asyncio.to_thread(
            lambda: json.dumps(
                self.privacy_governance.export_account(user_id), ensure_ascii=False
            )
        )
        response = self.web.Response(text=content, content_type="application/json")
        response.headers["Content-Disposition"] = (
            'attachment; filename="egoassist-account-export.json"'
        )
        return response

    async def post_privacy_retention(self, request):
        user_id = request[self.user_key]["id"]
        payload = await request.json()
        category = str(payload.get("category", "")).strip()
        if category not in {"transcript", "observation", "operational", "memory"}:
            raise ValueError("unsupported retention category")
        policy = self.product_database.set_retention_policy(
            user_id,
            category,
            int(payload.get("retentionDays", 365)),
            deletion_mode=str(payload.get("deletionMode", "hard-delete")),
        )
        applied = None
        if bool(payload.get("applyNow")):
            applied = await self.retention.apply(user_id)
        return self.web.json_response(
            {
                "policy": policy,
                "applied": applied,
                "retentionPolicies": self.product_database.retention_policies(user_id),
            }
        )

    async def post_privacy_delete(self, request):
        user_id = request[self.user_key]["id"]
        payload = await request.json() if request.can_read_body else {}
        if payload.get("confirm") is not True:
            raise ValueError("account deletion requires confirm=true")
        self.deleting_insight_users.add(user_id)
        await self.insight_executor.delete_user(user_id)
        await self.invalidate_live_sessions(user_id)
        await self.answer_executor.delete_user(user_id)
        await self.memory_recovery.delete_user(user_id)
        evidence = self.privacy_governance.delete_account(user_id)
        for session_id, owner in list(self.case_owners.items()):
            if owner.user_id == user_id:
                self.case_owners.pop(session_id, None)
        self.user_episodics.pop(user_id, None)
        for key in [key for key in self.case_episodics if key[0] == user_id]:
            self.case_episodics.pop(key, None)
        self.user_runtimes.pop(user_id, None)
        self.user_routing.pop(user_id, None)
        self.user_tools.pop(user_id, None)
        response = self.web.json_response({"deleted": True, "evidence": evidence})
        response.del_cookie(self.AUTH_COOKIE)
        return response
