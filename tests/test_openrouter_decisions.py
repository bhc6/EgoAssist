import copy
import json

import pytest
import requests

from egoassist.eval.openrouter_decisions import DecisionsClient, validate_answers


QUESTIONS = {
    "flag": {"type": "noul", "instructions": "flag", "criteria": {"true": "yes", "false": "no"}},
    "team": {"type": "choice", "instructions": "team", "criteria": {"a": "A", "b": "B"}},
    "level": {"type": "score", "instructions": "level", "criteria": ["low", "high"]},
}
BODY = {"answers": {
    "flag": {"type": "noul", "noul": 0.9},
    "team": {"type": "choice", "choice": "a", "probabilities": {"a": 0.8, "b": 0.2}},
    "level": {"type": "score", "score": 0.6, "probabilities": {"0": 0.4, "1": 0.6}},
}}


class Session:
    def __init__(self, code=200, body=None, error=None):
        self.code, self.body, self.error = code, body, error
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        response = requests.Response()
        response.status_code = self.code
        response._content = (self.body if self.body is not None else json.dumps(BODY)).encode()
        return response


def test_all_types_and_transport_evidence():
    session = Session()
    result = DecisionsClient("secret-key", session=session).decide("state", QUESTIONS)
    assert result["status"] == "ok"
    assert result["answers"] == BODY["answers"]
    assert "secret-key" not in json.dumps(result)
    assert session.calls[0][1]["allow_redirects"] is False
    assert session.calls[0][1]["json"]["questions"] == QUESTIONS


@pytest.mark.parametrize("field,value", [("noul", True), ("noul", float("nan")), ("noul", 1.1)])
def test_reject_invalid_probabilities(field, value):
    body = copy.deepcopy(BODY)
    body["answers"]["flag"][field] = value
    with pytest.raises(ValueError):
        validate_answers(body, QUESTIONS)


@pytest.mark.parametrize("mutation", ["missing", "unknown-choice", "incomplete-distribution", "mass", "score", "type"])
def test_reject_incomplete_or_invalid_typed_response(mutation):
    body = copy.deepcopy(BODY)
    if mutation == "missing": del body["answers"]["flag"]
    if mutation == "unknown-choice": body["answers"]["team"]["choice"] = "c"
    if mutation == "incomplete-distribution": del body["answers"]["team"]["probabilities"]["b"]
    if mutation == "mass": body["answers"]["team"]["probabilities"]["b"] = 0.8
    if mutation == "score": body["answers"]["level"]["score"] = 2
    if mutation == "type": body["answers"]["flag"]["type"] = "choice"
    with pytest.raises(ValueError): validate_answers(body, QUESTIONS)


@pytest.mark.parametrize("code", [302, 401, 429, 500])
def test_http_failure_preserved_and_never_retried(code):
    session = Session(code, 'error secret-key')
    result = DecisionsClient("secret-key", session=session).decide("state", QUESTIONS)
    assert result["status"] == "unavailable"
    assert result["httpStatus"] == code
    assert len(session.calls) == 1
    assert "secret-key" not in json.dumps(result)
    assert "answers" not in result


def test_timeout_not_converted_to_negative_decision():
    session = Session(error=requests.Timeout("secret-key"))
    result = DecisionsClient("secret-key", session=session).decide("state", QUESTIONS)
    assert result["status"] == "unavailable"
    assert result["errorType"] == "Timeout"
    assert "secret-key" not in json.dumps(result)
    assert "answers" not in result


@pytest.mark.parametrize("body", ['not json', '[]', '{"answers":{}}'])
def test_invalid_payload_is_not_success(body):
    result = DecisionsClient("secret-key", session=Session(body=body)).decide("state", QUESTIONS)
    assert result["status"] == "invalid"
    assert result["rawResponse"] == body


def test_invalid_question_makes_no_request():
    session = Session()
    with pytest.raises(ValueError):
        DecisionsClient("secret-key", session=session).decide("state", {"x": {"type": "invalid"}})
    assert not session.calls


def test_missing_key_rejected():
    with pytest.raises(ValueError): DecisionsClient(" ")
