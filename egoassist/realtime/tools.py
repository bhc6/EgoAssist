"""Small, auditable tool executor scoped entirely by the active Skill."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from .capabilities import tool_truth


ToolHandler = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


def _relative_deadline(arguments: dict[str, Any], declaration: dict[str, Any]) -> dict[str, Any]:
    seconds = arguments["seconds"]
    return {
        "status": "unsupported",
        "label": arguments.get("label", "")[:80],
        "dueAt": (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds"),
        "reason": "The local adapter can calculate a deadline but has no persistent alarm delivery scheduler.",
    }


def _local_acknowledgement(arguments: dict[str, Any], declaration: dict[str, Any]) -> dict[str, Any]:
    configured = declaration.get("result", {})
    return {
        "status": str(configured.get("status", "demo-only")),
        "message": str(configured.get("message", "No external product connector is configured.")),
    }


DEFAULT_HANDLERS: dict[str, ToolHandler] = {
    "builtin.relative-deadline.v1": _relative_deadline,
    "builtin.local-acknowledgement.v1": _local_acknowledgement,
}


def tool_result_succeeded(result: Any, *, is_error: bool = False) -> bool:
    if is_error or not isinstance(result, dict):
        return False
    return str(result.get("status", "")).casefold() in {
        "accepted", "completed", "ok", "scheduled", "success", "succeeded",
    }


class SkillToolExecutor:
    """Validate a Skill-owned contract, then dispatch to a registered adapter."""

    def __init__(
        self,
        handlers: Mapping[str, ToolHandler] | None = None,
        *,
        audit: list[dict[str, Any]] | None = None,
    ) -> None:
        self._handlers = dict(DEFAULT_HANDLERS if handlers is None else handlers)
        self.audit: list[dict[str, Any]] = [dict(item) for item in (audit or [])]

    def register_handler(self, name: str, handler: ToolHandler) -> None:
        if not name or name in self._handlers:
            raise ValueError(f"tool handler is already registered or invalid: {name}")
        self._handlers[name] = handler

    def execute(
        self,
        tool: str,
        arguments: dict[str, Any],
        declared_tools: list[dict[str, Any]],
        confirmed: bool = False,
    ) -> dict[str, Any]:
        declaration = next((item for item in declared_tools if item.get("name") == tool), None)
        if declaration is None:
            raise ValueError(f"tool is not available in the active skill: {tool}")
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")
        validated = self._validate_arguments(arguments, declaration.get("arguments", {}))
        availability = tool_truth(declaration)
        if not availability["executable"]:
            result = {
                "status": availability["status"],
                "tool": str(tool),
                "reason": availability["reason"],
            }
            self.audit.append({
                "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "tool": tool,
                "handler": str(declaration.get("handler", "")),
                "arguments": validated,
                "result": result,
            })
            return result
        if declaration.get("requires_confirmation", True) and not confirmed:
            return {"status": "confirmation_required", "tool": tool, "arguments": validated}
        handler_name = str(declaration.get("handler", ""))
        handler = self._handlers.get(handler_name)
        if handler is None:
            raise ValueError(f"tool handler is not registered: {handler_name or '<missing>'}")
        result = handler(validated, declaration)
        self.audit.append({
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tool": tool,
            "handler": handler_name,
            "arguments": validated,
            "result": result,
        })
        return result

    @staticmethod
    def _validate_arguments(arguments: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
        if schema and schema.get("type", "object") != "object":
            raise ValueError("tool argument schema must describe an object")
        properties = schema.get("properties", {}) if schema else {}
        unknown = set(arguments) - set(properties)
        if unknown and not schema.get("additionalProperties", False):
            raise ValueError(f"undeclared tool arguments: {', '.join(sorted(unknown))}")
        validated = dict(arguments)
        for name, field in properties.items():
            if name not in validated and "default" in field:
                validated[name] = field["default"]
        missing = set(schema.get("required", [])) - set(validated)
        if missing:
            raise ValueError(f"missing required tool arguments: {', '.join(sorted(missing))}")
        for name, value in validated.items():
            field = properties.get(name)
            if field:
                SkillToolExecutor._validate_value(name, value, field)
        return validated

    @staticmethod
    def _validate_value(name: str, value: Any, field: dict[str, Any]) -> None:
        expected = field.get("type")
        valid = {
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "string": isinstance(value, str),
            "boolean": isinstance(value, bool),
        }.get(expected, True)
        if not valid:
            raise ValueError(f"invalid type for tool argument {name}: expected {expected}")
        if expected in {"integer", "number"}:
            if "minimum" in field and value < field["minimum"]:
                raise ValueError(f"tool argument below minimum: {name}")
            if "maximum" in field and value > field["maximum"]:
                raise ValueError(f"tool argument above maximum: {name}")
        if expected == "string" and len(value) > field.get("maxLength", len(value)):
            raise ValueError(f"tool argument exceeds maxLength: {name}")
