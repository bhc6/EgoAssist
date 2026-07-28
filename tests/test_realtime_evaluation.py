from egoassist.realtime.evaluation import InterventionRecord, summarize_interventions


def test_sequential_metrics_cover_help_silence_reuse_and_barge_in():
    result = summarize_interventions([
        InterventionRecord("guide", "guide", True, True, 200, True),
        InterventionRecord("silent", "silent", False, False, 100, False),
        InterventionRecord("silent", "guide", False, False, 300, False),
    ])
    assert result["intervention_precision"] == 0.5
    assert result["unnecessary_interruption_rate"] == 0.5
    assert result["followup_reuse"] == 1.0
    assert result["barge_in_success"] == 0.5
    assert result["mean_latency_ms"] == 200
