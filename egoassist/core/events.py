"""Event ownership and payload references shared by persistence and consumers."""

JOB_EVENTS = {
    "answer.job": "turnId",
    "insight.job": "jobId",
    "memory.recovery.job": "turnId",
    "long_event.job": "requestId",
}
TRANSCRIPT_EVENTS = {"message", "turn_inbox", "scene.turn", "scene.transcript", "source.event"}
OBSERVATION_EVENTS = {
    "ambient.frame",
    "ambient_observation",
    "scene.result",
    "observation.review",
}


def retention_category(row):
    kind = row.get("type", "")
    if kind in TRANSCRIPT_EVENTS:
        return "transcript"
    if kind in OBSERVATION_EVENTS:
        return "observation"
    if kind.startswith("memory.") or kind.startswith("long_event."):
        return "memory"
    if kind in {"runtime_event", "model.prompt", "control_decision", "model.request.body", "model.response.body"}:
        return "operational"
    return None


def retention_categories(row):
    """Raw model audits obey both log and source-content retention limits."""
    categories = {retention_category(row)} - {None}
    kind = row.get("eventType") or row.get("type", "")
    if kind.startswith("provider.model."):
        categories.update(("operational", "transcript"))
        if row.get("payload", row).get("stage") == "vision":
            categories.add("observation")
    elif kind in {"model.request.body", "model.response.body"}:
        source_kind = row.get("sourceKind", "")
        if source_kind.startswith("scene."):
            categories.add("transcript" if source_kind.startswith("scene.transcription.") else "observation")
        elif source_kind.startswith(("long_event.", "insight.")):
            categories.add("memory")
        else:
            categories.add("transcript")
    return categories


def references(value):
    """Collect identifiers, excluding the journal's chronological parent link."""
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {
                "parentId",
                "userId",
                "ownerId",
                "sessionId",
                "caseId",
                "taskId",
                "contextId",
                "consentId",
                "personRef",
                "deviceId",
                "checkpointId",
            }:
                continue
            if key == "snapshotId" and isinstance(item, str):
                found.add("evidence-snapshot:" + item)
                continue
            if key.endswith(("Id", "Ids")) or key in {"id", "source_ids", "sha256"}:
                if isinstance(item, str):
                    found.add(item)
                elif isinstance(item, list):
                    found.update(v for v in item if isinstance(v, str))
            if isinstance(item, (dict, list)):
                found.update(references(item))
    elif isinstance(value, list):
        for item in value:
            found.update(references(item))
    return found


def owned_identifiers(row):
    """Identifiers created by an event, excluding the evidence it consumes."""
    result = {row["id"]}
    kind = row.get("type")
    keys = {
        "turn_inbox": ("turnId",),
        "intent_envelope": ("intentId",),
        "job_ledger": ("jobId",),
        "result_inbox": ("resultId",),
        "answer.job": ("responseId",),
        "insight.job": ("jobId",),
        "tool_call": ("callId",),
        "runtime_event": ("responseId",),
    }.get(kind, ())
    result.update(row[key] for key in keys if row.get(key))
    if kind == "runtime_event" and row.get("eventType") == "provider.model.request":
        call_id = row.get("payload", {}).get("callId")
        if call_id:
            result.add(call_id)
    if kind == "evidence.package":
        snapshot = (row.get("packet") or {}).get("snapshotId")
        if snapshot:
            result.add("evidence-snapshot:" + snapshot)
    if kind == 'long_event.batch':
        result.update(event['eventId'] for event in row.get('events', []) if event.get('eventId'))
    if kind == "message":
        message = row.get("message", {})
        if message.get("role") == "user":
            turn_id = (message.get("metadata") or row.get("metadata") or {}).get(
                "turnId"
            )
            if turn_id:
                result.add(turn_id)
        elif row.get("responseId"):
            result.add(row["responseId"])
    return result


def latest(journal, kind, key):
    def reduce(rows, row):
        if row.get("type") == kind and row.get(key):
            rows[row[key]] = row

    return journal.project(("latest", kind, key), dict, reduce)
