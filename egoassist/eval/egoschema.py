"""EgoSchema benchmark adapter.

EgoSchema (https://egoschema.github.io) is a long-form video QA benchmark
with 5031 multiple-choice questions derived from Ego4D footage.  Each item
has a video_uid, question, and five answer options (A–E).

This adapter wraps any QA pipeline that accepts (question, evidence_texts,
evidence_frames) and produces a free-text answer, evaluates top-1 accuracy,
and optionally reports per-category breakdowns.

Data format expected (``full_subset.json`` or the 500-item subset)::

    [
      {
        "q_uid": "...",
        "video_uid": "...",
        "question": "...",
        "option 0": "...",
        ...
        "option 4": "...",
        "answer": 2          # 0-indexed correct option
      },
      ...
    ]

Usage
-----
from egoassist.eval.egoschema import EgoSchemaAdapter

adapter = EgoSchemaAdapter("egoschema_fullsuperset.json")
results = adapter.run(qa_fn=my_qa_fn, n=100)
print(results["accuracy"])   # e.g. 0.62
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Callable

LOGGER = logging.getLogger(__name__)

_OPTION_LETTERS = ["A", "B", "C", "D", "E"]


def _parse_option(answer_text: str) -> int | None:
    """Extract the chosen option index (0-4) from an LLM response string."""
    text = answer_text.strip()
    # Explicit letter at start: "A)", "A.", "(A)", "A:"
    m = re.match(r"^\(?([A-Ea-e])\)?[.):\s]", text)
    if m:
        return ord(m.group(1).upper()) - ord("A")
    # Bare letter
    if len(text) == 1 and text.upper() in _OPTION_LETTERS:
        return ord(text.upper()) - ord("A")
    return None


class EgoSchemaAdapter:
    """Evaluate a QA function against the EgoSchema benchmark."""

    def __init__(self, data_path: str | Path) -> None:
        self.data_path = Path(data_path)
        self._items: list[dict[str, Any]] | None = None

    def _load(self) -> list[dict[str, Any]]:
        if self._items is None:
            with self.data_path.open(encoding="utf-8") as fh:
                self._items = json.load(fh)
        return self._items

    def run(
        self,
        qa_fn: Callable[[str, list[str]], str],
        *,
        n: int | None = None,
        verbose: bool = False,
    ) -> dict[str, Any]:
        """Evaluate *qa_fn* on the first *n* items (all items if *n* is None).

        Args:
            qa_fn:    ``fn(question_with_options, evidence_texts) -> answer_str``
                      Pass an empty list as evidence if the model should answer
                      from the question alone.
            n:        Number of items to evaluate (default: full set).
            verbose:  Log each item's prediction.

        Returns:
            dict with keys: accuracy, correct, total, predictions.
        """
        items = self._load()
        if n is not None:
            items = items[:n]

        correct = 0
        predictions: list[dict[str, Any]] = []

        for idx, item in enumerate(items):
            options = [item.get(f"option {i}", "") for i in range(5)]
            option_str = "\n".join(f"  {_OPTION_LETTERS[i]}. {opt}" for i, opt in enumerate(options))
            question = f"{item['question']}\n{option_str}"

            try:
                raw_answer = qa_fn(question, [])
            except Exception as exc:
                LOGGER.warning("EgoSchema qa_fn failed on item %d: %s", idx, exc)
                raw_answer = ""

            pred_idx = _parse_option(raw_answer)
            gold_idx = int(item.get("answer", -1))
            hit = pred_idx == gold_idx

            if hit:
                correct += 1

            entry = {
                "q_uid": item.get("q_uid", str(idx)),
                "predicted": _OPTION_LETTERS[pred_idx] if pred_idx is not None else "?",
                "gold": _OPTION_LETTERS[gold_idx] if 0 <= gold_idx < 5 else "?",
                "correct": hit,
            }
            predictions.append(entry)

            if verbose:
                LOGGER.info("[%d/%d] %s pred=%s gold=%s", idx + 1, len(items),
                            entry["q_uid"], entry["predicted"], entry["gold"])

        total = len(items)
        accuracy = correct / total if total else 0.0
        return {
            "benchmark": "EgoSchema",
            "accuracy": round(accuracy, 4),
            "correct": correct,
            "total": total,
            "predictions": predictions,
        }
