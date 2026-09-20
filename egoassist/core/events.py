"""Event ownership and payload references shared by persistence and consumers."""

JOB_EVENTS = {
    "answer.job": "turnId",
    "insight.job": "jobId",
    "memory.recovery.job": "turnId",
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
    if kind.startswith("memory."):
        return "memory"
    if kind in {"runtime_event", "model.prompt", "control_decision"}:
        return "operational"
    return None


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
