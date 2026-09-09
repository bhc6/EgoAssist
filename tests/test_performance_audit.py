from egoassist.realtime.performance import performance_snapshot


def test_performance_uses_measured_values_and_retains_response_attribution():
    events = [
        {"type": "control_decision", "eventId": "route", "payload": {"decision": {"latencyMs": 430, "timedOut": True}}},
        {"type": "response.first_audio", "eventId": "a1", "responseId": "r1", "latencyMs": 600},
        {"type": "response.first_audio", "eventId": "a2", "responseId": "r2", "latencyMs": 800},
        {"type": "response.first_text", "latencyMs": None},
        {"type": "response.first_audio", "latencyMs": float("nan")},
        {"type": "response.finished", "responseId": "r2", "payload": {
            "usage": {"inputTokens": 1200, "outputTokens": 55}, "status": "response.done"}},
    ]
    result = performance_snapshot(events)
    audio = result["metrics"]["responseFirstAudioMs"]
    assert (audio["value"], audio["median"], audio["samples"]) == (800, 700, 2)
    assert (audio["eventId"], audio["responseId"]) == ("a2", "r2")
    assert "responseFirstTextMs" not in result["metrics"]
    assert result["timeouts"] == 1
    assert result["latestUsage"]["inputTokens"] == 1200
    assert performance_snapshot([])["metrics"] == {}
def test_polling_projection_keeps_full_prompts_in_original_audit():
    from egoassist.prompts import compact_audit_payload

    event = {"decision": {"latencyMs": 12.5, "requestMetrics": {
        "messages": [{"role": "system", "content": "actual provider instructions"}], "httpStatus": 200,
    }}}
    compact = compact_audit_payload("control_decision", event)
    assert "messages" not in compact["decision"]["requestMetrics"]
    assert compact["decision"]["latencyMs"] == 12.5
    assert event["decision"]["requestMetrics"]["messages"][0]["content"] == "actual provider instructions"
    prompt = {"instructions": "actual prompt", "model": "provider-model"}
    assert compact_audit_payload("model.prompt", prompt)["instructionsLength"] == len("actual prompt")
    assert prompt["instructions"] == "actual prompt"
