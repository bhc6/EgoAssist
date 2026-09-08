"""Execute an explicitly routed read-only reference request with durable receipts."""
from typing import Any, Callable


def execute_reference_request(harness, execute: Callable[..., dict], declarations: list[dict],
                              origin: dict[str, Any]) -> dict:
    call_id = "reference-" + str(origin["turnId"])
    metadata = {"origin": origin, "source": "semantic-task-control"}
    harness.record_tool_call(call_id, "show_reference", {}, metadata=metadata)
    available = any(tool.get("name") == "show_reference" for tool in declarations)
    try:
        result = execute("show_reference", {}, False, origin) if available else {
            "status": "unsupported", "reason": "No reference tool is available for the active Skill."
        }
    except (ValueError, RuntimeError) as exc:
        result = {"status": "failed", "reason": str(exc)}
    harness.record_tool_result(call_id, "show_reference", result,
                               is_error=result.get("status") != "resolved", metadata=metadata)
    return result
