"""Typed Decisions transport for offline experiments; never executes actions."""

from __future__ import annotations

import time

import requests

from egoassist.core.typed_decisions import _number, validate_answers, validate_questions


ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL = "typesafe/jev-1.13"




class DecisionsClient:
    def __init__(self, api_key: str, *, model: str = DEFAULT_MODEL, timeout: float = 15,
                 session: requests.Session | None = None):
        if not api_key or not api_key.strip():
            raise ValueError("OPENROUTER_API_KEY is missing")
        if not _number(timeout, 0.01, 300):
            raise ValueError("timeout must be between 0.01 and 300 seconds")
        self._api_key = api_key.strip()
        self.model, self.timeout = model, timeout
        self._session = session or requests.Session()
        self._owns_session = session is None

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def decide(self, state: str, questions: dict) -> dict:
        validate_questions(questions)
        if not isinstance(state, str):
            raise ValueError("state must be a string")
        payload = {"model": self.model, "state": state, "questions": questions}
        started = time.perf_counter()
        record = {"status": "unavailable", "endpoint": ENDPOINT, "request": payload}
        try:
            response = self._session.post(
                ENDPOINT, json=payload,
                headers={"Authorization": "Bearer " + self._api_key, "Content-Type": "application/json"},
                timeout=(min(10, self.timeout), self.timeout), allow_redirects=False,
            )
            record["httpStatus"] = response.status_code
            # No headers or credentials in exported request/response evidence.
            raw = response.text.replace(self._api_key, "[REDACTED]")
            record["rawResponse"] = raw
            if response.status_code == 200:
                import json
                try:
                    body = json.loads(raw)
                    record["response"] = body
                    record["answers"] = validate_answers(body, questions)
                    record["status"] = "ok"
                except (ValueError, TypeError) as exc:
                    record["status"] = "invalid"
                    record["errorType"] = type(exc).__name__
            else:
                record["errorType"] = "HTTPError"
        except requests.RequestException as exc:
            # Exception strings may contain request details; record only the type.
            record["errorType"] = type(exc).__name__
        record["latencyMs"] = round((time.perf_counter() - started) * 1000, 2)
        return record
