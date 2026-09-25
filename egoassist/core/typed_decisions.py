"""Structural validation shared by research and runtime typed decisions."""

import math
from typing import Any


def _number(value: Any, low: float, high: float) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and low <= value <= high)


def validate_questions(questions: dict) -> None:
    if not isinstance(questions, dict) or not questions:
        raise ValueError("questions must be a nonempty object")
    for name, question in questions.items():
        if not isinstance(name, str) or not isinstance(question, dict):
            raise ValueError("invalid question")
        kind, criteria = question.get("type"), question.get("criteria")
        if not isinstance(question.get("instructions"), str):
            raise ValueError("question requires instructions")
        if kind in {"choice", "noul"}:
            if (not isinstance(criteria, dict) or not criteria
                    or not all(isinstance(k, str) and isinstance(v, str) for k, v in criteria.items())):
                raise ValueError("choice/noul criteria must be a string mapping")
            if kind == "noul" and set(criteria) != {"true", "false"}:
                raise ValueError("noul criteria must define true and false")
        elif kind == "score":
            if not isinstance(criteria, list) or len(criteria) < 2 or not all(isinstance(x, str) for x in criteria):
                raise ValueError("score criteria must be an ordered list")
        else:
            raise ValueError("unsupported question type")


def validate_answers(body: Any, questions: dict) -> dict:
    """Check shape/ranges only; valid output does not establish semantic truth."""
    answers = body.get("answers") if isinstance(body, dict) else None
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise ValueError("answer keys differ from requested questions")
    for name, question in questions.items():
        answer, kind = answers[name], question["type"]
        if not isinstance(answer, dict) or answer.get("type") != kind:
            raise ValueError("answer type mismatch")
        if kind == "noul":
            if not _number(answer.get("noul"), 0, 1):
                raise ValueError("invalid noul probability")
            continue
        criteria = question["criteria"]
        keys = set(criteria) if kind == "choice" else {str(i) for i in range(len(criteria))}
        probabilities = answer.get("probabilities")
        if (not isinstance(probabilities, dict) or set(probabilities) != keys
                or not all(_number(x, 0, 1) for x in probabilities.values())
                or abs(sum(probabilities.values()) - 1) > 0.03):
            raise ValueError("invalid probability distribution")
        if kind == "choice" and (not isinstance(answer.get("choice"), str) or answer["choice"] not in keys):
            raise ValueError("unknown choice")
        if kind == "score" and not _number(answer.get("score"), 0, len(criteria) - 1):
            raise ValueError("score out of range")
        if "confidence" in answer and not _number(answer["confidence"], 0, 1):
            raise ValueError("invalid confidence")
    return answers
