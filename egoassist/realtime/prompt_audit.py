"""Cursor-based model audit projection; new model-bearing events are discoverable."""

from egoassist.prompts import compact_audit_payload


def decision_audit(row):
    """Keep the actual per-call questions, without replaying media or model context."""
    result = compact_audit_payload(row['type'], row)
    decision = dict(result.get('decision') or {})
    decision.pop('rawOutput', None)
    metrics = dict(decision.get('requestMetrics') or {})
    original = (row.get('decision') or {}).get('requestMetrics') or {}
    for name in ('decisions', 'taskControlCheck', 'cancellationCheck'):
        if name not in original:
            continue
        stage = dict(metrics.get(name) or {})
        stage['questions'] = (original[name].get('request') or {}).get('questions') or {}
        metrics[name] = stage
    decision['requestMetrics'] = metrics
    result['decision'] = decision
    return result


def is_model_event(row):
    kind = row.get("eventType") or row.get("type", "")
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
    return bool(row.get("model") or payload.get("model") or
                kind.startswith(("scene.", "perception.", "tool.", "memory.recovery.")) or
                kind in {"tool_call", "tool_result", "tool_execution", "control_decision", "interaction.classification_retry", "model.prompt",
                         "response.dispatched", "episodic.memory.injected", "context.data.injected"})


def audit_page(rows, *, before=None, limit=30):
    if not 1 <= limit <= 200:
        raise ValueError("audit limit must be between 1 and 200")
    actual = [row for row in rows if is_model_event(row)]
    end = len(actual)
    if before:
        end = next((i for i, row in enumerate(actual) if row["id"] == before), -1)
        if end < 0:
            raise ValueError("unknown audit cursor")
    page = actual[max(0, end - limit):end]
    return {"requests": page, "totalAuditEvents": len(actual),
            "nextBefore": page[0]["id"] if end > limit else None}
