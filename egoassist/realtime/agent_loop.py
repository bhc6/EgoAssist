"""Plan-Act-Observe agent loop for EgoAssist.

Implements a ReAct-style multi-turn loop:

    observe (context + memory) → think (LLM) → tool_call → observe (result) → …

The loop is transport-agnostic: inject any async LLM callable via ``llm_fn``.
Tool execution is delegated to ``SkillToolExecutor``, which already supports
MCP providers via ``attach_mcp_provider``.

Full trajectory is written to ``ContextHarness`` (JSONL) for replay/debug.

Usage
-----
from egoassist.realtime.agent_loop import AgentLoop

loop = AgentLoop(
    llm_fn=my_openai_call,
    tool_executor=executor,
    harness=harness,
    max_turns=12,
)
answer = await loop.run("What did I say I'd buy at the supermarket?",
                        memory_context="... retrieved memories ...")
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

LOGGER = logging.getLogger(__name__)

# Sentinel: LLM returned this to signal no more tool calls needed.
_STOP = object()


@dataclass
class Turn:
    """A single step in the agent trajectory."""
    role: str                          # "user" | "assistant" | "tool"
    content: str
    tool_call: dict[str, Any] | None = None
    tool_result: dict[str, Any] | None = None
    elapsed_ms: float = 0.0
    span_id: str = ""


@dataclass
class LoopResult:
    """Return value of ``AgentLoop.run()``."""
    answer: str
    turns: list[Turn] = field(default_factory=list)
    turn_count: int = 0
    stopped_reason: str = "answer"     # "answer" | "max_turns" | "tool_error"
    total_ms: float = 0.0


class AgentLoop:
    """ReAct-style observe→think→act loop with SkillToolExecutor.

    Parameters
    ----------
    llm_fn:
        Async callable: ``(messages: list[dict]) -> dict``
        The returned dict should follow the OpenAI chat-completion shape::

            {"role": "assistant", "content": "...", "tool_calls": [...]}  # or
            {"role": "assistant", "content": "final answer"}

    tool_executor:
        ``SkillToolExecutor`` instance with all desired tool handlers registered.
        MCP providers can be attached via ``executor.attach_mcp_provider()``.

    harness:
        ``ContextHarness`` for trajectory logging; pass ``None`` to skip logging.

    max_turns:
        Hard cap on tool-call iterations to prevent runaway loops.

    tool_declarations:
        Optional list of tool schema dicts to pass to the LLM.  If ``None``,
        the loop sends no tool schema (the LLM cannot call tools).

    retry_on_tool_error:
        If ``True``, a tool error is reported back to the LLM and the loop
        continues; otherwise the loop stops immediately with ``"tool_error"``.
    """

    def __init__(
        self,
        *,
        llm_fn: Callable[[list[dict[str, Any]]], Awaitable[dict[str, Any]]],
        tool_executor: Any,        # SkillToolExecutor – avoid circular import
        harness: Any | None = None,
        max_turns: int = 12,
        tool_declarations: list[dict[str, Any]] | None = None,
        retry_on_tool_error: bool = True,
        timeout_seconds: float = 120.0,
    ) -> None:
        self._llm_fn = llm_fn
        self._executor = tool_executor
        self._harness = harness
        self.max_turns = int(max_turns)
        self._tool_declarations = tool_declarations or []
        self._retry_on_tool_error = retry_on_tool_error
        self._timeout = float(timeout_seconds)

    async def run(
        self,
        user_message: str,
        *,
        system: str = "",
        memory_context: str = "",
    ) -> LoopResult:
        """Execute the loop and return a ``LoopResult``.

        Args:
            user_message:   The user's request.
            system:         Optional system prompt prepended before memory.
            memory_context: Retrieved memory text injected after the system prompt.
        """
        t0 = time.monotonic()
        turns: list[Turn] = []

        # Build initial message list
        messages: list[dict[str, Any]] = []
        sys_parts = [p for p in [system, memory_context] if p]
        if sys_parts:
            messages.append({"role": "system", "content": "\n\n".join(sys_parts)})
        messages.append({"role": "user", "content": user_message})
        turns.append(Turn(role="user", content=user_message))

        stopped_reason = "max_turns"
        final_answer = ""

        for turn_idx in range(self.max_turns):
            # --- Think: call the LLM ---
            t1 = time.monotonic()
            try:
                response = await asyncio.wait_for(
                    self._llm_fn(messages),
                    timeout=self._timeout,
                )
            except asyncio.TimeoutError:
                LOGGER.warning("AgentLoop: LLM call timed out at turn %d", turn_idx)
                stopped_reason = "tool_error"
                break
            elapsed_ms = (time.monotonic() - t1) * 1000

            content = response.get("content") or ""
            tool_calls = response.get("tool_calls") or []

            # Record assistant turn
            assistant_turn = Turn(
                role="assistant",
                content=content,
                tool_call=tool_calls[0] if tool_calls else None,
                elapsed_ms=elapsed_ms,
            )
            turns.append(assistant_turn)
            messages.append({"role": "assistant", "content": content,
                             "tool_calls": tool_calls or None})

            # --- No tool call: final answer ---
            if not tool_calls:
                final_answer = content
                stopped_reason = "answer"
                self._log(turns[-1])
                break

            # --- Act: execute each tool call ---
            for tc in tool_calls:
                fn_name = tc.get("function", {}).get("name", "") or tc.get("name", "")
                raw_args = tc.get("function", {}).get("arguments", "{}") or tc.get("arguments", "{}")
                if isinstance(raw_args, str):
                    try:
                        args = json.loads(raw_args)
                    except json.JSONDecodeError:
                        args = {}
                else:
                    args = dict(raw_args)

                t2 = time.monotonic()
                try:
                    # Support both sync and async handlers
                    raw_result = self._executor.execute(
                        fn_name, args, self._tool_declarations, confirmed=True
                    )
                    if inspect.isawaitable(raw_result):
                        raw_result = await raw_result
                    result_dict = raw_result if isinstance(raw_result, dict) else {"output": str(raw_result)}
                    is_error = False
                except Exception as exc:
                    result_dict = {"status": "error", "message": str(exc)}
                    is_error = True
                    LOGGER.warning("AgentLoop: tool %r failed: %s", fn_name, exc)

                tool_turn = Turn(
                    role="tool",
                    content=json.dumps(result_dict, ensure_ascii=False),
                    tool_result=result_dict,
                    elapsed_ms=(time.monotonic() - t2) * 1000,
                )
                turns.append(tool_turn)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", fn_name),
                    "content": tool_turn.content,
                })

                if is_error and not self._retry_on_tool_error:
                    stopped_reason = "tool_error"
                    final_answer = content
                    break

            if stopped_reason == "tool_error":
                break

        # Log trajectory to harness
        if self._harness is not None:
            try:
                self._harness.record_agent_loop(
                    user_message=user_message,
                    turns=[{"role": t.role, "content": t.content} for t in turns],
                    stopped_reason=stopped_reason,
                )
            except Exception:
                pass  # harness logging must never crash the loop

        return LoopResult(
            answer=final_answer,
            turns=turns,
            turn_count=len([t for t in turns if t.role == "assistant"]),
            stopped_reason=stopped_reason,
            total_ms=(time.monotonic() - t0) * 1000,
        )

    def _log(self, turn: Turn) -> None:
        LOGGER.debug("AgentLoop[%s] elapsed=%.0fms content=%.120s",
                     turn.role, turn.elapsed_ms, turn.content)
