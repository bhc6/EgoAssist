"""Bind async tool decisions and durable execution receipts to one response."""
import asyncio
from dataclasses import replace
import json
from uuid import uuid4

from egoassist.prompts import prompt_text


class ToolCallRunner:
    def __init__(self, bridge):
        self.bridge = bridge
        self.tasks = {}

    def start(self, request):
        self.bridge.loop.call_soon_threadsafe(self._start, request)

    def _start(self, request):
        if self.bridge.closed:
            return
        task = asyncio.create_task(self._run(request))
        self.tasks[task] = False
        def finished(completed):
            self.tasks.pop(completed, None)
            failed = completed.cancelled() or completed.exception() is not None
            if failed and self.bridge._inflight_response is request:
                with self.bridge._response_lock:
                    self.bridge._inflight_response = None
                    self.bridge._response_pending = False
                    self.bridge._cancel_when_created = False
                self.bridge._return_result_to_inbox(request, "tool-task-stopped")
                self.bridge._schedule_queued_dispatch()
        task.add_done_callback(finished)

    def cancel_selection(self):
        def cancel():
            for task, executing in tuple(self.tasks.items()):
                if not executing:
                    task.cancel()
        self.bridge.loop.call_soon_threadsafe(cancel)

    async def close(self):
        for task, executing in tuple(self.tasks.items()):
            if not executing:
                task.cancel()
        # An accepted effect must commit its receipt even after its socket closes.
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)

    async def _run(self, request):
        bridge = self.bridge
        generation = bridge._dialogue_generation
        origin = dict(request.origin or {})
        selection_id = "selection-" + uuid4().hex
        result_ids = []
        outcome = "selection-finished"

        def current():
            return (not bridge.closed and generation == bridge._dialogue_generation
                    and bridge._inflight_response is request and not bridge._cancel_when_created)

        def reply(instructions):
            nonlocal request
            with bridge._response_lock:
                if not current():
                    return
                updated_origin = {**origin, "resultIds": result_ids} if result_ids else origin
                request = replace(request, instructions=instructions, origin=updated_origin)
                bridge._inflight_response = request
            bridge._send_response_direct(request)

        try:
            if not current():
                return
            bridge._schedule({"type": "tool_status", "callId": selection_id,
                              "tool": bridge.tool_model.model, "status": "selecting", "origin": origin})
            from .settings import tool_settings
            budget = tool_settings()
            history, receipts = [], {}
            calls_used = 0
            selection = {}
            for round_index in range(budget["maxRounds"]):
                bridge._context_harness.journal.append("tool.round", {
                    "selectionId": selection_id, "round": round_index + 1, "budget": budget,
                    "callsUsed": calls_used, "origin": origin, "model": bridge.tool_model.model})
                selection = await bridge.tool_model.select(
                    request.instructions, bridge._provider_tools, bridge._context_harness,
                    {**origin, "selectionId": selection_id, "round": round_index + 1}, history=history)
                if not current():
                    outcome = "interrupted"
                    return
                calls = selection.get("tool_calls") or []
                if not calls:
                    break
                history.append({"role": "assistant", "content": selection.get("content") or "", "tool_calls": calls})
                for index, call in enumerate(calls):
                    if not current():
                        outcome = "interrupted"
                        return
                    if calls_used >= budget["maxCalls"]:
                        outcome = "budget-exhausted"
                        bridge._context_harness.journal.append("tool.deferred", {
                            "selectionId": selection_id, "origin": origin, "pendingCalls": calls[index:], "budget": budget})
                        break
                    name, arguments = call["function"]["name"], json.loads(call["function"]["arguments"])
                    call_id = call["id"]
                    signature = (name, json.dumps(arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":")))
                    claim, cached = bridge._claim_tool_call(call_id, signature)
                    if claim not in {"new", "duplicate"}:
                        raise ValueError("Tool call identity was reused with different arguments")
                    # Model retries must not execute the same effect a second time.
                    if signature in receipts:
                        result = receipts[signature]
                    else:
                        if claim != "new":
                            raise ValueError("Tool call identity was reused outside this execution")
                        calls_used += 1
                        bridge._context_harness.record_tool_call(call_id, name, arguments, metadata={"origin": origin})
                        bridge._context_harness.journal.append("tool_execution", {
                            "callId": call_id, "accepted": True, "policy": "automatic", "origin": origin,
                        }, idempotency_key=f"tool-execution:{call_id}")
                        bridge._schedule({"type": "tool_status", "callId": call_id, "tool": name,
                                          "arguments": arguments, "origin": origin, "status": "running"})
                        self.tasks[asyncio.current_task()] = True
                        try:
                            result = await asyncio.to_thread(bridge._invoke_tool_handler, name, arguments, True, origin)
                            if not isinstance(result, dict):
                                raise ValueError("tool handler must return a result object")
                            is_error = result.get("status") in {"failed", "error", "interrupted", "rejected", "unsupported"}
                        except Exception as exc:
                            result, is_error = {"status": "error", "error": str(exc)}, True
                        committed = bridge._complete_tool_call(call_id, name, result, is_error, False, origin,
                                                               continue_response=False, publish_context=current())
                        self.tasks[asyncio.current_task()] = False
                        if committed.get("resultId"):
                            result_ids.append(committed["resultId"])
                        receipts[signature] = result
                    history.append({"role": "tool", "tool_call_id": call_id, "name": name,
                                    "content": json.dumps(result, ensure_ascii=False)})
                if outcome == "budget-exhausted":
                    break
            else:
                outcome = "budget-exhausted"
            if current():
                bridge.inject_context_data("tool_selection", {"content": selection.get("content"), "origin": origin,
                                           "status": outcome, "callsUsed": calls_used, "budget": budget})
                if outcome == "budget-exhausted":
                    bridge._context_harness.journal.append("tool.budget.exhausted", {
                        "origin": origin, "selectionId": selection_id, "callsUsed": calls_used, "budget": budget})
                reply(request.instructions + "\n" + prompt_text(
                    "tool_use/budget_exhausted" if outcome == "budget-exhausted" else "tool_use/after_selection"))
        except asyncio.CancelledError:
            outcome = "interrupted"
        except Exception as exc:
            outcome = "failed"
            if current():
                bridge._schedule({"type": "error", "message": "Tool decision failed: " + type(exc).__name__})
                reply(prompt_text("tool_use/unavailable"))
        finally:
            bridge._context_harness.journal.append("tool.selection.finished", {
                "callId": selection_id, "status": outcome, "model": bridge.tool_model.model, "origin": origin,
            })
            bridge._schedule({"type": "tool_status", "callId": selection_id,
                              "tool": bridge.tool_model.model, "origin": origin, "status": outcome})
            if not current() and bridge._inflight_response is request:
                with bridge._response_lock:
                    bridge._inflight_response = None
                    bridge._response_pending = False
                    bridge._cancel_when_created = False
                bridge._return_result_to_inbox(request, "tool-selection-interrupted")
                bridge._schedule_queued_dispatch()
