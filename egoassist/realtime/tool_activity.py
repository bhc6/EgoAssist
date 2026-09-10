"""Project tool lifecycle evidence for the console without inventing outcomes."""


def tool_activity(entries, calls=None):
    calls = {} if calls is None else calls
    for entry in entries:
        kind, call_id = entry.get("type"), entry.get("callId")
        if kind in {"tool.request", "tool.selection", "tool.selection.failed", "tool.selection.finished"}:
            call_id = call_id or entry.get("origin", {}).get("selectionId")
            if not call_id:
                continue
            row = calls.setdefault(call_id, {"callId": call_id})
            row.update(tool=entry.get("model"), origin=entry.get("origin", {}), updatedAt=entry.get("timestamp"))
            if kind == "tool.request":
                row.update(status="selecting", createdAt=entry.get("timestamp"), request={
                    "messages": entry.get("messages"), "tools": entry.get("tools")})
            elif kind == "tool.selection.finished":
                row["status"] = entry["status"]
            else:
                row.update(status="failed" if kind.endswith("failed") else "selection-finished",
                           latencyMs=entry.get("latencyMs"), result=entry.get("message") or {"error": entry.get("error")},
                           usage=entry.get("usage"))
            continue
        if not call_id or kind not in {"tool_call", "tool_confirmation", "tool_execution", "tool_result"}:
            continue
        row = calls.setdefault(call_id, {"callId": call_id, "status": "received"})
        row.update(updatedAt=entry.get("timestamp"))
        if kind == "tool_call":
            row.update(tool=entry.get("name"), arguments=entry.get("arguments"),
                       origin=entry.get("metadata", {}).get("origin", {}), createdAt=entry.get("timestamp"))
        elif kind == "tool_confirmation":
            row.update(status="waiting-confirmation", description=entry.get("description", ""))
        elif kind == "tool_execution":
            row.update(status="running", accepted=entry.get("accepted"))
        else:
            result = entry.get("result")
            status = result.get("status") if isinstance(result, dict) else None
            row.update(status=status or ("failed" if entry.get("isError") else "completed"), result=result,
                       isError=entry.get("isError", False))
    return list(calls.values())
